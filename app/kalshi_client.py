"""
Kalshi API client — fetches live tennis markets and prices.

Kalshi v2 API docs: https://docs.kalshi.com
Authentication: RSA-PSS signature with SHA256.
Headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE, KALSHI-ACCESS-TIMESTAMP
"""

import os
import base64
import datetime
import httpx
from typing import Optional
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from app.models import MatchData, PlayerInfo, TournamentLevel, Surface


KALSHI_BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
KALSHI_API_KEY = os.getenv("KALSHI_API_KEY", "")
KALSHI_API_SECRET = os.getenv("KALSHI_API_SECRET", "")

_private_key = None


def _load_private_key():
    """Load RSA private key from the KALSHI_API_SECRET env var."""
    global _private_key
    if _private_key is not None:
        return _private_key

    secret = KALSHI_API_SECRET
    if not secret:
        return None

    # The env var contains the PEM key content directly
    # Railway stores multiline env vars; handle escaped newlines
    key_data = secret.replace("\\n", "\n").encode("utf-8")
    _private_key = serialization.load_pem_private_key(key_data, password=None)
    return _private_key


def _sign_request(method: str, path: str, timestamp: str) -> str:
    """
    Sign a Kalshi API request using RSA-PSS with SHA256.
    Message format: {timestamp}{method}{path_without_query}
    """
    private_key = _load_private_key()
    if not private_key:
        raise ValueError("Kalshi private key not configured")

    # Strip query params for signing
    path_without_query = path.split("?")[0]
    message = f"{timestamp}{method}{path_without_query}".encode("utf-8")

    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )

    return base64.b64encode(signature).decode("utf-8")


def _auth_headers(method: str, path: str) -> dict:
    """Build authenticated headers for a Kalshi API request."""
    timestamp = str(int(datetime.datetime.now().timestamp() * 1000))
    signature = _sign_request(method, path, timestamp)

    return {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json",
    }


