"""
Kalshi order placement — adds write capability to the trading system.

Uses the same RSA-PSS authentication as kalshi_client.py.
Places limit YES orders at the TARGET price calculated by the engine.

Kalshi API docs: https://docs.kalshi.com
Endpoint: POST /trade-api/v2/portfolio/orders
"""

import os
import json
import httpx
import logging
from app.kalshi_client import _auth_headers, KALSHI_BASE_URL

logger = logging.getLogger(__name__)

# Safety caps (configurable via env vars)
CONTRACTS_PER_TRADE = int(os.getenv("CONTRACTS_PER_TRADE", "50"))
MAX_CONTRACTS_PER_ORDER = int(os.getenv("MAX_CONTRACTS_PER_ORDER", "100"))


async def place_limit_order(
    ticker: str,
    yes_price: int,   # Limit price in cents (1–99)
    count: int,       # Number of contracts to buy (each pays $1 if YES wins)
    dry_run: bool = True,
) -> dict:
    """
    Place a limit YES buy order on Kalshi.

    Args:
        ticker:    Kalshi market ticker (e.g., KXATPMATCH-25FEB-T-SINNER)
        yes_price: Limit price in cents (e.g., 58 → buy at 58¢, pays $1 if YES wins)
        count:     Number of contracts (capped at MAX_CONTRACTS_PER_ORDER)
        dry_run:   If True, log the intended order without executing it

    Returns:
        dict with keys: dry_run, status, ticker, yes_price, count, order (if placed)
    """
    count = min(count, MAX_CONTRACTS_PER_ORDER)

    if dry_run:
        msg = (
            f"[DRY RUN] Would place: BUY {count}x {ticker} @ {yes_price}¢"
            f"  (max cost: ${count * yes_price / 100:.2f}  |  max payout: ${count:.0f})"
        )
        logger.info(msg)
        return {
            "dry_run": True,
            "ticker": ticker,
            "yes_price": yes_price,
            "count": count,
            "status": "simulated",
        }

    path = "/trade-api/v2/portfolio/orders"
    url = f"{KALSHI_BASE_URL}/portfolio/orders"

    payload = {
        "ticker": ticker,
        "action": "buy",
        "type": "limit",
        "count": count,
        "yes_price": yes_price,
    }

    try:
        headers = _auth_headers("POST", path)
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            result = response.json()
            logger.info(f"Order placed successfully: {result}")
            return {"dry_run": False, "status": "placed", "order": result}

    except httpx.HTTPStatusError as e:
        logger.error(f"Order failed {e.response.status_code} for {ticker}: {e.response.text}")
        return {
            "dry_run": False,
            "status": "failed",
            "ticker": ticker,
            "error": e.response.text,
        }
    except Exception as e:
        logger.error(f"Order error for {ticker}: {e}")
        return {
            "dry_run": False,
            "status": "error",
            "ticker": ticker,
            "error": str(e),
        }


async def sell_position(
    ticker: str,
    yes_price: int,
    count: int,
    dry_run: bool = True,
) -> dict:
    """
    Sell a YES position on Kalshi (market sell at given price).

    Args:
        ticker:    Kalshi market ticker
        yes_price: Sell price in cents (1–99)
        count:     Number of contracts to sell
        dry_run:   If True, log the intended sell without executing
    """
    count = min(count, MAX_CONTRACTS_PER_ORDER)

    if dry_run:
        logger.info(
            f"[DRY RUN] Would sell: SELL {count}x {ticker} @ {yes_price}¢"
        )
        return {
            "dry_run": True,
            "ticker": ticker,
            "yes_price": yes_price,
            "count": count,
            "status": "simulated",
            "action": "sell",
        }

    path = "/trade-api/v2/portfolio/orders"
    url = f"{KALSHI_BASE_URL}/portfolio/orders"

    payload = {
        "ticker": ticker,
        "action": "sell",
        "type": "limit",
        "count": count,
        "yes_price": yes_price,
    }

    try:
        headers = _auth_headers("POST", path)
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            result = response.json()
            logger.info(f"Sell order placed: {result}")
            return {"dry_run": False, "status": "placed", "action": "sell", "order": result}

    except httpx.HTTPStatusError as e:
        logger.error(f"Sell failed {e.response.status_code} for {ticker}: {e.response.text}")
        return {"dry_run": False, "status": "failed", "action": "sell", "ticker": ticker, "error": e.response.text}
    except Exception as e:
        logger.error(f"Sell error for {ticker}: {e}")
        return {"dry_run": False, "status": "error", "action": "sell", "ticker": ticker, "error": str(e)}


async def fetch_positions() -> list[dict]:
    """
    Fetch all open positions from Kalshi portfolio.
    Returns list of position dicts with ticker, count, average price, etc.
    """
    path = "/trade-api/v2/portfolio/positions"
    url = f"{KALSHI_BASE_URL}/portfolio/positions"

    try:
        headers = _auth_headers("GET", path)
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            positions = data.get("market_positions", [])
            logger.info(f"Fetched {len(positions)} open positions")
            return positions

    except Exception as e:
        logger.error(f"Failed to fetch positions: {e}")
        return []


async def fetch_market_price(ticker: str) -> dict | None:
    """Fetch current market data for a single ticker."""
    path = f"/trade-api/v2/markets/{ticker}"
    url = f"{KALSHI_BASE_URL}/markets/{ticker}"

    try:
        headers = _auth_headers("GET", path)
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json().get("market", {})
    except Exception as e:
        logger.error(f"Failed to fetch market {ticker}: {e}")
        return None


async def fetch_event_markets(event_ticker: str) -> list[dict]:
    """Fetch all markets for an event (to find the underdog's ticker)."""
    path = "/trade-api/v2/markets"
    url = f"{KALSHI_BASE_URL}/markets"

    try:
        headers = _auth_headers("GET", path)
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                url, headers=headers,
                params={"event_ticker": event_ticker, "limit": 10},
            )
            response.raise_for_status()
            return response.json().get("markets", [])
    except Exception as e:
        logger.error(f"Failed to fetch event markets {event_ticker}: {e}")
        return []
