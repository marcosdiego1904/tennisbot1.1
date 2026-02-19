"""
Automation orchestrator — runs the full betting workflow automatically.

Cycle (every N minutes via scheduler.py):
  1. Fetch all open tennis markets from Kalshi
  2. Run the engine → get BUY signals with TARGET prices
  3. For each BUY signal:
     a. Skip if we already placed an order for this event (SQLite dedup)
     b. Query Matchstat for win probability confirmation
     c. If Matchstat confirms → place limit order on Kalshi
     d. Record the result in SQLite

Config via env vars:
  DRY_RUN=true                  Don't place real orders (default: true)
  CONTRACTS_PER_TRADE=50        Contracts per order (default: 50)
  MATCHSTAT_MIN_WIN_PCT=0.65    Min Matchstat win% to confirm (default: 65%)
"""

import os
import json
import logging
import aiosqlite
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.kalshi_client import fetch_tennis_markets
from app.tennis_data import load_tournament_db
from app.engine import analyze_all
from app.models import Signal
from app.matchstat_client import get_player_win_probability, confirms_signal
from app.kalshi_orders import place_limit_order, CONTRACTS_PER_TRADE

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "orders.db"

# DRY_RUN=true by default — set DRY_RUN=false in .env to place real orders
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() != "false"

# Global state (in-memory, reset on server restart)
_last_run: Optional[datetime] = None
_last_run_summary: dict = {}
_total_orders_this_session: int = 0


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

async def init_db():
    """Create the orders tracking table if it doesn't exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS placed_orders (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                event_ticker     TEXT    NOT NULL,
                ticker           TEXT    NOT NULL,
                player_fav       TEXT    NOT NULL,
                player_dog       TEXT    NOT NULL,
                tournament       TEXT,
                target_price     INTEGER NOT NULL,
                contracts        INTEGER NOT NULL,
                kalshi_price     INTEGER NOT NULL,
                matchstat_win_pct REAL,
                dry_run          INTEGER NOT NULL DEFAULT 1,
                status           TEXT    NOT NULL,
                placed_at        TEXT    NOT NULL,
                order_response   TEXT
            )
        """)
        await db.commit()


async def already_ordered(event_ticker: str) -> bool:
    """Return True if we already processed this event (any status)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM placed_orders WHERE event_ticker = ?",
            (event_ticker,),
        )
        return await cursor.fetchone() is not None


async def record_order(
    event_ticker: str,
    ticker: str,
    player_fav: str,
    player_dog: str,
    tournament: str,
    target_price: int,
    contracts: int,
    kalshi_price: int,
    matchstat_win_pct: Optional[float],
    dry_run: bool,
    status: str,
    order_response: str,
):
    """Persist an order (or rejection) to SQLite."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO placed_orders
                (event_ticker, ticker, player_fav, player_dog, tournament,
                 target_price, contracts, kalshi_price, matchstat_win_pct,
                 dry_run, status, placed_at, order_response)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_ticker, ticker, player_fav, player_dog, tournament,
                target_price, contracts, kalshi_price, matchstat_win_pct,
                1 if dry_run else 0, status,
                datetime.now(timezone.utc).isoformat(),
                order_response,
            ),
        )
        await db.commit()


