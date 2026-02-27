#!/usr/bin/env python3
"""
Kalshi Auto-Sell Bot — Tiered Take-Profit + Dynamic Stop-Loss
=============================================================
Monitors your open YES positions on Kalshi and automatically manages them
using a tiered take-profit (TP) strategy combined with a three-layer
dynamic stop-loss system.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TAKE-PROFIT STRATEGY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  TP1 — profit ≥ +25%  →  sell 30% of initial contracts
        Locks in a partial gain; the rest keeps riding.

  TP2 — profit ≥ +50%  →  sell 40% of initial contracts  (after TP1)
        At this point you're guaranteed a net profit regardless.

  TP3 — profit ≥ +75% OR price ≥ 92¢  →  sell ALL remaining contracts

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STOP-LOSS STRATEGY (three layers, evaluated in priority order)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  1. TRAILING STOP — protects profits once they reach a threshold.
     Computed dynamically from the peak bid ever seen:

       If peak profit on original position ≥ $2:
         trailing_sl = avg_buy + (peak_gain_per_contract × 30%)
       If peak profit on original position ≥ $4:
         trailing_sl = avg_buy + (peak_gain_per_contract × 50%)

     When bid falls below trailing_sl → sell ALL remaining.
     Example: bought 53¢, peak bid 80¢ → trailing_sl = 53 + 27×0.50 = 66.5¢

  2. HARD STOP — large loss protection.
     Triggers when profit ≤ HARD_SL_PCT (default −35%)
     which equals bid ≤ avg_buy × 0.65 (dynamic, scales with entry price).
     Action: sell ALL remaining contracts.

  3. SOFT STOP — moderate loss protection (fires once, then arms hard stop).
     Triggers when profit ≤ SOFT_SL_PCT (default −20%).
     Action: sell SOFT_SL_RATIO (default 50%) of remaining contracts.
     Recovers half your capital early; hard stop protects the rest.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EVALUATION ORDER each scan cycle
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  (1) Trailing Stop  →  bid < trailing_sl  (if active)
  (2) Hard Stop      →  profit ≤ −35%
  (3) Soft Stop      →  profit ≤ −20%  (once only)
  (4) TP3            →  profit ≥ +75% or price ≥ 92¢
  (5) TP2            →  profit ≥ +50% (after TP1)
  (6) TP1            →  profit ≥ +25%
  (7) Hold           →  update peak bid, log status

HOW IT WORKS
  1. Fetches all open YES positions from Kalshi.
  2. For each, calculates average buy price from fill history.
  3. Checks the current YES bid (what buyers will pay right now).
  4. Updates peak bid in SQLite if a new high is reached.
  5. Evaluates conditions in priority order and executes sells.
  6. Persists all state to SQLite — restarts are safe.
  7. Logs everything to console + logs/bot.log.
  8. Sleeps POLL_INTERVAL seconds and repeats forever.

CONFIGURATION (env vars — see .env.example for full list)
  KALSHI_API_KEY         Required
  KALSHI_API_SECRET      Required (RSA PEM, use literal \\n)
  DRY_RUN                true = simulate only (default: true)
  POLL_INTERVAL          Seconds between scans (default: 10)
  DB_PATH                SQLite file (default: data/orders.db)

USAGE
  cp .env.example .env   # fill in credentials
  python bot.py          # run locally
  nohup python bot.py >> logs/bot.log 2>&1 &   # run on a server
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

# Take-profit levels
TP1_PROFIT_PCT   = float(os.getenv("TP1_PROFIT_PCT",   "25"))    # % profit → sell TP1_SELL_RATIO
TP1_SELL_RATIO   = float(os.getenv("TP1_SELL_RATIO",   "0.30"))
TP2_PROFIT_PCT   = float(os.getenv("TP2_PROFIT_PCT",   "50"))    # % profit → sell TP2_SELL_RATIO (after TP1)
TP2_SELL_RATIO   = float(os.getenv("TP2_SELL_RATIO",   "0.40"))
TP3_PROFIT_PCT   = float(os.getenv("TP3_PROFIT_PCT",   "75"))    # % profit → sell ALL remaining
TP3_PRICE_TARGET = int(os.getenv(  "TP3_PRICE_TARGET", "92"))    # ¢ price → also triggers TP3

# Stop-loss: soft + hard (dynamic, relative to entry price)
SOFT_SL_PCT   = float(os.getenv("SOFT_SL_PCT",   "-20"))   # % loss → sell SOFT_SL_RATIO
SOFT_SL_RATIO = float(os.getenv("SOFT_SL_RATIO", "0.50"))  # fraction of remaining to sell at soft stop
HARD_SL_PCT   = float(os.getenv("HARD_SL_PCT",   "-35"))   # % loss → sell ALL (= avg_buy × 0.65)

# Trailing stop: activates once peak profit (on original position) exceeds threshold
TRAIL_SL_THRESHOLD_1 = float(os.getenv("TRAIL_SL_THRESHOLD_1", "2.0"))  # $2 → lock 30% of peak gain
TRAIL_SL_RATIO_1     = float(os.getenv("TRAIL_SL_RATIO_1",     "0.30"))
TRAIL_SL_THRESHOLD_2 = float(os.getenv("TRAIL_SL_THRESHOLD_2", "4.0"))  # $4 → lock 50% of peak gain
TRAIL_SL_RATIO_2     = float(os.getenv("TRAIL_SL_RATIO_2",     "0.50"))

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
    path_no_query = path.split("?")[0]
    message = f"{ts}{method}{path_no_query}".encode()
    key = _load_key()
    sig = key.sign(
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
    url = f"{BASE_URL}{path}"
    headers = _auth_headers("GET", f"/trade-api/v2{path}")
    resp = client.get(url, headers=headers, params=params, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


def _post(client: httpx.Client, path: str, body: dict) -> dict:
    url = f"{BASE_URL}{path}"
    headers = _auth_headers("POST", f"/trade-api/v2{path}")
    resp = client.post(url, headers=headers, json=body, timeout=15.0)
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
    """Weighted average price paid for YES contracts, in cents. None if unavailable."""
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
        data   = _get(client, f"/markets/{ticker}")
        market = data.get("market", {})
        bid    = market.get("yes_bid")
        if bid and int(bid) > 0:
            return int(bid)
        return None
    except Exception as e:
        log.warning(f"    Could not read market data for {ticker}: {e}")
        return None


def sell_position(client: httpx.Client, ticker: str, count: int, yes_price: int) -> dict:
    """
    Market sell order for `count` YES contracts.
    yes_price (current bid in ¢) is required by the Kalshi API even for market orders.
    """
    body = {
        "action": "sell",
        "type": "market",
        "ticker": ticker,
        "count": count,
        "side": "yes",
        "yes_price": max(1, yes_price),
        "client_order_id": str(uuid.uuid4()),
    }
    return _post(client, "/portfolio/orders", body)


# ── State Persistence (SQLite) ────────────────────────────────────────────────
def _init_state_table():
    """Create the tp_state table and add any missing columns (safe for existing DBs)."""
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
        # Migrate existing tables that pre-date the v2 stop-loss columns
        for col, default in [("soft_stop_done", 0), ("max_bid_seen", 0)]:
            try:
                conn.execute(
                    f"ALTER TABLE tp_state ADD COLUMN {col} INTEGER NOT NULL DEFAULT {default}"
                )
            except sqlite3.OperationalError:
                pass  # column already exists — no action needed


def _load_state(ticker: str, current_count: int) -> dict:
    """
    Load persisted state for a ticker. Initializes on first sight,
    treating current_count as the baseline (100% of position).
    """
    now = datetime.datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT initial_count, tp1_done, tp2_done, soft_stop_done, max_bid_seen "
            "FROM tp_state WHERE ticker=?",
            (ticker,),
        ).fetchone()
        if row:
            return {
                "initial":        row[0],
                "tp1_done":       bool(row[1]),
                "tp2_done":       bool(row[2]),
                "soft_stop_done": bool(row[3]),
                "max_bid_seen":   row[4],
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
    """Persist all mutable state fields for a ticker."""
    now = datetime.datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE tp_state "
            "SET tp1_done=?, tp2_done=?, soft_stop_done=?, max_bid_seen=?, updated_at=? "
            "WHERE ticker=?",
            (int(state["tp1_done"]), int(state["tp2_done"]),
             int(state["soft_stop_done"]), int(state["max_bid_seen"]),
             now, ticker),
        )


def _clear_state(ticker: str):
    """Remove a ticker's state once the position is fully closed."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM tp_state WHERE ticker=?", (ticker,))


