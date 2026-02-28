#!/usr/bin/env python3
"""
Kalshi Auto-Sell Bot — Dual-Mode: Favorite + Longshot
======================================================
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


def get_yes_bid(client: httpx.Client, ticker: str) -> int | None:
    """Current YES bid in whole cents, or None."""
    try:
        market = _get(client, f"/markets/{ticker}").get("market", {})
        bid    = market.get("yes_bid")
        return int(bid) if bid and int(bid) > 0 else None
    except Exception as e:
        log.warning(f"    Could not read market data for {ticker}: {e}")
        return None


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

        conn.execute("""
            CREATE TABLE IF NOT EXISTS sell_events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker       TEXT    NOT NULL,
                action       TEXT    NOT NULL,
                mode         TEXT    NOT NULL,
                count_sold   INTEGER NOT NULL,
                bid_cents    INTEGER NOT NULL,
                avg_buy      REAL    NOT NULL,
                profit_pct   REAL    NOT NULL,
                pnl_est      REAL,
                dry_run      INTEGER NOT NULL DEFAULT 1,
                executed_at  TEXT    NOT NULL
            )
        """)


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


def _record_sell_event(
    ticker: str, action: str, mode: str,
    count_sold: int, bid_cents: int, avg_buy: float, profit_pct: float,
):
    """Persist a sell action to the sell_events table."""
    pnl_est = round((bid_cents - avg_buy) * count_sold / 100, 2)
    now = datetime.datetime.utcnow().isoformat()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO sell_events
                    (ticker, action, mode, count_sold, bid_cents, avg_buy,
                     profit_pct, pnl_est, dry_run, executed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ticker, action, mode, count_sold, bid_cents, avg_buy,
                 round(profit_pct, 2), pnl_est, 1 if DRY_RUN else 0, now),
            )
    except Exception as e:
        log.warning(f"    Could not record sell event for {ticker}: {e}")


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
    action: str = "", mode: str = "", avg_buy: float = 0.0,
) -> bool:
    if DRY_RUN:
        log.info(
            f"    [DRY RUN] {label} — Would sell {count} contract(s) "
            f"at ~{bid}¢  (profit {profit_pct:+.1f}%)"
        )
        _record_sell_event(ticker, action, mode, count, bid, avg_buy, profit_pct)
        return True
    log.info(f"    {label} — Selling {count} contract(s) at ~{bid}¢  (profit {profit_pct:+.1f}%)...")
    try:
        log.info(f"    SOLD OK: {sell_position(client, ticker, count, yes_price=bid)}")
        _record_sell_event(ticker, action, mode, count, bid, avg_buy, profit_pct)
        return True
    except Exception as e:
        log.error(f"    SELL FAILED: {e}")
        return False


# ── Mode-specific evaluators ──────────────────────────────────────────────────
def _evaluate_favorite(
    client: httpx.Client, ticker: str, count: int,
    bid: int, avg_buy: float, profit_pct: float, state: dict,
):
    """
    Exit logic for FAVORITE positions (avg_buy ≥ LONGSHOT_THRESHOLD).
    Priority: Trailing Stop → Hard Stop → Soft Stop → TP3 → TP2 → TP1 → Hold
    """
    initial        = state["initial"]
    tp1_done       = state["tp1_done"]
    tp2_done       = state["tp2_done"]
    soft_stop_done = state["soft_stop_done"]
    max_bid_seen   = state["max_bid_seen"]

    hard_sl_price = avg_buy * (1.0 + HARD_SL_PCT / 100.0)
    trailing_sl   = _calc_trailing_sl(avg_buy, max_bid_seen, initial)
    trail_str     = f"{trailing_sl:.1f}¢" if trailing_sl is not None else "—"

    log.info(
        f"    [FAVORITE]  avg_buy={avg_buy:.1f}¢  bid={bid}¢  profit={profit_pct:+.1f}%  "
        f"peak={max_bid_seen}¢  trail_sl={trail_str}  hard_sl={hard_sl_price:.1f}¢  "
        f"tp1={'✓' if tp1_done else '○'}  tp2={'✓' if tp2_done else '○'}  "
        f"soft={'✓' if soft_stop_done else '○'}"
    )

    # (1) Trailing stop
    if trailing_sl is not None and bid < trailing_sl:
        _execute_sell(client, ticker, count, bid, profit_pct,
            f"TRAILING STOP (bid {bid}¢ < floor {trailing_sl:.1f}¢, peak {max_bid_seen}¢) — sell ALL {count}",
            action="TRAILING_STOP", mode="FAVORITE", avg_buy=avg_buy)
        _clear_state(ticker)
        return

    # (2) Hard stop — dynamic floor at avg_buy × 0.65
    if profit_pct <= HARD_SL_PCT:
        _execute_sell(client, ticker, count, bid, profit_pct,
            f"HARD STOP (profit {profit_pct:.1f}% ≤ {HARD_SL_PCT:.0f}%, floor {hard_sl_price:.1f}¢) — sell ALL {count}",
            action="HARD_STOP", mode="FAVORITE", avg_buy=avg_buy)
        _clear_state(ticker)
        return

    # (3) Soft stop — partial exit
    if profit_pct <= SOFT_SL_PCT and not soft_stop_done:
        qty = max(1, round(count * SOFT_SL_RATIO))
        if _execute_sell(client, ticker, qty, bid, profit_pct,
                f"SOFT STOP (profit {profit_pct:.1f}% ≤ {SOFT_SL_PCT:.0f}%) — sell {qty} of {count} ({SOFT_SL_RATIO:.0%})",
                action="SOFT_STOP", mode="FAVORITE", avg_buy=avg_buy):
            state["soft_stop_done"] = True
            _save_state(ticker, state)
        return

    # (4) TP3 — sell ALL remaining
    if profit_pct >= TP3_PROFIT_PCT or bid >= TP3_PRICE_TARGET:
        _execute_sell(client, ticker, count, bid, profit_pct,
            f"TP3 — sell remaining {count} (profit {profit_pct:+.1f}% | price {bid}¢)",
            action="TP3", mode="FAVORITE", avg_buy=avg_buy)
        _clear_state(ticker)
        return

    # (5) TP2 — sell 40% of initial (after TP1)
    if profit_pct >= TP2_PROFIT_PCT and tp1_done and not tp2_done:
        qty = max(1, min(round(initial * TP2_SELL_RATIO), count))
        if _execute_sell(client, ticker, qty, bid, profit_pct,
                f"TP2 — sell {qty} of {count} ({TP2_SELL_RATIO:.0%} of initial {initial})",
                action="TP2", mode="FAVORITE", avg_buy=avg_buy):
            state["tp2_done"] = True
            _save_state(ticker, state)
        return

    # (6) TP1 — sell 30% of initial
    if profit_pct >= TP1_PROFIT_PCT and not tp1_done:
        qty = max(1, min(round(initial * TP1_SELL_RATIO), count))
        if _execute_sell(client, ticker, qty, bid, profit_pct,
                f"TP1 — sell {qty} of {count} ({TP1_SELL_RATIO:.0%} of initial {initial})",
                action="TP1", mode="FAVORITE", avg_buy=avg_buy):
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
    bid: int, avg_buy: float, profit_pct: float, state: dict,
):
    """
    Exit logic for LONGSHOT positions (avg_buy < LONGSHOT_THRESHOLD).

    Single clean exit at LS_EXIT_PRICE (default 57¢): once the price reaches
    the coin-flip zone the original edge is gone — sell everything and move on.
    Partial exits are skipped: they cut the best positions while running.

    Priority: Hard Stop → Exit TP → Hold
    """
    hard_sl_price = avg_buy * (1.0 + LS_HARD_SL_PCT / 100.0)

    log.info(
        f"    [LONGSHOT]  avg_buy={avg_buy:.1f}¢  bid={bid}¢  profit={profit_pct:+.1f}%  "
        f"exit_target={LS_EXIT_PRICE}¢  hard_sl={hard_sl_price:.1f}¢ ({LS_HARD_SL_PCT:.0f}%)"
    )

    # (1) Hard stop — accept big loss, recover what's left
    if profit_pct <= LS_HARD_SL_PCT:
        _execute_sell(client, ticker, count, bid, profit_pct,
            f"LONGSHOT HARD STOP (profit {profit_pct:.1f}% ≤ {LS_HARD_SL_PCT:.0f}%, "
            f"floor {hard_sl_price:.1f}¢) — sell ALL {count}",
            action="LONGSHOT_HARD_STOP", mode="LONGSHOT", avg_buy=avg_buy)
        _clear_state(ticker)
        return

    # (2) Exit TP — coin-flip zone reached, edge is gone, sell everything
    if bid >= LS_EXIT_PRICE:
        _execute_sell(client, ticker, count, bid, profit_pct,
            f"LONGSHOT EXIT (price {bid}¢ ≥ {LS_EXIT_PRICE}¢) — sell ALL {count}",
            action="LONGSHOT_EXIT", mode="LONGSHOT", avg_buy=avg_buy)
        _clear_state(ticker)
        return

    # (3) Hold — waiting for the move
    log.info(
        f"    Holding (longshot) — exit at {LS_EXIT_PRICE}¢  "
        f"(hard SL at {LS_HARD_SL_PCT:.0f}%, floor {hard_sl_price:.1f}¢)"
    )


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

        avg_buy = get_avg_buy_price(client, ticker)
        bid     = get_yes_bid(client, ticker)

        if avg_buy is None:
            log.warning("    Skipping — could not determine average buy price.")
            continue
        if bid is None:
            log.warning("    Skipping — no YES bid available (market may be illiquid).")
            continue

        profit_pct = ((bid - avg_buy) / avg_buy) * 100

        # Load state and refresh peak bid
        state = _load_state(ticker, count)
        if bid > state["max_bid_seen"]:
            state["max_bid_seen"] = bid
            _save_state(ticker, state)

        # Route to the correct exit strategy
        if avg_buy < LONGSHOT_THRESHOLD:
            _evaluate_longshot(client, ticker, count, bid, avg_buy, profit_pct, state)
        else:
            _evaluate_favorite(client, ticker, count, bid, avg_buy, profit_pct, state)


# ── Entry Point ───────────────────────────────────────────────────────────────
def main():
    if not API_KEY or not API_SECRET:
        log.error("KALSHI_API_KEY and KALSHI_API_SECRET must be set in your environment.")
        sys.exit(1)

    _init_state_table()

    mode = "DRY RUN (no real orders)" if DRY_RUN else "LIVE (real orders will be placed)"

    log.info("=" * 72)
    log.info("  Kalshi Auto-Sell Bot — Dual-Mode: Favorite + Longshot")
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
    log.info("=" * 72)

    if DRY_RUN:
        log.info("  Set DRY_RUN=false in .env when ready to go live.")
        log.info("=" * 72)

    with httpx.Client() as client:
        while True:
            now = datetime.datetime.now().strftime("%H:%M:%S")
            log.info(f"─── Scan @ {now} " + "─" * 47)
            run_scan(client)
            log.info(f"Sleeping {POLL_INTERVAL}s...\n")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
