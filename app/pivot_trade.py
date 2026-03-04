"""
Pivot Trade Logic — monitors open positions and executes pivots.

Flow:
  1. Scan open positions from Kalshi (or from placed_orders DB)
  2. For each position, check if price dropped >= STOP_LOSS_PCT from entry
  3. If stop-loss triggered:
     a. Check live score via TennisApi1
     b. If momentum_score >= 2 (underdog winning) → PIVOT:
        - Sell favorite position
        - Buy underdog at deflated price
     c. If momentum_score < 2 → just sell (normal stop-loss exit)
  4. Log everything to pivot_trades table

Config (.env):
  STOP_LOSS_PCT=35           Sell when price drops this % from entry (default: 35)
  PIVOT_MIN_MOMENTUM=2       Min momentum_score to trigger pivot (default: 2)
  PIVOT_SCAN_SECONDS=30      How often to scan positions (default: 30)
  DRY_RUN=true               Don't place real orders (default: true)
"""

import os
import json
import logging
import aiosqlite
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.kalshi_orders import (
    fetch_positions,
    sell_position,
    place_limit_order,
    fetch_market_price,
    fetch_event_markets,
    CONTRACTS_PER_TRADE,
)
from app.live_scores import find_live_score

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "orders.db"

# Configuration
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "35"))
PIVOT_MIN_MOMENTUM = int(os.getenv("PIVOT_MIN_MOMENTUM", "2"))
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() != "false"

# In-memory state
_last_scan: Optional[datetime] = None
_last_scan_summary: dict = {}
_total_pivots_this_session: int = 0
_total_stop_losses_this_session: int = 0


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

async def init_pivot_db():
    """Create the pivot_trades tracking table."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pivot_trades (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                event_ticker     TEXT    NOT NULL,
                -- Original position (favorite)
                fav_ticker       TEXT    NOT NULL,
                fav_name         TEXT    NOT NULL,
                dog_name         TEXT    NOT NULL,
                entry_price      INTEGER NOT NULL,
                exit_price       INTEGER NOT NULL,
                price_drop_pct   REAL    NOT NULL,
                -- Live score at time of pivot
                momentum_score   INTEGER,
                live_sets        TEXT,
                live_games       TEXT,
                -- Pivot action
                action           TEXT    NOT NULL,
                dog_ticker       TEXT,
                dog_buy_price    INTEGER,
                contracts        INTEGER NOT NULL,
                -- Meta
                dry_run          INTEGER NOT NULL DEFAULT 1,
                sell_result      TEXT,
                buy_result       TEXT,
                scanned_at       TEXT    NOT NULL
            )
        """)
        await db.commit()


async def record_pivot(
    event_ticker: str,
    fav_ticker: str,
    fav_name: str,
    dog_name: str,
    entry_price: int,
    exit_price: int,
    price_drop_pct: float,
    momentum_score: Optional[int],
    live_sets: Optional[str],
    live_games: Optional[str],
    action: str,
    dog_ticker: Optional[str],
    dog_buy_price: Optional[int],
    contracts: int,
    dry_run: bool,
    sell_result: str,
    buy_result: str,
):
    """Persist a pivot trade (or stop-loss exit) to SQLite."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO pivot_trades
                (event_ticker, fav_ticker, fav_name, dog_name,
                 entry_price, exit_price, price_drop_pct,
                 momentum_score, live_sets, live_games,
                 action, dog_ticker, dog_buy_price, contracts,
                 dry_run, sell_result, buy_result, scanned_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_ticker, fav_ticker, fav_name, dog_name,
                entry_price, exit_price, round(price_drop_pct, 2),
                momentum_score, live_sets, live_games,
                action, dog_ticker, dog_buy_price, contracts,
                1 if dry_run else 0, sell_result, buy_result,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()


async def get_pivot_history(limit: int = 100) -> list[dict]:
    """Return pivot trade history (newest first)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM pivot_trades ORDER BY scanned_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def already_pivoted(event_ticker: str) -> bool:
    """Check if we already pivoted on this event."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM pivot_trades WHERE event_ticker = ?",
            (event_ticker,),
        )
        return await cursor.fetchone() is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_bot_enabled() -> bool:
    """Check if the auto-sell bot is enabled."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                )
            """)
            await db.commit()
            async with db.execute(
                "SELECT value FROM bot_settings WHERE key='bot_enabled'"
            ) as cur:
                row = await cur.fetchone()
                return row is None or row[0] == "true"
    except Exception:
        return True


def _find_underdog_ticker(
    event_markets: list[dict],
    fav_ticker: str,
) -> tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Given all markets in an event, find the underdog's ticker.
    Returns (dog_ticker, dog_name, current_yes_price) or (None, None, None).
    """
    for m in event_markets:
        ticker = m.get("ticker", "")
        if ticker and ticker != fav_ticker:
            yes_sub = m.get("yes_sub_title", "")
            # Get best available price
            price = m.get("last_price") or m.get("yes_ask") or m.get("yes_bid")
            return ticker, yes_sub, price
    return None, None, None


