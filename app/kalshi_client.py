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

# All known Kalshi tennis series tickers
TENNIS_SERIES = [
    "KXATPMATCH",
    "KXWTAMATCH",
    "KXATCHMATCH",
]

_private_key = None


def _load_private_key():
    """Load RSA private key from the KALSHI_API_SECRET env var."""
    global _private_key
    if _private_key is not None:
        return _private_key

    secret = KALSHI_API_SECRET
    if not secret:
        return None

    key_data = secret.replace("\\n", "\n").encode("utf-8")
    _private_key = serialization.load_pem_private_key(key_data, password=None)
    return _private_key


def _sign_request(method: str, path: str, timestamp: str) -> str:
    """Sign a Kalshi API request using RSA-PSS with SHA256."""
    private_key = _load_private_key()
    if not private_key:
        raise ValueError("Kalshi private key not configured")

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
    url = f"{KALSHI_BASE_URL}{path}"
    headers = _auth_headers("GET", f"/trade-api/v2{path}")

    resp = await client.get(url, headers=headers, params=params, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


async def fetch_tennis_markets(
    rankings: Optional[dict] = None,
    tournament_db: Optional[dict] = None,
) -> list[MatchData]:
    """
    Main entry point: fetch all open tennis markets from Kalshi.
    Strategy: fetch markets directly by series ticker (more reliable than events→markets).
    """
    rankings = rankings or {}
    tournament_db = tournament_db or {}

    async with httpx.AsyncClient() as client:
        all_raw_markets = []

        # Strategy 1: fetch markets directly by series ticker
        for series in TENNIS_SERIES:
            try:
                data = await _kalshi_get(client, "/markets", params={
                    "status": "open",
                    "series_ticker": series,
                    "limit": 100,
                })
                markets = data.get("markets", [])
                all_raw_markets.extend(markets)
            except httpx.HTTPStatusError:
                continue

        # Strategy 2: if nothing found, broad search via events
        if not all_raw_markets:
            try:
                data = await _kalshi_get(client, "/events", params={
                    "status": "open",
                    "limit": 200,
                })
                all_events = data.get("events", [])
                tennis_events = [
                    e for e in all_events
                    if any(kw in (e.get("title", "") + " " + e.get("event_ticker", "")).lower()
                           for kw in ["tennis", "atp", "wta", "challenger"])
                ]
                for event in tennis_events:
                    event_ticker = event.get("event_ticker", "")
                    try:
                        mdata = await _kalshi_get(client, "/markets", params={
                            "event_ticker": event_ticker,
                            "limit": 50,
                        })
                        all_raw_markets.extend(mdata.get("markets", []))
                    except httpx.HTTPStatusError:
                        continue
            except httpx.HTTPStatusError:
                pass

        # Parse all raw markets into MatchData
        matches = []
        for market in all_raw_markets:
            match = _parse_market(market, rankings, tournament_db)
            if match:
                matches.append(match)

        return matches


async def debug_fetch(client: httpx.AsyncClient) -> dict:
    """
    Debug helper: returns raw data at each step so we can see
    exactly what Kalshi returns and where parsing fails.
    """
    debug_info = {
        "series_tried": [],
        "raw_markets_found": 0,
        "full_market_dump": None,
        "parsed_ok": 0,
        "parse_failures": [],
    }

    all_raw = []

    # Try many possible series tickers for tennis
    all_series_to_try = TENNIS_SERIES + [
        "KXATPCHALLENGER",
        "KXATCHALL",
        "KXATCHCHAL",
    ]

    for series in all_series_to_try:
        entry = {"series": series}
        try:
            data = await _kalshi_get(client, "/markets", params={
                "status": "open",
                "series_ticker": series,
                "limit": 20,
            })
            markets = data.get("markets", [])
            entry["count"] = len(markets)
            if markets:
                # Show first market sample
                entry["first_title"] = markets[0].get("title", "")
            all_raw.extend(markets)
        except Exception as e:
            entry["error"] = str(e)
        debug_info["series_tried"].append(entry)

    # Dump ALL fields from first market so we can see what's available
    if all_raw:
        debug_info["full_market_dump"] = all_raw[0]

    # Try fetching with different statuses to find live matches
    for status_val in ["active", "open", "trading"]:
        try:
            data = await _kalshi_get(client, "/markets", params={
                "status": status_val,
                "series_ticker": "KXATPMATCH",
                "limit": 5,
            })
            markets = data.get("markets", [])
            has_prices = [m for m in markets if m.get("yes_price") or m.get("last_price") or m.get("yes_bid")]
            debug_info[f"status_{status_val}"] = {
                "count": len(markets),
                "with_prices": len(has_prices),
                "sample_fields": list(markets[0].keys()) if markets else [],
            }
        except Exception as e:
            debug_info[f"status_{status_val}"] = {"error": str(e)}

    # Search events broadly — try to find challenger events
    try:
        # Get ALL events with cursor pagination isn't possible in one call
        # But let's try to find tennis specifically
        for series_q in ["KXATPMATCH", "KXWTAMATCH"]:
            data = await _kalshi_get(client, "/events", params={
                "status": "open",
                "series_ticker": series_q,
                "limit": 5,
            })
            events = data.get("events", [])
            if events:
                debug_info[f"events_{series_q}"] = [
                    {"ticker": e.get("event_ticker"), "title": e.get("title"), "series": e.get("series_ticker")}
                    for e in events[:3]
                ]
    except Exception as e:
        debug_info["events_search_error"] = str(e)

    debug_info["raw_markets_found"] = len(all_raw)

    # Show parse attempts
    for m in all_raw[:5]:
        result = _parse_market(m, {}, {})
        if result:
            debug_info["parsed_ok"] += 1
        else:
            debug_info["parse_failures"].append({
                "ticker": m.get("ticker"),
                "title": m.get("title"),
                "yes_price": m.get("yes_price"),
                "last_price": m.get("last_price"),
                "yes_bid": m.get("yes_bid"),
                "yes_ask": m.get("yes_ask"),
                "reason": _debug_parse_failure(m),
            })

    return debug_info


def _debug_parse_failure(market: dict) -> str:
    """Explain why a market failed to parse."""
    title = market.get("title", "")
    subtitle = market.get("subtitle", "")
    yes_price = market.get("yes_price", 0)

    if yes_price == 0:
        return "yes_price is 0"

    players = _extract_players(title, subtitle)
    if not players:
        return f"Could not extract players from title='{title}', subtitle='{subtitle}'"

    return "Unknown"


def _parse_market(
    market: dict,
    rankings: dict,
    tournament_db: dict,
) -> Optional[MatchData]:
    """
    Parse a Kalshi market into our MatchData format.
    Tries multiple strategies to extract player names and match info.
    """
    title = market.get("title", "")
    subtitle = market.get("subtitle", "")
    event_ticker = market.get("event_ticker", "")

    # Extract yes price (the favorite's implied probability)
    yes_price = market.get("yes_price", 0)  # in cents (0-100)
    no_price = market.get("no_price", 0)
    volume = market.get("volume", 0)

    if yes_price == 0 and no_price == 0:
        return None

    fav_prob = yes_price / 100.0

    # Try to extract player names — try title first, then subtitle, then event_ticker
    players = _extract_players(title, subtitle)
    if not players:
        players = _extract_players(subtitle, title)
    if not players:
        # Try to extract from event_ticker (e.g., KXATPMATCH-26FEB06AUGFIL)
        players = _extract_players_from_ticker(event_ticker)

    if not players:
        return None

    fav_name, dog_name = players

    # If yes_price < 50, the "yes" player is actually the underdog
    if fav_prob < 0.50:
        fav_prob = no_price / 100.0 if no_price > 0 else 1.0 - fav_prob
        fav_name, dog_name = dog_name, fav_name
        yes_price = no_price if no_price > 0 else (100 - yes_price)

    # Look up rankings — try last name and full name
    fav_ranking = _lookup_ranking(fav_name, rankings)
    dog_ranking = _lookup_ranking(dog_name, rankings)

    # Determine tournament level and surface
    all_text = f"{title} {subtitle} {event_ticker}"
    tournament_level, surface, tournament_name = _classify_tournament(
        all_text, tournament_db
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
        kalshi_event_ticker=event_ticker,
    )


def _extract_players(text1: str, text2: str) -> Optional[tuple[str, str]]:
    """
    Extract player names from market text.
    Handles multiple Kalshi title formats:
      - "Will Rublev beat Bublik?"
      - "Rublev vs Bublik"
      - "Rublev vs. Bublik"
      - "Hugo Dellien vs Juan Manuel La Serna"
    """
    # Try both texts
    for text in [text1, text2]:
        if not text:
            continue

        # Pattern: "Will X beat Y?"
        t = text.lower()
        if "will " in t and " beat " in t:
            text_clean = text.replace("?", "").strip()
            parts = text_clean.lower().split("will ", 1)
            if len(parts) == 2:
                beat_parts = parts[1].split(" beat ", 1)
                if len(beat_parts) == 2:
                    p1 = beat_parts[0].strip().title()
                    p2 = beat_parts[1].strip().title()
                    if p1 and p2:
                        return (p1, p2)

        # Pattern: "X vs Y" or "X vs. Y"
        for sep in [" vs. ", " vs "]:
            if sep in text.lower():
                idx = text.lower().index(sep)
                p1 = text[:idx].strip().title()
                p2 = text[idx + len(sep):].strip().rstrip("?").title()
                if p1 and p2:
                    return (p1, p2)

        # Pattern: "X v Y"
        if " v " in text.lower():
            idx = text.lower().index(" v ")
            p1 = text[:idx].strip().title()
            p2 = text[idx + 3:].strip().rstrip("?").title()
            if p1 and p2:
                return (p1, p2)

    return None


def _extract_players_from_ticker(ticker: str) -> Optional[tuple[str, str]]:
    """
    Last resort: try to extract player abbreviations from event ticker.
    e.g., KXATPMATCH-26FEB06AUGFIL → can't get full names, but flags it exists.
    """
    # This is unreliable — return None so we skip rather than show garbage
    return None


def _lookup_ranking(player_name: str, rankings: dict) -> Optional[int]:
    """Look up a player's ranking by trying multiple name formats."""
    if not rankings:
        return None

    name_lower = player_name.lower()

    # Try full name
    if name_lower in rankings:
        return rankings[name_lower]

    # Try last name only
    parts = name_lower.split()
    if parts:
        last_name = parts[-1]
        if last_name in rankings:
            return rankings[last_name]

    return None


def _classify_tournament(
    text: str,
    tournament_db: dict,
) -> tuple[TournamentLevel, Surface, str]:
    """
    Classify tournament from any available text.
    Uses the tournament_db for known tournaments,
    falls back to keyword matching.
    """
    title_lower = text.lower()

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

    # Extract a clean tournament name
    tournament_name = text.split(" - ")[0] if " - " in text else text[:50]

    return (level, surface, tournament_name.strip())