async def get_all_orders() -> list[dict]:
    """Return all recorded orders (newest first, max 200 rows)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM placed_orders ORDER BY placed_at DESC LIMIT 200"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Main automation cycle
# ---------------------------------------------------------------------------

async def run_automation_cycle() -> dict:
    """
    Execute one complete automation cycle.
    Called by the scheduler every N minutes.
    Returns a summary dict of what happened.
    """
    global _last_run, _last_run_summary, _total_orders_this_session

    _last_run = datetime.now(timezone.utc)

    summary = {
        "started_at": _last_run.isoformat(),
        "dry_run": DRY_RUN,
        "markets_fetched": 0,
        "buy_signals": 0,
        "already_ordered": 0,
        "matchstat_checked": 0,
        "matchstat_confirmed": 0,
        "matchstat_rejected": 0,
        "orders_placed": 0,
        "orders_failed": 0,
        "skipped_no_ticker": 0,
        "details": [],
    }

    try:
        # Step 1: Fetch markets and run engine
        tournament_db = load_tournament_db()
        matches = await fetch_tennis_markets(tournament_db)
        summary["markets_fetched"] = len(matches)

        results = analyze_all(matches)
        buy_signals = [r for r in results if r.signal == Signal.BUY]
        summary["buy_signals"] = len(buy_signals)

        logger.info(
            f"Cycle: {len(matches)} markets → {len(buy_signals)} BUY signals"
        )

        # Step 2: Process each BUY signal
        for result in buy_signals:
            m = result.match
            event_ticker = m.kalshi_event_ticker or ""
            ticker = m.kalshi_ticker or ""
            target_cents = int(round(result.target_price * 100))

            detail = {
                "player_fav": m.player_fav.name,
                "player_dog": m.player_dog.name,
                "tournament": m.tournament_name,
                "kalshi_price": m.kalshi_price,
                "target_price": target_cents,
                "ticker": ticker,
                "event_ticker": event_ticker,
            }

            # Guard: must have valid tickers
            if not ticker or not event_ticker:
                detail["action"] = "skipped_no_ticker"
                summary["skipped_no_ticker"] += 1
                summary["details"].append(detail)
                continue

            # Guard: skip if already processed this event
            if await already_ordered(event_ticker):
                detail["action"] = "already_ordered"
                summary["already_ordered"] += 1
                summary["details"].append(detail)
                continue

            # Step 3: Confirm with Matchstat
            summary["matchstat_checked"] += 1
            win_pct = await get_player_win_probability(m.player_fav.name)
            detail["matchstat_win_pct"] = round(win_pct * 100, 1) if win_pct is not None else None
            matchstat_ok = confirms_signal(win_pct)
            detail["matchstat_confirmed"] = matchstat_ok

            if not matchstat_ok:
                detail["action"] = "matchstat_rejected"
                summary["matchstat_rejected"] += 1
                # Record the rejection so we don't re-check this event
                await record_order(
                    event_ticker=event_ticker, ticker=ticker,
                    player_fav=m.player_fav.name, player_dog=m.player_dog.name,
                    tournament=m.tournament_name, target_price=target_cents,
                    contracts=0, kalshi_price=m.kalshi_price,
                    matchstat_win_pct=win_pct, dry_run=DRY_RUN,
                    status="rejected_by_matchstat", order_response="",
                )
                summary["details"].append(detail)
                continue

            summary["matchstat_confirmed"] += 1

            # Step 4: Place the limit order
            order_result = await place_limit_order(
                ticker=ticker,
                yes_price=target_cents,
                count=CONTRACTS_PER_TRADE,
                dry_run=DRY_RUN,
            )

            order_status = order_result.get("status", "unknown")
            detail["action"] = f"order_{order_status}"
            detail["order_result"] = order_result

            if order_status in ("placed", "simulated"):
                summary["orders_placed"] += 1
                _total_orders_this_session += 1
            else:
                summary["orders_failed"] += 1

            # Record in SQLite
            await record_order(
                event_ticker=event_ticker, ticker=ticker,
                player_fav=m.player_fav.name, player_dog=m.player_dog.name,
                tournament=m.tournament_name, target_price=target_cents,
                contracts=CONTRACTS_PER_TRADE, kalshi_price=m.kalshi_price,
                matchstat_win_pct=win_pct, dry_run=DRY_RUN,
                status=order_status, order_response=json.dumps(order_result),
            )

            summary["details"].append(detail)

    except Exception as e:
        logger.error(f"Automation cycle error: {e}", exc_info=True)
        summary["error"] = str(e)

    _last_run_summary = summary
    logger.info(
        f"Cycle complete — {summary['orders_placed']} orders"
        f" ({summary['matchstat_confirmed']} Matchstat confirmed)"
        f" | dry_run={DRY_RUN}"
    )
    return summary


def get_status() -> dict:
    """Return current automation state for the status API endpoint."""
    return {
        "dry_run": DRY_RUN,
        "last_run": _last_run.isoformat() if _last_run else None,
        "total_orders_this_session": _total_orders_this_session,
        "last_run_summary": _last_run_summary,
        "config": {
            "contracts_per_trade": CONTRACTS_PER_TRADE,
            "matchstat_min_win_pct": float(os.getenv("MATCHSTAT_MIN_WIN_PCT", "0.65")),
            "matchstat_api_configured": bool(os.getenv("MATCHSTAT_API_KEY")),
        },
    }
