#!/usr/bin/env python3
"""
Kalshi Auto-Sell Bot — Tiered Take-Profit Edition
==================================================
Monitors your open YES positions on Kalshi and automatically exits them
using a tiered take-profit (TP) strategy plus a fixed stop-loss (SL).

STRATEGY
  TP Level 1 — profit ≥ TP1_PROFIT_PCT (default 25%):
      Sell TP1_SELL_RATIO (default 30%) of initial contracts.
      Locks in a partial gain while letting the rest ride.

  TP Level 2 — profit ≥ TP2_PROFIT_PCT (default 50%):
      Sell TP2_SELL_RATIO (default 40%) of initial contracts.
      Only fires after TP1 has already executed.
      You are now guaranteed a net profit regardless of what happens next.

  TP Level 3 — profit ≥ TP3_PROFIT_PCT (default 75%) OR price ≥ TP3_PRICE_TARGET (default 92¢):
      Sell ALL remaining contracts.

  Stop-Loss — profit ≤ SL_PROFIT_PCT (default -34%) OR price ≤ SL_PRICE (default 35¢):
      Sell ALL remaining contracts immediately.
      Recovers partial capital when the favorite is clearly losing.

HOW IT WORKS
  1. Fetches all your open YES positions from Kalshi.
  2. For each position, calculates your average buy price from fill history.
  3. Checks the current YES bid (what buyers will pay you right now).
  4. Evaluates TP/SL conditions in priority order: SL → TP3 → TP2 → TP1.
  5. Executes market sell for the appropriate number of contracts.
  6. Persists TP state to SQLite so restarts don't cause double-triggers.
  7. Logs everything to console + logs/bot.log.
  8. Sleeps POLL_INTERVAL seconds and repeats forever.

CONFIGURATION (environment variables)
  KALSHI_API_KEY       Your Kalshi API public key (required)
  KALSHI_API_SECRET    Your RSA private key in PEM format (required)
                       Use literal \\n for newlines in the env var.

  TP1_PROFIT_PCT       Profit % to trigger Level 1 (default: 25)
  TP1_SELL_RATIO       Fraction of initial contracts sold at Level 1 (default: 0.30)
  TP2_PROFIT_PCT       Profit % to trigger Level 2 (default: 50)
  TP2_SELL_RATIO       Fraction of initial contracts sold at Level 2 (default: 0.40)
  TP3_PROFIT_PCT       Profit % to trigger Level 3 (default: 75)
  TP3_PRICE_TARGET     Price in ¢ to also trigger Level 3 (default: 92)
  SL_PROFIT_PCT        Loss % to trigger stop-loss — negative value (default: -34)
  SL_PRICE             Price in ¢ to also trigger stop-loss (default: 35)

  POLL_INTERVAL        Seconds between each scan cycle (default: 10)
  DRY_RUN              If "true", logs what WOULD happen but does NOT sell (default: true)
  KALSHI_BASE_URL      API base URL (default: Kalshi production)
  DB_PATH              SQLite path for persisting TP state (default: data/orders.db)

USAGE
  # Install dependencies (same as the dashboard)
  pip install -r requirements.txt

  # Copy and fill in your credentials
  cp .env.example .env
  # Edit .env: set KALSHI_API_KEY, KALSHI_API_SECRET, DRY_RUN=false

  # Run locally
  python bot.py

  # Run on a server (keeps running after you disconnect)
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
    pass  # python-dotenv not installed — rely on actual env vars

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL         = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
API_KEY          = os.getenv("KALSHI_API_KEY", "")
API_SECRET       = os.getenv("KALSHI_API_SECRET", "")
POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL", "10"))       # seconds between scans
DRY_RUN          = os.getenv("DRY_RUN", "true").lower() == "true"
DB_PATH          = os.getenv("DB_PATH", "data/orders.db")

# Tiered take-profit
TP1_PROFIT_PCT   = float(os.getenv("TP1_PROFIT_PCT",   "25"))   # % profit to trigger TP1
TP1_SELL_RATIO   = float(os.getenv("TP1_SELL_RATIO",   "0.30")) # fraction of initial to sell
TP2_PROFIT_PCT   = float(os.getenv("TP2_PROFIT_PCT",   "50"))   # % profit to trigger TP2
TP2_SELL_RATIO   = float(os.getenv("TP2_SELL_RATIO",   "0.40")) # fraction of initial to sell
TP3_PROFIT_PCT   = float(os.getenv("TP3_PROFIT_PCT",   "75"))   # % profit to trigger TP3
TP3_PRICE_TARGET = int(os.getenv(  "TP3_PRICE_TARGET", "92"))   # ¢ price to also trigger TP3

# Stop-loss
SL_PROFIT_PCT    = float(os.getenv("SL_PROFIT_PCT", "-34"))     # % loss to trigger SL (negative)
SL_PRICE         = int(os.getenv(  "SL_PRICE",      "35"))      # ¢ price to also trigger SL

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
    """Build signed headers for a Kalshi API request."""
    ts = str(int(time.time() * 1000))
    path_no_query = path.split("?")[0]
    message = f"{ts}{method}{path_no_query}".encode()
    key = _load_key()
    sig = key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
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
    """
    Fetch all open YES positions (where you hold at least 1 contract).
    Returns a list of position dicts from Kalshi's /portfolio/positions.
    """
    data = _get(client, "/portfolio/positions")
    all_positions = data.get("market_positions", [])
    return [p for p in all_positions if (p.get("position") or 0) > 0]


def get_avg_buy_price(client: httpx.Client, ticker: str) -> float | None:
    """
    Calculate the weighted average price paid for YES contracts in a market.
    Reads fill history (trades executed) and ignores sell fills.
    Returns price in cents (e.g. 53.0 means 53¢), or None if unavailable.
    """
    fills = []
    cursor = None

    for _ in range(5):  # paginate up to 5 pages of fills
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

    buy_fills = [
        f for f in fills
        if f.get("action") == "buy" and f.get("side") == "yes"
    ]

    if not buy_fills:
        log.warning(f"    No YES buy fills found for {ticker}.")
        return None

    total_qty  = sum(f.get("count", 0) for f in buy_fills)
    total_cost = sum(
        f.get("count", 0) * (f.get("yes_price") or f.get("price") or 0)
        for f in buy_fills
    )

    if total_qty == 0:
        return None

    return total_cost / total_qty


def get_yes_bid(client: httpx.Client, ticker: str) -> int | None:
    """
    Get the current YES bid for a market — the highest price a buyer
    is willing to pay if you sell right now.
    Returns price in whole cents (e.g. 67 means 67¢), or None.
    """
    try:
        data = _get(client, f"/markets/{ticker}")
        market = data.get("market", {})
        bid = market.get("yes_bid")
        if bid and int(bid) > 0:
            return int(bid)
        return None
    except Exception as e:
        log.warning(f"    Could not read market data for {ticker}: {e}")
        return None


def sell_position(client: httpx.Client, ticker: str, count: int, yes_price: int) -> dict:
    """
    Place a market sell order for the specified number of YES contracts.
    yes_price is the minimum acceptable price in cents (use current bid for
    immediate execution). Kalshi requires this field even on market orders.
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


