#!/usr/bin/env python3
"""
Kalshi Auto-Sell Bot — Tri-Mode: Favorite + Longshot + Pivot
=============================================================
Automatically detects whether each position is a FAVORITE (avg_buy ≥ 30¢)
or a LONGSHOT (avg_buy < 30¢) and applies a completely different exit
strategy for each.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MODE A — FAVORITE  (avg_buy ≥ LONGSHOT_THRESHOLD, default 30¢)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Take-Profit (% based — scales with entry price):
    TP1 +25%  → sell 30% of initial
    TP2 +50%  → sell 40% of initial  (after TP1)
    TP3 +75% or price ≥ 92¢  → sell ALL remaining

  Stop-Loss (three layers):
    Trailing  — once peak profit ≥ $2 or $4, a floor is set above entry
    Hard      — profit ≤ −35%  (= price ≤ avg_buy × 0.65)  → sell ALL
    Soft      — profit ≤ −20%  → sell 50% (once only)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MODE B — LONGSHOT  (avg_buy < LONGSHOT_THRESHOLD, default 30¢)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  A longshot is bought because the market mispriced the probability.
  When the price reaches ~57¢ (the coin-flip zone), the mispricing is
  gone — the original edge no longer exists. Sell everything and redeploy.

  Partial exits are avoided intentionally: they cut the best positions
  while they're running and add complexity for no real benefit.

  Take-Profit (single clean exit):
    price ≥ LS_EXIT_PRICE (default 57¢)  →  sell ALL  (+~280% on 15¢ entry)

  Stop-Loss (single hard stop — user accepts the variance):
    Hard  — profit ≤ −60%  → sell ALL remaining
    (No soft stop, no trailing: longshots are volatile by nature)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MODE C — PIVOT TRADE  (after SL exit on favorite)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  When a favorite position hits hard/soft SL AND the underdog is actually
  winning (confirmed via live score data), the bot can buy the underdog
  with a fraction of the recovered capital.

  Conditions (ALL must be true):
    1. Hard or soft SL just triggered on a favorite position
    2. Live score confirms underdog is winning (momentum_score ≥ 2)
    3. Underdog price is in value zone (25¢–50¢)
    4. Momentum: favorite price dropped ≥ 25% from peak

  Sizing: 50–60% of recovered capital (scales with underdog price)
  Exit:   Treated as LONGSHOT (exit at 57¢, hard SL at -40%)
  Limit:  Max 1 pivot per event (no double pivots)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SHARED MECHANICS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  • Mode is detected automatically from avg_buy each scan.
  • All state (TP levels hit, peak bid) persisted in SQLite — restart-safe.
  • Every scan updates max_bid_seen for trailing-stop calculation.
  • DRY_RUN=true simulates all actions without placing real orders.

CONFIGURATION  (see .env.example for the full list)
  KALSHI_API_KEY / KALSHI_API_SECRET   Required
  LONGSHOT_THRESHOLD                   ¢ below which longshot mode is used (default: 30)
  DRY_RUN                              true = simulate (default: true)
  POLL_INTERVAL                        seconds between scans (default: 10)
  PIVOT_ENABLED                        true = enable pivot trades (default: false)
  TENNISAPI_KEY                        RapidAPI key for live scores (required for pivot)

USAGE
  cp .env.example .env   # fill in credentials + tune parameters
  python bot.py
  nohup python bot.py >> logs/bot.log 2>&1 &
"""

import os
import sys
import time
import uuid
import sqlite3
import base64
import logging
import datetime
import asyncio
import httpx
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ── Load .env if present ──────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL      = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
API_KEY       = os.getenv("KALSHI_API_KEY", "")
API_SECRET    = os.getenv("KALSHI_API_SECRET", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))
DRY_RUN       = os.getenv("DRY_RUN", "true").lower() == "true"
DB_PATH       = os.getenv("DB_PATH", "data/orders.db")

# Mode detection threshold (¢): below this → LONGSHOT mode
LONGSHOT_THRESHOLD = int(os.getenv("LONGSHOT_THRESHOLD", "30"))

# ── FAVORITE mode config ──────────────────────────────────────────────────────
# Take-profit (% based)
TP1_PROFIT_PCT   = float(os.getenv("TP1_PROFIT_PCT",   "25"))
TP1_SELL_RATIO   = float(os.getenv("TP1_SELL_RATIO",   "0.30"))
TP2_PROFIT_PCT   = float(os.getenv("TP2_PROFIT_PCT",   "50"))
TP2_SELL_RATIO   = float(os.getenv("TP2_SELL_RATIO",   "0.40"))
TP3_PROFIT_PCT   = float(os.getenv("TP3_PROFIT_PCT",   "75"))
TP3_PRICE_TARGET = int(os.getenv(  "TP3_PRICE_TARGET", "92"))   # ¢

