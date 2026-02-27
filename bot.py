#!/usr/bin/env python3
"""
Kalshi Auto-Sell Bot
====================
Monitors your open YES positions on Kalshi and automatically sells
when your profit target is reached.

HOW IT WORKS
  1. Fetches all your open YES positions from Kalshi.
  2. For each position, calculates your average buy price from fill history.
  3. Checks the current YES bid (what buyers will pay you right now).
  4. If (bid - avg_buy) / avg_buy >= PROFIT_TARGET → places a market sell order.
  5. Logs everything to console + logs/bot.log.
  6. Sleeps POLL_INTERVAL seconds and repeats forever.

CONFIGURATION (environment variables)
  KALSHI_API_KEY      Your Kalshi API public key (required)
  KALSHI_API_SECRET   Your RSA private key in PEM format (required)
                      Use literal \\n for newlines in the env var.
  PROFIT_TARGET       Sell when profit >= this % (default: 35)
  POLL_INTERVAL       Seconds between each scan cycle (default: 10)
  DRY_RUN             If "true", logs what WOULD happen but does NOT sell (default: true)
  KALSHI_BASE_URL     API base URL (default: Kalshi production)

USAGE
  # Install dependencies (same as the dashboard)
  pip install -r requirements.txt

  # Copy and fill in your credentials
  cp .env.example .env
  # Edit .env: set KALSHI_API_KEY, KALSHI_API_SECRET, PROFIT_TARGET, DRY_RUN=false

  # Run locally
  python bot.py

  # Run on a server (keeps running after you disconnect)
  nohup python bot.py >> logs/bot.log 2>&1 &
"""

import os
import sys
import time
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
BASE_URL      = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
API_KEY       = os.getenv("KALSHI_API_KEY", "")
API_SECRET    = os.getenv("KALSHI_API_SECRET", "")
PROFIT_TARGET = float(os.getenv("PROFIT_TARGET", "35"))    # percentage, e.g. 35 = 35%
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))      # seconds between scans
DRY_RUN       = os.getenv("DRY_RUN", "true").lower() == "true"

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
    resp.raise_for_status()
    return resp.json()


# ── Kalshi API Calls ──────────────────────────────────────────────────────────
def get_open_positions(client: httpx.Client) -> list[dict]:
    """
    Fetch all open YES positions (where you hold at least 1 contract).
    Returns a list of position dicts from Kalshi's /portfolio/positions.
    """
    data = _get(client, "/portfolio/positions")
    all_positions = data.get("market_positions", [])
    # Only keep positions where we have a positive YES balance
    return [p for p in all_positions if (p.get("position") or 0) > 0]


def get_avg_buy_price(client: httpx.Client, ticker: str) -> float | None:
    """
    Calculate the weighted average price paid for YES contracts in a market.
    Reads fill history (trades executed) and ignores sell fills.
    Returns price in cents (e.g. 20.5 means 20.5¢), or None if unavailable.
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

    # Filter: only YES buy fills
    buy_fills = [
        f for f in fills
        if f.get("action") == "buy" and f.get("side") == "yes"
    ]

    if not buy_fills:
        log.warning(f"    No YES buy fills found for {ticker}.")
        return None

    # Weighted average: sum(contracts * price) / sum(contracts)
    total_qty   = sum(f.get("count", 0) for f in buy_fills)
    # Kalshi fills use "yes_price" for the price paid per YES contract
    total_cost  = sum(
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
    Returns price in whole cents (e.g. 30 means 30¢), or None.
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


def sell_position(client: httpx.Client, ticker: str, count: int) -> dict:
    """
    Place a market sell order for all YES contracts in a position.
    Market orders execute immediately at the best available price.
    """
    body = {
        "action": "sell",
        "type": "market",
        "ticker": ticker,
        "count": count,
        "side": "yes",
    }
    return _post(client, "/portfolio/orders", body)


# ── Main Scan Logic ───────────────────────────────────────────────────────────
def run_scan(client: httpx.Client):
    """
    One full scan cycle:
    1. Get all open positions.
    2. For each, compute profit vs target.
    3. Sell if target is reached (or log if DRY_RUN).
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

        # Can't make a decision without both prices
        if avg_buy is None:
            log.warning(f"    Skipping — could not determine average buy price.")
            continue
        if bid is None:
            log.warning(f"    Skipping — no YES bid available (market may be illiquid).")
            continue

        profit_pct = ((bid - avg_buy) / avg_buy) * 100

        log.info(
            f"    avg_buy={avg_buy:.1f}¢  |  bid={bid}¢  |  "
            f"profit={profit_pct:+.1f}%  |  target={PROFIT_TARGET:+.0f}%"
        )

        if profit_pct >= PROFIT_TARGET:
            if DRY_RUN:
                log.info(
                    f"    [DRY RUN] Would sell {count} contracts of {ticker} "
                    f"at ~{bid}¢ (profit {profit_pct:+.1f}%)."
                )
            else:
                log.info(f"    TARGET REACHED! Selling {count} contracts...")
                try:
                    result = sell_position(client, ticker, count)
                    log.info(f"    SOLD OK: {result}")
                except Exception as e:
                    log.error(f"    SELL FAILED: {e}")
        else:
            gap = PROFIT_TARGET - profit_pct
            log.info(f"    Holding. Need {gap:.1f}% more to trigger.")


# ── Entry Point ───────────────────────────────────────────────────────────────
def main():
    if not API_KEY or not API_SECRET:
        log.error("KALSHI_API_KEY and KALSHI_API_SECRET must be set in your environment.")
        sys.exit(1)

    mode = "DRY RUN (no real orders)" if DRY_RUN else "LIVE (real orders will be placed)"

    log.info("=" * 62)
    log.info("  Kalshi Auto-Sell Bot")
    log.info(f"  Mode          : {mode}")
    log.info(f"  Profit target : {PROFIT_TARGET}%")
    log.info(f"  Poll interval : {POLL_INTERVAL}s")
    log.info("=" * 62)

    if DRY_RUN:
        log.info("  Set DRY_RUN=false in .env when you're ready to go live.")
        log.info("=" * 62)

    with httpx.Client() as client:
        while True:
            now = datetime.datetime.now().strftime("%H:%M:%S")
            log.info(f"─── Scan @ {now} " + "─" * 40)
            run_scan(client)
            log.info(f"Sleeping {POLL_INTERVAL}s...\n")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