async def _kalshi_get(client: httpx.AsyncClient, path: str, params: dict = None) -> dict:
    """Make an authenticated GET request to Kalshi API."""
    # Build full URL with params for the actual request
    url = f"{KALSHI_BASE_URL}{path}"
    headers = _auth_headers("GET", f"/trade-api/v2{path}")

    resp = await client.get(url, headers=headers, params=params, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


async def get_tennis_events(client: httpx.AsyncClient) -> list[dict]:
    """
    Fetch tennis-related events from Kalshi.
    Kalshi uses specific series tickers for tennis:
      - KXATPMATCH: ATP tour matches
      - KXWTAMATCH: WTA tour matches
    We also search for challenger-level events.
    """
    TENNIS_SERIES = [
        "KXATPMATCH",
        "KXWTAMATCH",
        "KXATPCHALLENGER",
    ]

    all_events = []

    for series in TENNIS_SERIES:
        try:
            data = await _kalshi_get(client, "/events", params={
                "status": "open",
                "series_ticker": series,
                "limit": 100,
            })
            events = data.get("events", [])
            all_events.extend(events)
        except httpx.HTTPStatusError:
            # Series might not exist, skip
            continue

    # Fallback: if no events found via series, do a broad keyword search
    if not all_events:
        try:
            data = await _kalshi_get(client, "/events", params={
                "status": "open",
                "limit": 200,
            })
            broad_events = data.get("events", [])
            all_events = [
                e for e in broad_events
                if any(kw in (e.get("title", "") + " " + e.get("event_ticker", "")).lower()
                       for kw in ["tennis", "atp", "wta", "kxatpmatch", "kxwtamatch"])
            ]
        except httpx.HTTPStatusError:
            pass

    return all_events


async def get_markets_for_event(client: httpx.AsyncClient, event_ticker: str) -> list[dict]:
    """Fetch all markets under a specific event."""
    data = await _kalshi_get(client, "/markets", params={
        "event_ticker": event_ticker,
        "limit": 50,
    })
    return data.get("markets", [])


async def get_market(client: httpx.AsyncClient, ticker: str) -> dict:
    """Fetch a single market by ticker."""
    data = await _kalshi_get(client, f"/markets/{ticker}")
    return data.get("market", {})


async def fetch_tennis_markets(
    rankings: Optional[dict] = None,
    tournament_db: Optional[dict] = None,
) -> list[MatchData]:
    """
    Main entry point: fetch all open tennis markets from Kalshi,
    enrich with rankings and tournament data, return as MatchData list.
    """
    rankings = rankings or {}
    tournament_db = tournament_db or {}

    async with httpx.AsyncClient() as client:
        events = await get_tennis_events(client)
        matches = []

        for event in events:
            event_ticker = event.get("event_ticker", "")

            markets = await get_markets_for_event(client, event_ticker)

            for market in markets:
                match = _parse_market(market, event, rankings, tournament_db)
                if match:
                    matches.append(match)

        return matches


def _parse_market(
    market: dict,
    event: dict,
    rankings: dict,
    tournament_db: dict,
) -> Optional[MatchData]:
    """
    Parse a Kalshi market into our MatchData format.
    Kalshi tennis markets typically have:
      - title like "Will Rublev beat Bublik?"
      - yes_price / no_price
      - volume
    """
    title = market.get("title", "")
    subtitle = market.get("subtitle", "")
    event_title = event.get("title", "")

    # Extract yes price (the favorite's implied probability)
    yes_price = market.get("yes_price", 0)  # in cents (0-100)
    no_price = market.get("no_price", 0)
    volume = market.get("volume", 0)

    if yes_price == 0:
        return None

    # Determine who is favorite based on the yes price
    # In Kalshi tennis markets, YES typically = the named player wins
    fav_prob = yes_price / 100.0

    # Try to extract player names from title
    players = _extract_players(title, event_title)
    if not players:
        return None

    fav_name, dog_name = players

    # If yes_price < 50, the "yes" player is actually the underdog
    if fav_prob < 0.50:
        fav_prob = no_price / 100.0
        fav_name, dog_name = dog_name, fav_name
        yes_price = no_price

    # Look up rankings
    fav_ranking = rankings.get(fav_name.lower())
    dog_ranking = rankings.get(dog_name.lower())

    # Determine tournament level and surface
    tournament_level, surface, tournament_name = _classify_tournament(
        event_title, tournament_db
    )

    return MatchData(
        player_fav=PlayerInfo(name=fav_name, ranking=fav_ranking),
        player_dog=PlayerInfo(name=dog_name, ranking=dog_ranking),
        fav_probability=fav_prob,
        kalshi_price=yes_price,
        tournament_name=tournament_name,
        tournament_level=tournament_level,
        surface=surface,
        volume=volume,
        kalshi_ticker=market.get("ticker"),
        kalshi_event_ticker=event.get("event_ticker"),
    )


def _extract_players(market_title: str, event_title: str) -> Optional[tuple[str, str]]:
    """
    Extract player names from Kalshi market title.
    Common formats:
      - "Will Rublev beat Bublik?"
      - "Rublev vs Bublik"
      - "Rublev vs. Bublik"
    """
    text = market_title

    # Pattern: "Will X beat Y?"
    if "will " in text.lower() and " beat " in text.lower():
        text_clean = text.replace("?", "").strip()
        parts = text_clean.lower().split("will ", 1)
        if len(parts) == 2:
            remainder = parts[1]
            beat_parts = remainder.split(" beat ", 1)
            if len(beat_parts) == 2:
                p1 = beat_parts[0].strip().title()
                p2 = beat_parts[1].strip().title()
                return (p1, p2)

    # Pattern: "X vs Y" or "X vs. Y"
    for sep in [" vs. ", " vs "]:
        if sep in text.lower():
            idx = text.lower().index(sep)
            p1 = text[:idx].strip().title()
            p2 = text[idx + len(sep):].strip().rstrip("?").title()
            if p1 and p2:
                return (p1, p2)

    return None


def _classify_tournament(
    event_title: str,
    tournament_db: dict,
) -> tuple[TournamentLevel, Surface, str]:
    """
    Classify tournament from event title.
    Uses the tournament_db for known tournaments,
    falls back to keyword matching.
    """
    title_lower = event_title.lower()

    # Check tournament_db first
    for name, info in tournament_db.items():
        if name.lower() in title_lower:
            return (
                TournamentLevel(info.get("level", "ATP")),
                Surface(info.get("surface", "Hard")),
                name,
            )

    # Keyword fallback
    level = TournamentLevel.ATP
    if "challenger" in title_lower:
        level = TournamentLevel.CHALLENGER
    elif "grand slam" in title_lower or any(
        gs in title_lower
        for gs in ["australian open", "roland garros", "french open", "wimbledon", "us open"]
    ):
        level = TournamentLevel.GRAND_SLAM
    elif "wta" in title_lower:
        level = TournamentLevel.WTA

    surface = Surface.HARD
    if "clay" in title_lower or "roland garros" in title_lower or "french open" in title_lower:
        surface = Surface.CLAY
    elif "grass" in title_lower or "wimbledon" in title_lower:
        surface = Surface.GRASS

    return (level, surface, event_title)