# Stop-loss: soft + hard + trailing
SOFT_SL_PCT          = float(os.getenv("SOFT_SL_PCT",          "-20"))
SOFT_SL_RATIO        = float(os.getenv("SOFT_SL_RATIO",        "0.50"))
HARD_SL_PCT          = float(os.getenv("HARD_SL_PCT",          "-35"))
TRAIL_SL_THRESHOLD_1 = float(os.getenv("TRAIL_SL_THRESHOLD_1", "2.0"))  # $
TRAIL_SL_RATIO_1     = float(os.getenv("TRAIL_SL_RATIO_1",     "0.30"))
TRAIL_SL_THRESHOLD_2 = float(os.getenv("TRAIL_SL_THRESHOLD_2", "4.0"))  # $
TRAIL_SL_RATIO_2     = float(os.getenv("TRAIL_SL_RATIO_2",     "0.50"))

# ── LONGSHOT mode config ──────────────────────────────────────────────────────
# Single clean exit: once the longshot reaches the coin-flip zone (≈55-60¢),
# the mispricing that justified the bet is gone — sell everything and move on.
LS_EXIT_PRICE  = int(os.getenv(  "LS_EXIT_PRICE",  "57"))   # ¢ — sell 100% here
LS_HARD_SL_PCT = float(os.getenv("LS_HARD_SL_PCT", "-60"))  # accept big loss

# ── PIVOT TRADE config ──────────────────────────────────────────────────────
# After a favorite SL fires, optionally buy the underdog if live score confirms
# the underdog is actually winning. Requires TENNISAPI_KEY for live scores.
PIVOT_ENABLED         = os.getenv("PIVOT_ENABLED", "false").lower() == "true"
PIVOT_CAPITAL_RATIO   = float(os.getenv("PIVOT_CAPITAL_RATIO",   "0.60"))  # % of recovered capital
PIVOT_MIN_DOG_PRICE   = int(os.getenv(  "PIVOT_MIN_DOG_PRICE",  "25"))    # ¢ — too cheap = too risky
PIVOT_MAX_DOG_PRICE   = int(os.getenv(  "PIVOT_MAX_DOG_PRICE",  "50"))    # ¢ — above 50 = no value
PIVOT_MIN_MOMENTUM    = float(os.getenv("PIVOT_MIN_MOMENTUM",   "0.25"))  # 25% drop from peak
PIVOT_MIN_SCORE_MOMENTUM = int(os.getenv("PIVOT_MIN_SCORE_MOMENTUM", "2")) # momentum_score ≥ 2
PIVOT_HARD_SL_PCT     = float(os.getenv("PIVOT_HARD_SL_PCT",    "-35"))   # tighter than longshot
PIVOT_EXIT_PRICE      = int(os.getenv(  "PIVOT_EXIT_PRICE",     "57"))    # ¢ — same as longshot exit

# ── Logging ───────────────────────────────────────────────────────────────────
Path("logs").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("kalshi-bot")

# ── RSA Authentication ────────────────────────────────────────────────────────
_private_key = None


def _load_key():
    global _private_key
    if _private_key is not None:
        return _private_key
    if not API_SECRET:
        raise RuntimeError("KALSHI_API_SECRET is not set.")
    pem = API_SECRET.replace("\\n", "\n").encode()
    _private_key = serialization.load_pem_private_key(pem, password=None)
    return _private_key