# ── Trailing Stop Calculator ──────────────────────────────────────────────────
def _calc_trailing_sl(avg_buy: float, max_bid_seen: int, initial_count: int) -> float | None:
    """
    Compute the trailing stop-loss price in cents, or None if not yet active.

    Uses initial_count (original position size) to calculate peak profit in $,
    ensuring thresholds are consistent even after partial TP sells.

    TRAIL_SL_THRESHOLD_1 ($2) → lock TRAIL_SL_RATIO_1 (30%) of peak gain per contract
    TRAIL_SL_THRESHOLD_2 ($4) → lock TRAIL_SL_RATIO_2 (50%) of peak gain per contract
    """
    peak_gain_per_contract = max_bid_seen - avg_buy  # cents
    if peak_gain_per_contract <= 0:
        return None  # position has never been in profit

    peak_profit_dollars = (peak_gain_per_contract / 100.0) * initial_count

    if peak_profit_dollars >= TRAIL_SL_THRESHOLD_2:
        return avg_buy + peak_gain_per_contract * TRAIL_SL_RATIO_2
    if peak_profit_dollars >= TRAIL_SL_THRESHOLD_1:
        return avg_buy + peak_gain_per_contract * TRAIL_SL_RATIO_1
    return None


# ── Sell Helper ───────────────────────────────────────────────────────────────
def _execute_sell(
    client: httpx.Client,
    ticker: str,
    count: int,
    bid: int,
    profit_pct: float,
    label: str,
) -> bool:
    """Execute (or simulate) a market sell. Returns True on success."""
    if DRY_RUN:
        log.info(
            f"    [DRY RUN] {label} — Would sell {count} contract(s) "
            f"at ~{bid}¢  (profit {profit_pct:+.1f}%)"
        )
        return True

    log.info(
        f"    {label} — Selling {count} contract(s) at ~{bid}¢  "
        f"(profit {profit_pct:+.1f}%)..."
    )
    try:
        result = sell_position(client, ticker, count, yes_price=bid)
        log.info(f"    SOLD OK: {result}")
        return True
    except Exception as e:
        log.error(f"    SELL FAILED: {e}")
        return False