async def _get_entry_data_from_db(event_ticker: str) -> Optional[dict]:
    """Look up the original entry from placed_orders for this event."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM placed_orders
               WHERE event_ticker = ? AND status IN ('placed', 'simulated')
               ORDER BY placed_at DESC LIMIT 1""",
            (event_ticker,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Main pivot scan
# ---------------------------------------------------------------------------

async def scan_and_pivot() -> dict:
    """
    Execute one pivot trade scan cycle.

    For each open Kalshi position:
    1. Compare current price to entry price
    2. If dropped >= STOP_LOSS_PCT → check live score
    3. If momentum >= PIVOT_MIN_MOMENTUM → sell fav + buy dog
    4. If momentum < PIVOT_MIN_MOMENTUM → just sell (stop loss)
    """
    global _last_scan, _last_scan_summary
    global _total_pivots_this_session, _total_stop_losses_this_session

    _last_scan = datetime.now(timezone.utc)

    summary = {
        "scanned_at": _last_scan.isoformat(),
        "dry_run": DRY_RUN,
        "bot_enabled": True,
        "positions_checked": 0,
        "stop_losses_triggered": 0,
        "pivots_executed": 0,
        "stop_loss_only": 0,
        "live_score_unavailable": 0,
        "already_pivoted": 0,
        "no_drop": 0,
        "details": [],
    }

    # Check if bot is enabled
    if not await _get_bot_enabled():
        summary["bot_enabled"] = False
        _last_scan_summary = summary
        logger.debug("Pivot scan skipped — bot disabled")
        return summary

    # Fetch all open positions from Kalshi
    positions = await fetch_positions()
    summary["positions_checked"] = len(positions)

    if not positions:
        _last_scan_summary = summary
        return summary

    for pos in positions:
        ticker = pos.get("ticker", "")
        event_ticker = pos.get("event_ticker", "")
        position_count = pos.get("total_traded", 0) or pos.get("position", 0)
        # Kalshi returns resting_orders_count / market_exposure / realized_pnl etc.
        # The average entry price might be in different fields depending on API version

        if not ticker or not event_ticker:
            continue

        # Skip if already pivoted on this event
        if await already_pivoted(event_ticker):
            summary["already_pivoted"] += 1
            continue

        detail = {
            "ticker": ticker,
            "event_ticker": event_ticker,
        }

        # Get entry data from our DB
        entry_data = await _get_entry_data_from_db(event_ticker)
        if not entry_data:
            detail["action"] = "skipped_no_entry_data"
            summary["details"].append(detail)
            continue

        entry_price = entry_data["target_price"]  # cents
        fav_name = entry_data["player_fav"]
        dog_name = entry_data["player_dog"]
        contracts = entry_data["contracts"]

        detail["fav_name"] = fav_name
        detail["dog_name"] = dog_name
        detail["entry_price"] = entry_price

        # Get current market price
        market = await fetch_market_price(ticker)
        if not market:
            detail["action"] = "skipped_no_market_data"
            summary["details"].append(detail)
            continue

        current_price = market.get("last_price") or market.get("yes_bid") or 0
        detail["current_price"] = current_price

        if current_price <= 0 or entry_price <= 0:
            detail["action"] = "skipped_bad_price"
            summary["details"].append(detail)
            continue

        # Calculate price drop percentage
        price_drop_pct = ((entry_price - current_price) / entry_price) * 100
        detail["price_drop_pct"] = round(price_drop_pct, 1)

        if price_drop_pct < STOP_LOSS_PCT:
            detail["action"] = "holding"
            summary["no_drop"] += 1
            summary["details"].append(detail)
            continue

        # ---------------------------------------------------------------
        # STOP-LOSS TRIGGERED — now check live score for pivot decision
        # ---------------------------------------------------------------
        summary["stop_losses_triggered"] += 1
        logger.info(
            f"STOP-LOSS triggered: {fav_name} vs {dog_name} "
            f"entry={entry_price}¢ current={current_price}¢ "
            f"drop={price_drop_pct:.1f}%"
        )

        # Check live score
        live_result = await find_live_score(fav_name, dog_name)

        momentum = None
        live_sets_str = None
        live_games_str = None

        if live_result:
            score, fav_is_home = live_result
            momentum = score.momentum_score(fav_is_home)
            live_sets_str = f"{score.home_sets}-{score.away_sets}"
            live_games_str = f"{score.home_games}-{score.away_games}"
            detail["momentum_score"] = momentum
            detail["live_sets"] = live_sets_str
            detail["live_games"] = live_games_str
            detail["fav_is_home"] = fav_is_home
        else:
            detail["live_score"] = "unavailable"
            summary["live_score_unavailable"] += 1

        # Decide: PIVOT or just STOP-LOSS
        if momentum is not None and momentum >= PIVOT_MIN_MOMENTUM:
            # -------------------------------------------------------
            # PIVOT: sell favorite + buy underdog
            # -------------------------------------------------------
            detail["action"] = "pivot"

            # Step 1: Sell the favorite
            sell_price = max(current_price, 1)  # sell at current market
            sell_result = await sell_position(
                ticker=ticker,
                yes_price=sell_price,
                count=contracts,
                dry_run=DRY_RUN,
            )
            detail["sell_result"] = sell_result

            # Step 2: Find and buy the underdog
            event_markets = await fetch_event_markets(event_ticker)
            dog_ticker, dog_full_name, dog_price = _find_underdog_ticker(
                event_markets, ticker
            )
            detail["dog_ticker"] = dog_ticker
            detail["dog_price"] = dog_price

            buy_result_str = ""
            dog_buy_price = None

            if dog_ticker and dog_price:
                # The underdog price is deflated — buy it
                dog_buy_price = dog_price
                buy_result = await place_limit_order(
                    ticker=dog_ticker,
                    yes_price=dog_buy_price,
                    count=contracts,
                    dry_run=DRY_RUN,
                )
                detail["buy_result"] = buy_result
                buy_result_str = json.dumps(buy_result)
            else:
                detail["buy_result"] = "no_dog_ticker_found"
                buy_result_str = "no_dog_ticker_found"

            # Record the pivot
            await record_pivot(
                event_ticker=event_ticker,
                fav_ticker=ticker,
                fav_name=fav_name,
                dog_name=dog_name,
                entry_price=entry_price,
                exit_price=current_price,
                price_drop_pct=price_drop_pct,
                momentum_score=momentum,
                live_sets=live_sets_str,
                live_games=live_games_str,
                action="pivot",
                dog_ticker=dog_ticker,
                dog_buy_price=dog_buy_price,
                contracts=contracts,
                dry_run=DRY_RUN,
                sell_result=json.dumps(sell_result),
                buy_result=buy_result_str,
            )

            summary["pivots_executed"] += 1
            _total_pivots_this_session += 1

            logger.info(
                f"PIVOT executed: sold {fav_name} @ {sell_price}¢, "
                f"bought {dog_name} @ {dog_buy_price}¢ "
                f"(momentum={momentum}, sets={live_sets_str})"
            )
        else:
            # -------------------------------------------------------
            # STOP-LOSS ONLY: sell and exit, no pivot
            # -------------------------------------------------------
            detail["action"] = "stop_loss_only"
            reason = (
                f"momentum={momentum}" if momentum is not None
                else "live_score_unavailable"
            )
            detail["no_pivot_reason"] = reason

            sell_price = max(current_price, 1)
            sell_result = await sell_position(
                ticker=ticker,
                yes_price=sell_price,
                count=contracts,
                dry_run=DRY_RUN,
            )
            detail["sell_result"] = sell_result

            await record_pivot(
                event_ticker=event_ticker,
                fav_ticker=ticker,
                fav_name=fav_name,
                dog_name=dog_name,
                entry_price=entry_price,
                exit_price=current_price,
                price_drop_pct=price_drop_pct,
                momentum_score=momentum,
                live_sets=live_sets_str,
                live_games=live_games_str,
                action="stop_loss_only",
                dog_ticker=None,
                dog_buy_price=None,
                contracts=contracts,
                dry_run=DRY_RUN,
                sell_result=json.dumps(sell_result),
                buy_result="",
            )

            summary["stop_loss_only"] += 1
            _total_stop_losses_this_session += 1

            logger.info(
                f"STOP-LOSS exit (no pivot): sold {fav_name} @ {sell_price}¢ "
                f"({reason})"
            )

        summary["details"].append(detail)

    _last_scan_summary = summary
    logger.info(
        f"Pivot scan complete — "
        f"{summary['pivots_executed']} pivots, "
        f"{summary['stop_loss_only']} stop-losses, "
        f"{summary['no_drop']} holding"
    )
    return summary


def get_pivot_status() -> dict:
    """Return current pivot scanner state for the API."""
    return {
        "dry_run": DRY_RUN,
        "stop_loss_pct": STOP_LOSS_PCT,
        "pivot_min_momentum": PIVOT_MIN_MOMENTUM,
        "last_scan": _last_scan.isoformat() if _last_scan else None,
        "total_pivots_this_session": _total_pivots_this_session,
        "total_stop_losses_this_session": _total_stop_losses_this_session,
        "last_scan_summary": _last_scan_summary,
    }