# ── TP State Persistence (SQLite) ─────────────────────────────────────────────
def _init_tp_table():
    """Create the tp_state table if it doesn't exist yet."""
    Path("data").mkdir(exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tp_state (
                ticker          TEXT PRIMARY KEY,
                initial_count   INTEGER NOT NULL,
                tp1_done        INTEGER NOT NULL DEFAULT 0,
                tp2_done        INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            )
        """)


def _load_tp_state(ticker: str, current_count: int) -> dict:
    """
    Load persisted TP state for a ticker.
    If the ticker is not yet known, initialize it with current_count as the
    baseline (i.e. treat whatever is held now as 100% of the position).
    """
    now = datetime.datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT initial_count, tp1_done, tp2_done FROM tp_state WHERE ticker=?",
            (ticker,),
        ).fetchone()
        if row:
            return {"initial": row[0], "tp1_done": bool(row[1]), "tp2_done": bool(row[2])}
        # First time seeing this ticker — record it
        conn.execute(
            "INSERT INTO tp_state (ticker, initial_count, tp1_done, tp2_done, created_at, updated_at) "
            "VALUES (?, ?, 0, 0, ?, ?)",
            (ticker, current_count, now, now),
        )
    log.info(f"    New position tracked: initial_count={current_count}")
    return {"initial": current_count, "tp1_done": False, "tp2_done": False}


def _save_tp_state(ticker: str, state: dict):
    """Persist updated TP level flags for a ticker."""
    now = datetime.datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE tp_state SET tp1_done=?, tp2_done=?, updated_at=? WHERE ticker=?",
            (int(state["tp1_done"]), int(state["tp2_done"]), now, ticker),
        )


def _clear_tp_state(ticker: str):
    """Remove a ticker from the state table once the position is fully closed."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM tp_state WHERE ticker=?", (ticker,))