def _auth_headers(method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    message = f"{ts}{method}{path.split('?')[0]}".encode()
    sig = _load_key().sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": API_KEY,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }


# ── HTTP Helpers ──────────────────────────────────────────────────────────────
def _get(client: httpx.Client, path: str, params: dict = None) -> dict:
    url  = f"{BASE_URL}{path}"
    resp = client.get(url, headers=_auth_headers("GET", f"/trade-api/v2{path}"),
                      params=params, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


def _post(client: httpx.Client, path: str, body: dict) -> dict:
    url  = f"{BASE_URL}{path}"
    resp = client.post(url, headers=_auth_headers("POST", f"/trade-api/v2{path}"),
                       json=body, timeout=15.0)
    if not resp.is_success:
        raise httpx.HTTPStatusError(
            f"{resp.status_code} {resp.text}", request=resp.request, response=resp
        )
    return resp.json()


# ── Kalshi API Calls ──────────────────────────────────────────────────────────
def get_open_positions(client: httpx.Client) -> list[dict]:
    data = _get(client, "/portfolio/positions")
    return [p for p in data.get("market_positions", []) if (p.get("position") or 0) > 0]


def get_avg_buy_price(client: httpx.Client, ticker: str) -> float | None:
    """Weighted average YES buy price in cents, or None."""
    fills, cursor = [], None
    for _ in range(5):
        params = {"ticker": ticker, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        try:
            data = _get(client, "/portfolio/fills", params=params)
        except Exception as e:
            log.warning(f"    Could not read fills for {ticker}: {e}")
            return None
        page = data.get("fills", [])
        fills.extend(page)
        cursor = data.get("cursor")
        if not cursor or not page:
            break

    buy_fills = [f for f in fills if f.get("action") == "buy" and f.get("side") == "yes"]
    if not buy_fills:
        log.warning(f"    No YES buy fills found for {ticker}.")
        return None

    total_qty  = sum(f.get("count", 0) for f in buy_fills)
    total_cost = sum(
        f.get("count", 0) * (f.get("yes_price") or f.get("price") or 0)
        for f in buy_fills
    )
    return total_cost / total_qty if total_qty else None


def get_market_prices(client: httpx.Client, ticker: str) -> tuple[int | None, int | None]:
    """
    Returns (ref_price, yes_bid) in whole cents, or (None, None).

    ref_price  — last traded price; used for P&L calculation and stop-loss
                 decisions so that wide bid/ask spreads in live in-play
                 markets do not trigger false exits.
    yes_bid    — current best bid; used as the floor price in sell orders
                 so that the order actually fills at market.

    In pre-match markets the spread is tight so both values are nearly
    identical.  In live in-play markets the spread can be 30-50 ¢ wide:
    comparing avg_buy (paid at ask) against yes_bid would show a large
    artificial loss and incorrectly fire a stop-loss.
    """
    try:
        market    = _get(client, f"/markets/{ticker}").get("market", {})
        last_p    = market.get("last_price")
        bid_p     = market.get("yes_bid")
        yes_bid   = int(bid_p)  if bid_p  and int(bid_p)  > 0 else None
        ref_price = int(last_p) if last_p and int(last_p) > 0 else yes_bid
        return ref_price, yes_bid
    except Exception as e:
        log.warning(f"    Could not read market data for {ticker}: {e}")
        return None, None


def sell_position(client: httpx.Client, ticker: str, count: int, yes_price: int) -> dict:
    """Market sell. yes_price (current bid) required by Kalshi even on market orders."""
    return _post(client, "/portfolio/orders", {
        "action": "sell",
        "type": "market",
        "ticker": ticker,
        "count": count,
        "side": "yes",
        "yes_price": max(1, yes_price),
        "client_order_id": str(uuid.uuid4()),
    })


# ── State Persistence (SQLite) ────────────────────────────────────────────────
def _init_state_table():
    Path("data").mkdir(exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tp_state (
                ticker          TEXT PRIMARY KEY,
                initial_count   INTEGER NOT NULL,
                tp1_done        INTEGER NOT NULL DEFAULT 0,
                tp2_done        INTEGER NOT NULL DEFAULT 0,
                soft_stop_done  INTEGER NOT NULL DEFAULT 0,
                max_bid_seen    INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            )
        """)
        for col, default in [("soft_stop_done", 0), ("max_bid_seen", 0)]:
            try:
                conn.execute(
                    f"ALTER TABLE tp_state ADD COLUMN {col} INTEGER NOT NULL DEFAULT {default}"
                )
            except sqlite3.OperationalError:
                pass  # column already exists

        # Pivot trade tracking: records completed pivots to enforce max 1 per event
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pivot_trades (
                event_ticker    TEXT PRIMARY KEY,
                fav_ticker      TEXT NOT NULL,
                dog_ticker      TEXT NOT NULL,
                fav_sell_price  INTEGER NOT NULL,
                dog_buy_price   INTEGER NOT NULL,
                contracts       INTEGER NOT NULL,
                capital_used    REAL NOT NULL,
                created_at      TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # Default to enabled on first run
        conn.execute(
            "INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('bot_enabled', 'true')"
        )
        conn.commit()


def _is_bot_enabled() -> bool:
    """Check the bot_enabled flag in SQLite. Returns True if the bot should scan."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT value FROM bot_settings WHERE key='bot_enabled'"
            ).fetchone()
            return row is None or row[0] == "true"
    except Exception:
        return True  # default to enabled on DB error


def _load_state(ticker: str, current_count: int) -> dict:
    now = datetime.datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT initial_count, tp1_done, tp2_done, soft_stop_done, max_bid_seen "
            "FROM tp_state WHERE ticker=?", (ticker,)
        ).fetchone()
        if row:
            return {
                "initial": row[0], "tp1_done": bool(row[1]), "tp2_done": bool(row[2]),
                "soft_stop_done": bool(row[3]), "max_bid_seen": row[4],
            }
        conn.execute(
            "INSERT INTO tp_state "
            "(ticker, initial_count, tp1_done, tp2_done, soft_stop_done, max_bid_seen, created_at, updated_at) "
            "VALUES (?, ?, 0, 0, 0, 0, ?, ?)",
            (ticker, current_count, now, now),
        )
    log.info(f"    New position tracked — initial_count={current_count}")
    return {"initial": current_count, "tp1_done": False, "tp2_done": False,
            "soft_stop_done": False, "max_bid_seen": 0}


def _save_state(ticker: str, state: dict):
    now = datetime.datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE tp_state "
            "SET tp1_done=?, tp2_done=?, soft_stop_done=?, max_bid_seen=?, updated_at=? "
            "WHERE ticker=?",
            (int(state["tp1_done"]), int(state["tp2_done"]),
             int(state["soft_stop_done"]), int(state["max_bid_seen"]), now, ticker),
        )


def _clear_state(ticker: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM tp_state WHERE ticker=?", (ticker,))


# ── Trailing Stop Calculator (Favorite mode only) ─────────────────────────────
def _calc_trailing_sl(avg_buy: float, max_bid_seen: int, initial_count: int) -> float | None:
    """
    Returns trailing SL floor in cents, or None if not yet active.
    Uses initial_count so thresholds stay consistent after partial TP sells.
    """
    peak_gain = max_bid_seen - avg_buy
    if peak_gain <= 0:
        return None
    peak_profit_dollars = (peak_gain / 100.0) * initial_count
    if peak_profit_dollars >= TRAIL_SL_THRESHOLD_2:
        return avg_buy + peak_gain * TRAIL_SL_RATIO_2
    if peak_profit_dollars >= TRAIL_SL_THRESHOLD_1:
        return avg_buy + peak_gain * TRAIL_SL_RATIO_1
    return None


# ── Sell Helper ───────────────────────────────────────────────────────────────
def _execute_sell(
    client: httpx.Client, ticker: str, count: int,
    bid: int, profit_pct: float, label: str,
) -> bool:
    if DRY_RUN:
        log.info(
            f"    [DRY RUN] {label} — Would sell {count} contract(s) "
            f"at ~{bid}¢  (profit {profit_pct:+.1f}%)"
        )
        return True
    log.info(f"    {label} — Selling {count} contract(s) at ~{bid}¢  (profit {profit_pct:+.1f}%)...")
    try:
        log.info(f"    SOLD OK: {sell_position(client, ticker, count, yes_price=bid)}")
        return True
    except Exception as e:
        log.error(f"    SELL FAILED: {e}")
        return False


# ── Mode-specific evaluators ──────────────────────────────────────────────────
def _evaluate_favorite(
    client: httpx.Client, ticker: str, count: int,
    bid: int, sell_bid: int, avg_buy: float, profit_pct: float, state: dict,
):
    """
    Exit logic for FAVORITE positions (avg_buy ≥ LONGSHOT_THRESHOLD).
    Priority: Trailing Stop → Hard Stop → Soft Stop → TP3 → TP2 → TP1 → Hold

    bid      — ref price (last_price) used for all P&L / threshold comparisons.
    sell_bid — actual yes_bid used as floor in sell orders so they fill at market.
    """
    initial        = state["initial"]
    tp1_done       = state["tp1_done"]
    tp2_done       = state["tp2_done"]
    soft_stop_done = state["soft_stop_done"]
    max_bid_seen   = state["max_bid_seen"]

    hard_sl_price = avg_buy * (1.0 + HARD_SL_PCT / 100.0)
    trailing_sl   = _calc_trailing_sl(avg_buy, max_bid_seen, initial)
    trail_str     = f"{trailing_sl:.1f}¢" if trailing_sl is not None else "—"
    spread_str    = f"  spread={bid - sell_bid}¢" if sell_bid != bid else ""

    log.info(
        f"    [FAVORITE]  avg_buy={avg_buy:.1f}¢  ref={bid}¢  sell_bid={sell_bid}¢{spread_str}  "
        f"profit={profit_pct:+.1f}%  peak={max_bid_seen}¢  trail_sl={trail_str}  "
        f"hard_sl={hard_sl_price:.1f}¢  "
        f"tp1={'✓' if tp1_done else '○'}  tp2={'✓' if tp2_done else '○'}  "
        f"soft={'✓' if soft_stop_done else '○'}"
    )

    # (1) Trailing stop
    if trailing_sl is not None and bid < trailing_sl:
        _execute_sell(client, ticker, count, sell_bid, profit_pct,
            f"TRAILING STOP (ref {bid}¢ < floor {trailing_sl:.1f}¢, peak {max_bid_seen}¢) — sell ALL {count}")
        _clear_state(ticker)
        return

    # (2) Hard stop — dynamic floor at avg_buy × 0.65
    if profit_pct <= HARD_SL_PCT:
        _execute_sell(client, ticker, count, sell_bid, profit_pct,
            f"HARD STOP (profit {profit_pct:.1f}% ≤ {HARD_SL_PCT:.0f}%, floor {hard_sl_price:.1f}¢) — sell ALL {count}")
        _clear_state(ticker)
        # Evaluate pivot trade opportunity
        _evaluate_pivot(client, ticker, count, sell_bid, avg_buy, max_bid_seen)
        return

    # (3) Soft stop — partial exit
    if profit_pct <= SOFT_SL_PCT and not soft_stop_done:
        qty = max(1, round(count * SOFT_SL_RATIO))
        if _execute_sell(client, ticker, qty, sell_bid, profit_pct,
                f"SOFT STOP (profit {profit_pct:.1f}% ≤ {SOFT_SL_PCT:.0f}%) — sell {qty} of {count} ({SOFT_SL_RATIO:.0%})"):
            state["soft_stop_done"] = True
            _save_state(ticker, state)
        return

    # (4) TP3 — sell ALL remaining
    if profit_pct >= TP3_PROFIT_PCT or bid >= TP3_PRICE_TARGET:
        _execute_sell(client, ticker, count, sell_bid, profit_pct,
            f"TP3 — sell remaining {count} (profit {profit_pct:+.1f}% | ref {bid}¢)")
        _clear_state(ticker)
        return

    # (5) TP2 — sell 40% of initial (after TP1)
    if profit_pct >= TP2_PROFIT_PCT and tp1_done and not tp2_done:
        qty = max(1, min(round(initial * TP2_SELL_RATIO), count))
        if _execute_sell(client, ticker, qty, sell_bid, profit_pct,
                f"TP2 — sell {qty} of {count} ({TP2_SELL_RATIO:.0%} of initial {initial})"):
            state["tp2_done"] = True
            _save_state(ticker, state)
        return

    # (6) TP1 — sell 30% of initial
    if profit_pct >= TP1_PROFIT_PCT and not tp1_done:
        qty = max(1, min(round(initial * TP1_SELL_RATIO), count))
        if _execute_sell(client, ticker, qty, sell_bid, profit_pct,
                f"TP1 — sell {qty} of {count} ({TP1_SELL_RATIO:.0%} of initial {initial})"):
            state["tp1_done"] = True
            _save_state(ticker, state)
        return

    # (7) Hold
    if profit_pct < SOFT_SL_PCT:
        next_label = f"HARD STOP at {HARD_SL_PCT:.0f}% (floor {hard_sl_price:.1f}¢)"
    elif not tp1_done:
        next_label = f"TP1 at +{TP1_PROFIT_PCT:.0f}%"
    elif not tp2_done:
        next_label = f"TP2 at +{TP2_PROFIT_PCT:.0f}%"
    else:
        next_label = f"TP3 at +{TP3_PROFIT_PCT:.0f}% or {TP3_PRICE_TARGET}¢"
    log.info(f"    Holding — next trigger: {next_label}")


def _evaluate_longshot(
    client: httpx.Client, ticker: str, count: int,
    bid: int, sell_bid: int, avg_buy: float, profit_pct: float, state: dict,
):
    """
    Exit logic for LONGSHOT positions (avg_buy < LONGSHOT_THRESHOLD).

    Single clean exit at LS_EXIT_PRICE (default 57¢): once the price reaches
    the coin-flip zone the original edge is gone — sell everything and move on.
    Partial exits are skipped: they cut the best positions while running.

    bid      — ref price (last_price) used for all P&L / threshold comparisons.
    sell_bid — actual yes_bid used as floor in sell orders so they fill at market.

    Priority: Hard Stop → Exit TP → Hold
    """
    hard_sl_price = avg_buy * (1.0 + LS_HARD_SL_PCT / 100.0)
    spread_str    = f"  spread={bid - sell_bid}¢" if sell_bid != bid else ""

    log.info(
        f"    [LONGSHOT]  avg_buy={avg_buy:.1f}¢  ref={bid}¢  sell_bid={sell_bid}¢{spread_str}  "
        f"profit={profit_pct:+.1f}%  exit_target={LS_EXIT_PRICE}¢  "
        f"hard_sl={hard_sl_price:.1f}¢ ({LS_HARD_SL_PCT:.0f}%)"
    )

    # (1) Hard stop — accept big loss, recover what's left
    if profit_pct <= LS_HARD_SL_PCT:
        _execute_sell(client, ticker, count, sell_bid, profit_pct,
            f"LONGSHOT HARD STOP (profit {profit_pct:.1f}% ≤ {LS_HARD_SL_PCT:.0f}%, "
            f"floor {hard_sl_price:.1f}¢) — sell ALL {count}")
        _clear_state(ticker)
        return

    # (2) Exit TP — coin-flip zone reached, edge is gone, sell everything
    if bid >= LS_EXIT_PRICE:
        _execute_sell(client, ticker, count, sell_bid, profit_pct,
            f"LONGSHOT EXIT (ref {bid}¢ ≥ {LS_EXIT_PRICE}¢) — sell ALL {count}")
        _clear_state(ticker)
        return

    # (3) Hold — waiting for the move
    log.info(
        f"    Holding (longshot) — exit at {LS_EXIT_PRICE}¢  "
        f"(hard SL at {LS_HARD_SL_PCT:.0f}%, floor {hard_sl_price:.1f}¢)"
    )


# ── Pivot Trade Logic ────────────────────────────────────────────────────────

def _get_event_ticker(client: httpx.Client, ticker: str) -> str | None:
    """Extract event_ticker from a market ticker via Kalshi API."""
    try:
        market = _get(client, f"/markets/{ticker}").get("market", {})
        return market.get("event_ticker")
    except Exception as e:
        log.warning(f"    Could not get event_ticker for {ticker}: {e}")
        return None


def _get_sibling_ticker(client: httpx.Client, event_ticker: str, exclude_ticker: str) -> tuple[str | None, int | None]:
    """
    Find the other market in the same event (the underdog's market).
    Returns (ticker, yes_ask_price) or (None, None).
    """
    try:
        data = _get(client, f"/events/{event_ticker}")
        markets = data.get("event", {}).get("markets", [])
        if not markets:
            # Try fetching markets for this event
            data = _get(client, "/markets", params={"event_ticker": event_ticker, "limit": 10})
            markets = data.get("markets", [])

        for m in markets:
            t = m.get("ticker", "")
            if t and t != exclude_ticker:
                # Get current price of the underdog market
                price = m.get("last_price") or m.get("yes_ask") or m.get("yes_bid")
                return t, int(price) if price else None

        return None, None
    except Exception as e:
        log.warning(f"    Could not find sibling market for event {event_ticker}: {e}")
        return None, None


def _extract_players_from_ticker(ticker: str) -> tuple[str, str]:
    """
    Best-effort extraction of player names from Kalshi ticker.
    Tickers look like: KXATPMATCH-25FEB-T-SINNER
    The last part is the YES player's last name.
    """
    parts = ticker.upper().split("-")
    if len(parts) >= 4:
        return parts[-1].title(), ""  # Return the YES player name
    return "", ""


def _already_pivoted(event_ticker: str) -> bool:
    """Check if we already did a pivot trade for this event."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT event_ticker FROM pivot_trades WHERE event_ticker=?",
                (event_ticker,),
            ).fetchone()
            return row is not None
    except Exception:
        return False


def _record_pivot(event_ticker: str, fav_ticker: str, dog_ticker: str,
                  fav_sell_price: int, dog_buy_price: int, contracts: int, capital: float):
    """Record a pivot trade in the database."""
    now = datetime.datetime.utcnow().isoformat()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO pivot_trades "
                "(event_ticker, fav_ticker, dog_ticker, fav_sell_price, dog_buy_price, "
                "contracts, capital_used, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (event_ticker, fav_ticker, dog_ticker, fav_sell_price, dog_buy_price,
                 contracts, capital, now),
            )
    except Exception as e:
        log.error(f"    Failed to record pivot trade: {e}")


def _buy_position(client: httpx.Client, ticker: str, count: int, yes_price: int) -> dict:
    """Place a limit buy order for the pivot trade."""
    return _post(client, "/portfolio/orders", {
        "action": "buy",
        "type": "limit",
        "ticker": ticker,
        "count": count,
        "side": "yes",
        "yes_price": max(1, yes_price),
        "client_order_id": str(uuid.uuid4()),
    })


def _evaluate_pivot(
    client: httpx.Client, fav_ticker: str, count_sold: int,
    sell_price: int, avg_buy: float, max_bid_seen: int,
):
    """
    Evaluate whether to execute a pivot trade after a favorite SL fires.

    Conditions:
      1. Pivot is enabled (PIVOT_ENABLED=true)
      2. Live score confirms underdog is winning (momentum_score ≥ PIVOT_MIN_SCORE_MOMENTUM)
      3. Underdog price is in value zone (PIVOT_MIN_DOG_PRICE–PIVOT_MAX_DOG_PRICE)
      4. Price momentum: favorite dropped ≥ PIVOT_MIN_MOMENTUM from peak
      5. No previous pivot for this event
    """
    if not PIVOT_ENABLED:
        return

    # Check momentum from price (no score data needed for this)
    if max_bid_seen <= 0:
        return
    momentum_pct = (max_bid_seen - sell_price) / max_bid_seen
    if momentum_pct < PIVOT_MIN_MOMENTUM:
        log.info(f"    [PIVOT] Momentum too low: {momentum_pct:.1%} < {PIVOT_MIN_MOMENTUM:.0%} required")
        return

    # Find the event and sibling (underdog) market
    event_ticker = _get_event_ticker(client, fav_ticker)
    if not event_ticker:
        log.info("    [PIVOT] Could not determine event_ticker — skip pivot")
        return

    if _already_pivoted(event_ticker):
        log.info(f"    [PIVOT] Already pivoted on event {event_ticker} — no double pivots")
        return

    dog_ticker, dog_price = _get_sibling_ticker(client, event_ticker, fav_ticker)
    if not dog_ticker or not dog_price:
        log.info("    [PIVOT] Could not find underdog market — skip pivot")
        return

    # Check underdog price is in value zone
    if dog_price < PIVOT_MIN_DOG_PRICE or dog_price > PIVOT_MAX_DOG_PRICE:
        log.info(
            f"    [PIVOT] Underdog price {dog_price}¢ outside value zone "
            f"({PIVOT_MIN_DOG_PRICE}–{PIVOT_MAX_DOG_PRICE}¢) — skip pivot"
        )
        return

    # Check live score (the key differentiator for EV+)
    try:
        from app.live_scores import find_live_score
        # Extract player names — best effort from ticker
        fav_name, _ = _extract_players_from_ticker(fav_ticker)
        dog_name, _ = _extract_players_from_ticker(dog_ticker)

        if fav_name and dog_name:
            result = asyncio.run(find_live_score(fav_name, dog_name))
            if result:
                score, fav_is_home = result
                momentum = score.momentum_score(fav_is_home)
                log.info(
                    f"    [PIVOT] Live score: {score.home_player} {score.home_sets}-{score.away_sets} "
                    f"{score.away_player} (set {score.current_set}, games {score.home_games}-{score.away_games}) "
                    f"momentum_score={momentum}"
                )
                if momentum < PIVOT_MIN_SCORE_MOMENTUM:
                    log.info(
                        f"    [PIVOT] Score momentum {momentum} < {PIVOT_MIN_SCORE_MOMENTUM} required "
                        f"— underdog not clearly winning, skip pivot"
                    )
                    return
            else:
                log.info("    [PIVOT] No live score found — proceeding on price momentum only")
                # Without score data, require stronger price momentum
                if momentum_pct < 0.35:
                    log.info(f"    [PIVOT] No score data + momentum only {momentum_pct:.1%} < 35% — skip")
                    return
        else:
            log.info("    [PIVOT] Could not extract player names from tickers — using price momentum only")
            if momentum_pct < 0.35:
                log.info(f"    [PIVOT] No player names + momentum only {momentum_pct:.1%} < 35% — skip")
                return
    except ImportError:
        log.warning("    [PIVOT] app.live_scores not available — using price momentum only")
        if momentum_pct < 0.35:
            return
    except Exception as e:
        log.warning(f"    [PIVOT] Live score check failed: {e} — using price momentum only")
        if momentum_pct < 0.35:
            return

    # Calculate pivot sizing
    capital_recovered = (sell_price / 100.0) * count_sold

    # Scale ratio by underdog price (cheaper = riskier = less capital)
    if dog_price <= 30:
        ratio = 0.50
    elif dog_price <= 40:
        ratio = PIVOT_CAPITAL_RATIO  # default 0.60
    else:
        ratio = 0.45

    capital_pivot = capital_recovered * ratio
    contracts_pivot = int(capital_pivot / (dog_price / 100.0))

    if contracts_pivot < 1:
        log.info(f"    [PIVOT] Not enough capital for even 1 contract — skip")
        return

    reserve = capital_recovered - capital_pivot

    log.info(
        f"    [PIVOT] ═══ PIVOT TRADE SIGNAL ═══\n"
        f"    [PIVOT] Favorite sold: {fav_ticker} at {sell_price}¢ ({count_sold} contracts)\n"
        f"    [PIVOT] Capital recovered: ${capital_recovered:.2f}\n"
        f"    [PIVOT] Underdog: {dog_ticker} at {dog_price}¢\n"
        f"    [PIVOT] Momentum: {momentum_pct:.1%} drop from peak {max_bid_seen}¢\n"
        f"    [PIVOT] Sizing: {ratio:.0%} of recovered = ${capital_pivot:.2f} → {contracts_pivot} contracts\n"
        f"    [PIVOT] Reserve (safe): ${reserve:.2f} ({1-ratio:.0%})\n"
        f"    [PIVOT] Exit plan: TP at {PIVOT_EXIT_PRICE}¢ | SL at {PIVOT_HARD_SL_PCT:.0f}%"
    )

    if DRY_RUN:
        log.info(
            f"    [DRY RUN] [PIVOT] Would BUY {contracts_pivot} contracts of {dog_ticker} "
            f"at {dog_price}¢ (${capital_pivot:.2f})"
        )
        _record_pivot(event_ticker, fav_ticker, dog_ticker, sell_price, dog_price,
                      contracts_pivot, capital_pivot)
        return

    # Execute the pivot buy
    try:
        log.info(f"    [PIVOT] Placing BUY order: {contracts_pivot}x {dog_ticker} @ {dog_price}¢...")
        result = _buy_position(client, dog_ticker, contracts_pivot, dog_price)
        log.info(f"    [PIVOT] BUY OK: {result}")
        _record_pivot(event_ticker, fav_ticker, dog_ticker, sell_price, dog_price,
                      contracts_pivot, capital_pivot)
    except Exception as e:
        log.error(f"    [PIVOT] BUY FAILED: {e}")


# ── Main Scan Logic ───────────────────────────────────────────────────────────
def run_scan(client: httpx.Client):
    """
    One full scan cycle:
    1. Fetch open YES positions.
    2. For each: get avg_buy + current bid, update peak bid.
    3. Detect mode (FAVORITE vs LONGSHOT) from avg_buy.
    4. Delegate to the appropriate evaluator.
    """
    try:
        positions = get_open_positions(client)
    except Exception as e:
        log.error(f"Failed to fetch positions: {e}")
        return

    if not positions:
        log.info("No open YES positions found.")
        return

    log.info(f"Found {len(positions)} open position(s).")

    for pos in positions:
        ticker = pos.get("ticker", "")
        count  = pos.get("position", 0)

        log.info(f"  [{ticker}]  {count} contract(s)")

        avg_buy              = get_avg_buy_price(client, ticker)
        ref_price, yes_bid   = get_market_prices(client, ticker)

        if avg_buy is None:
            log.warning("    Skipping — could not determine average buy price.")
            continue
        if ref_price is None:
            log.warning("    Skipping — no market price available (market may be illiquid).")
            continue

        # Use last_price (ref_price) for P&L decisions; yes_bid for sell orders.
        # In live in-play markets the bid/ask spread can be 30-50 ¢ wide:
        # comparing avg_buy (paid at ask) against yes_bid would produce a large
        # artificial loss and incorrectly trigger a stop-loss.
        sell_bid   = yes_bid if yes_bid is not None else ref_price
        profit_pct = ((ref_price - avg_buy) / avg_buy) * 100

        # Load state and refresh peak ref_price
        state = _load_state(ticker, count)
        if ref_price > state["max_bid_seen"]:
            state["max_bid_seen"] = ref_price
            _save_state(ticker, state)

        # Route to the correct exit strategy
        if avg_buy < LONGSHOT_THRESHOLD:
            _evaluate_longshot(client, ticker, count, ref_price, sell_bid, avg_buy, profit_pct, state)
        else:
            _evaluate_favorite(client, ticker, count, ref_price, sell_bid, avg_buy, profit_pct, state)


# ── Entry Point ───────────────────────────────────────────────────────────────
def main():
    if not API_KEY or not API_SECRET:
        log.error("KALSHI_API_KEY and KALSHI_API_SECRET must be set in your environment.")
        sys.exit(1)

    _init_state_table()

    mode = "DRY RUN (no real orders)" if DRY_RUN else "LIVE (real orders will be placed)"

    log.info("=" * 72)
    log.info("  Kalshi Auto-Sell Bot — Tri-Mode: Favorite + Longshot + Pivot")
    log.info(f"  Mode          : {mode}")
    log.info(f"  Poll interval : {POLL_INTERVAL}s")
    log.info(f"  Mode split    : avg_buy < {LONGSHOT_THRESHOLD}¢ → LONGSHOT  |  ≥ {LONGSHOT_THRESHOLD}¢ → FAVORITE")
    log.info("─" * 72)
    log.info("  FAVORITE (% based)")
    log.info(f"    TP1 +{TP1_PROFIT_PCT:.0f}% → sell {TP1_SELL_RATIO:.0%}   "
             f"TP2 +{TP2_PROFIT_PCT:.0f}% → sell {TP2_SELL_RATIO:.0%}   "
             f"TP3 +{TP3_PROFIT_PCT:.0f}% or {TP3_PRICE_TARGET}¢ → sell ALL")
    log.info(f"    Trailing SL: ${TRAIL_SL_THRESHOLD_1:.0f}→{TRAIL_SL_RATIO_1:.0%} / "
             f"${TRAIL_SL_THRESHOLD_2:.0f}→{TRAIL_SL_RATIO_2:.0%} of peak gain  |  "
             f"Hard {HARD_SL_PCT:.0f}%  |  Soft {SOFT_SL_PCT:.0f}% ({SOFT_SL_RATIO:.0%})")
    log.info("  LONGSHOT (single clean exit)")
    log.info(f"    Exit at {LS_EXIT_PRICE}¢ → sell ALL  |  Hard SL {LS_HARD_SL_PCT:.0f}%  (no partials, no trailing)")
    log.info("─" * 72)
    if PIVOT_ENABLED:
        log.info("  PIVOT TRADE (enabled)")
        log.info(f"    Capital: {PIVOT_CAPITAL_RATIO:.0%} of recovered  |  "
                 f"Dog price zone: {PIVOT_MIN_DOG_PRICE}–{PIVOT_MAX_DOG_PRICE}¢")
        log.info(f"    Min momentum: {PIVOT_MIN_MOMENTUM:.0%} price drop  |  "
                 f"Min score momentum: {PIVOT_MIN_SCORE_MOMENTUM}")
        log.info(f"    Exit at {PIVOT_EXIT_PRICE}¢  |  Hard SL {PIVOT_HARD_SL_PCT:.0f}%")
    else:
        log.info("  PIVOT TRADE (disabled — set PIVOT_ENABLED=true to activate)")
    log.info("=" * 72)

    if DRY_RUN:
        log.info("  Set DRY_RUN=false in .env when ready to go live.")
        log.info("=" * 72)

    with httpx.Client() as client:
        while True:
            if _is_bot_enabled():
                now = datetime.datetime.now().strftime("%H:%M:%S")
                log.info(f"─── Scan @ {now} " + "─" * 47)
                run_scan(client)
                log.info(f"Sleeping {POLL_INTERVAL}s...\n")
            else:
                log.info("Bot is PAUSED — toggle via the dashboard to resume.")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