# ── Main Scan Logic ───────────────────────────────────────────────────────────
def run_scan(client: httpx.Client):
    """
    One full scan cycle. For each open YES position:
      1. Fetch avg buy price + current bid.
      2. Update peak bid (max_bid_seen) in DB.
      3. Compute trailing SL price.
      4. Evaluate exits in priority order: Trailing → Hard → Soft → TP3 → TP2 → TP1 → Hold.
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

        # Load state and update peak bid if a new high was reached
        state = _load_state(ticker, count)
        if bid > state["max_bid_seen"]:
            state["max_bid_seen"] = bid
            _save_state(ticker, state)

        initial        = state["initial"]
        tp1_done       = state["tp1_done"]
        tp2_done       = state["tp2_done"]
        soft_stop_done = state["soft_stop_done"]
        max_bid_seen   = state["max_bid_seen"]

        # Compute dynamic thresholds
        hard_sl_price  = avg_buy * (1.0 + HARD_SL_PCT / 100.0)   # e.g. 53 × 0.65 = 34.5¢
        trailing_sl    = _calc_trailing_sl(avg_buy, max_bid_seen, initial)

        # Build trailing SL display string for log
        if trailing_sl is not None:
            trail_str = f"trail_sl={trailing_sl:.1f}¢"
        else:
            trail_str = "trail_sl=—"

        log.info(
            f"    avg_buy={avg_buy:.1f}¢  bid={bid}¢  profit={profit_pct:+.1f}%  "
            f"peak={max_bid_seen}¢  {trail_str}  "
            f"hard_sl={hard_sl_price:.1f}¢  soft_sl={SOFT_SL_PCT:.0f}%  "
            f"tp1={'✓' if tp1_done else '○'}  tp2={'✓' if tp2_done else '○'}  "
            f"soft_stop={'✓' if soft_stop_done else '○'}"
        )

        # ── (1) TRAILING STOP — protect accumulated gains ─────────────────────
        if trailing_sl is not None and bid < trailing_sl:
            _execute_sell(
                client, ticker, count, bid, profit_pct,
                f"TRAILING STOP (bid {bid}¢ < floor {trailing_sl:.1f}¢, "
                f"peak {max_bid_seen}¢) — sell ALL {count}",
            )
            _clear_state(ticker)
            continue

        # ── (2) HARD STOP — dynamic price floor at entry × 0.65 ──────────────
        if profit_pct <= HARD_SL_PCT:
            _execute_sell(
                client, ticker, count, bid, profit_pct,
                f"HARD STOP (profit {profit_pct:.1f}% ≤ {HARD_SL_PCT:.0f}%, "
                f"floor {hard_sl_price:.1f}¢) — sell ALL {count}",
            )
            _clear_state(ticker)
            continue

        # ── (3) SOFT STOP — moderate loss, partial exit ───────────────────────
        if profit_pct <= SOFT_SL_PCT and not soft_stop_done:
            qty = max(1, round(count * SOFT_SL_RATIO))
            if _execute_sell(
                client, ticker, qty, bid, profit_pct,
                f"SOFT STOP (profit {profit_pct:.1f}% ≤ {SOFT_SL_PCT:.0f}%) "
                f"— sell {qty} of {count} ({SOFT_SL_RATIO:.0%})",
            ):
                state["soft_stop_done"] = True
                _save_state(ticker, state)
            continue

        # ── (4) TAKE-PROFIT Level 3 — sell ALL remaining ─────────────────────
        if profit_pct >= TP3_PROFIT_PCT or bid >= TP3_PRICE_TARGET:
            _execute_sell(
                client, ticker, count, bid, profit_pct,
                f"TP3 — sell remaining {count} "
                f"(profit {profit_pct:+.1f}% | price {bid}¢)",
            )
            _clear_state(ticker)
            continue

        # ── (5) TAKE-PROFIT Level 2 — sell 40% of initial (after TP1) ────────
        if profit_pct >= TP2_PROFIT_PCT and tp1_done and not tp2_done:
            qty = max(1, min(round(initial * TP2_SELL_RATIO), count))
            if _execute_sell(
                client, ticker, qty, bid, profit_pct,
                f"TP2 — sell {qty} of {count} ({TP2_SELL_RATIO:.0%} of initial {initial})",
            ):
                state["tp2_done"] = True
                _save_state(ticker, state)
            continue

        # ── (6) TAKE-PROFIT Level 1 — sell 30% of initial ────────────────────
        if profit_pct >= TP1_PROFIT_PCT and not tp1_done:
            qty = max(1, min(round(initial * TP1_SELL_RATIO), count))
            if _execute_sell(
                client, ticker, qty, bid, profit_pct,
                f"TP1 — sell {qty} of {count} ({TP1_SELL_RATIO:.0%} of initial {initial})",
            ):
                state["tp1_done"] = True
                _save_state(ticker, state)
            continue

        # ── (7) HOLD — log next trigger ───────────────────────────────────────
        if profit_pct < SOFT_SL_PCT:
            # Soft stop already fired — next trigger is hard stop
            next_label = f"HARD STOP at {HARD_SL_PCT:.0f}% (floor {hard_sl_price:.1f}¢)"
        elif not tp1_done:
            next_label = f"TP1 at +{TP1_PROFIT_PCT:.0f}%"
        elif not tp2_done:
            next_label = f"TP2 at +{TP2_PROFIT_PCT:.0f}%"
        else:
            next_label = f"TP3 at +{TP3_PROFIT_PCT:.0f}% or {TP3_PRICE_TARGET}¢"

        log.info(f"    Holding — next trigger: {next_label}")


# ── Entry Point ───────────────────────────────────────────────────────────────
def main():
    if not API_KEY or not API_SECRET:
        log.error("KALSHI_API_KEY and KALSHI_API_SECRET must be set in your environment.")
        sys.exit(1)

    _init_state_table()

    mode = "DRY RUN (no real orders)" if DRY_RUN else "LIVE (real orders will be placed)"

    log.info("=" * 72)
    log.info("  Kalshi Auto-Sell Bot — Tiered Take-Profit + Dynamic Stop-Loss")
    log.info(f"  Mode          : {mode}")
    log.info(f"  Poll interval : {POLL_INTERVAL}s")
    log.info("─" * 72)
    log.info("  TAKE-PROFIT")
    log.info(f"    TP1 : profit ≥ +{TP1_PROFIT_PCT:.0f}%  → sell {TP1_SELL_RATIO:.0%} of position")
    log.info(f"    TP2 : profit ≥ +{TP2_PROFIT_PCT:.0f}%  → sell {TP2_SELL_RATIO:.0%} of position  (after TP1)")
    log.info(f"    TP3 : profit ≥ +{TP3_PROFIT_PCT:.0f}% OR price ≥ {TP3_PRICE_TARGET}¢  → sell ALL remaining")
    log.info("  STOP-LOSS")
    log.info(f"    Trailing : peak ≥ ${TRAIL_SL_THRESHOLD_1:.0f} → floor at entry+{TRAIL_SL_RATIO_1:.0%} of gain")
    log.info(f"               peak ≥ ${TRAIL_SL_THRESHOLD_2:.0f} → floor at entry+{TRAIL_SL_RATIO_2:.0%} of gain  → sell ALL")
    log.info(f"    Hard     : profit ≤ {HARD_SL_PCT:.0f}%  (= entry × {1+HARD_SL_PCT/100:.2f})  → sell ALL")
    log.info(f"    Soft     : profit ≤ {SOFT_SL_PCT:.0f}%  → sell {SOFT_SL_RATIO:.0%} of remaining (once)")
    log.info("=" * 72)

    if DRY_RUN:
        log.info("  Set DRY_RUN=false in .env when you're ready to go live.")
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