# ── Sell Helper ───────────────────────────────────────────────────────────────
def _execute_sell(
    client: httpx.Client,
    ticker: str,
    count: int,
    bid: int,
    profit_pct: float,
    label: str,
) -> bool:
    """
    Execute (or simulate) a market sell order and log the outcome.
    Returns True if the sell succeeded (or would succeed in DRY_RUN).
    """
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
    One full scan cycle:
    1. Get all open YES positions.
    2. For each, compute current profit vs. avg buy price.
    3. Evaluate TP/SL conditions in order: SL → TP3 → TP2 → TP1.
    4. Execute partial or full market sell as appropriate.
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

        state    = _load_tp_state(ticker, count)
        initial  = state["initial"]
        tp1_done = state["tp1_done"]
        tp2_done = state["tp2_done"]

        log.info(
            f"    avg_buy={avg_buy:.1f}¢  bid={bid}¢  profit={profit_pct:+.1f}%  "
            f"initial={initial}  "
            f"tp1={'✓' if tp1_done else '○'}  tp2={'✓' if tp2_done else '○'}"
        )

        # ── STOP-LOSS (highest priority) ──────────────────────────────────────
        if bid <= SL_PRICE or profit_pct <= SL_PROFIT_PCT:
            if bid <= SL_PRICE:
                reason = f"price {bid}¢ ≤ {SL_PRICE}¢"
            else:
                reason = f"profit {profit_pct:.1f}% ≤ {SL_PROFIT_PCT:.0f}%"
            _execute_sell(
                client, ticker, count, bid, profit_pct,
                f"STOP-LOSS ({reason}) — sell ALL {count} contract(s)",
            )
            _clear_tp_state(ticker)
            continue

        # ── TAKE-PROFIT Level 3 — sell ALL remaining ──────────────────────────
        if profit_pct >= TP3_PROFIT_PCT or bid >= TP3_PRICE_TARGET:
            _execute_sell(
                client, ticker, count, bid, profit_pct,
                f"TP3 — sell remaining {count} contract(s) "
                f"(profit {profit_pct:+.1f}% | price {bid}¢)",
            )
            _clear_tp_state(ticker)
            continue

        # ── TAKE-PROFIT Level 2 — sell 40% of initial (after TP1) ────────────
        if profit_pct >= TP2_PROFIT_PCT and tp1_done and not tp2_done:
            qty = max(1, min(round(initial * TP2_SELL_RATIO), count))
            if _execute_sell(
                client, ticker, qty, bid, profit_pct,
                f"TP2 — sell {qty} of {count} contract(s) "
                f"({TP2_SELL_RATIO:.0%} of initial {initial})",
            ):
                state["tp2_done"] = True
                _save_tp_state(ticker, state)
            continue

        # ── TAKE-PROFIT Level 1 — sell 30% of initial ────────────────────────
        if profit_pct >= TP1_PROFIT_PCT and not tp1_done:
            qty = max(1, min(round(initial * TP1_SELL_RATIO), count))
            if _execute_sell(
                client, ticker, qty, bid, profit_pct,
                f"TP1 — sell {qty} of {count} contract(s) "
                f"({TP1_SELL_RATIO:.0%} of initial {initial})",
            ):
                state["tp1_done"] = True
                _save_tp_state(ticker, state)
            continue

        # ── HOLDING — no trigger reached yet ─────────────────────────────────
        if not tp1_done:
            next_label, next_pct = "TP1", TP1_PROFIT_PCT
        elif not tp2_done:
            next_label, next_pct = "TP2", TP2_PROFIT_PCT
        else:
            next_label, next_pct = "TP3", TP3_PROFIT_PCT

        log.info(
            f"    Holding. Profit {profit_pct:+.1f}% — "
            f"next trigger: {next_label} at +{next_pct:.0f}%  "
            f"(SL at {SL_PROFIT_PCT:.0f}% or ≤{SL_PRICE}¢)"
        )


# ── Entry Point ───────────────────────────────────────────────────────────────
def main():
    if not API_KEY or not API_SECRET:
        log.error("KALSHI_API_KEY and KALSHI_API_SECRET must be set in your environment.")
        sys.exit(1)

    _init_tp_table()

    mode = "DRY RUN (no real orders)" if DRY_RUN else "LIVE (real orders will be placed)"

    log.info("=" * 70)
    log.info("  Kalshi Auto-Sell Bot — Tiered Take-Profit Edition")
    log.info(f"  Mode          : {mode}")
    log.info(f"  Poll interval : {POLL_INTERVAL}s")
    log.info("─" * 70)
    log.info(f"  TP1           : profit ≥ +{TP1_PROFIT_PCT:.0f}%  → sell {TP1_SELL_RATIO:.0%} of position")
    log.info(f"  TP2           : profit ≥ +{TP2_PROFIT_PCT:.0f}%  → sell {TP2_SELL_RATIO:.0%} of position  (after TP1)")
    log.info(f"  TP3           : profit ≥ +{TP3_PROFIT_PCT:.0f}% OR price ≥ {TP3_PRICE_TARGET}¢  → sell remaining")
    log.info(f"  Stop-Loss     : profit ≤ {SL_PROFIT_PCT:.0f}% OR price ≤ {SL_PRICE}¢  → sell ALL")
    log.info("=" * 70)

    if DRY_RUN:
        log.info("  Set DRY_RUN=false in .env when you're ready to go live.")
        log.info("=" * 70)

    with httpx.Client() as client:
        while True:
            now = datetime.datetime.now().strftime("%H:%M:%S")
            log.info(f"─── Scan @ {now} " + "─" * 45)
            run_scan(client)
            log.info(f"Sleeping {POLL_INTERVAL}s...\n")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
