"""
Kalshi API client — fetches live tennis markets and prices.

Kalshi v2 API docs: https://docs.kalshi.com
Authentication: RSA-PSS signature with SHA256.
Headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE, KALSHI-ACCESS-TIMESTAMP

Kalshi market price fields:
  - yes_bid / yes_ask: current order book (what you can buy/sell at)
  - last_price: last traded price
  - no_bid / no_ask: inverse side
  - volume: total traded volume
"""

import os
import re
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


async def _kalshi_get_all(client: httpx.AsyncClient, path: str, params: dict, key: str = "markets") -> list[dict]:
    """Paginated fetch — gets all results using cursor."""
    all_items = []
    cursor = None

    for _ in range(10):  # max 10 pages
        p = dict(params)
        if cursor:
            p["cursor"] = cursor

        data = await _kalshi_get(client, path, params=p)
        items = data.get(key, [])
        all_items.extend(items)

        cursor = data.get("cursor")
        if not cursor or len(items) == 0:
            break

    return all_items


def _get_market_price(market: dict) -> Optional[int]:
    """
    Extract the best available price from a Kalshi market.
    Returns price in cents (1-99) or None if no price available.

    Priority:
    1. last_price (if traded)
    2. midpoint of yes_bid and yes_ask (if both exist)
    3. yes_ask (the price you'd buy at)
    """
    last = market.get("last_price")
    if last and last > 0:
        return int(last)

    yes_bid = market.get("yes_bid")
    yes_ask = market.get("yes_ask")

    if yes_bid and yes_ask and yes_bid > 0 and yes_ask > 0:
        return int((yes_bid + yes_ask) / 2)

    if yes_ask and yes_ask > 0:
        return int(yes_ask)

    if yes_bid and yes_bid > 0:
        return int(yes_bid)

    return None


async def fetch_tennis_markets(
    rankings: Optional[dict] = None,
    tournament_db: Optional[dict] = None,
) -> list[MatchData]:
    """
    Main entry point: fetch all open tennis markets from Kalshi.
    Uses paginated fetch to get ALL markets, not just first 20.
    """
    rankings = rankings or {}
    tournament_db = tournament_db or {}

    async with httpx.AsyncClient() as client:
        all_raw_markets = []

        for series in TENNIS_SERIES:
            try:
                markets = await _kalshi_get_all(client, "/markets", params={
                    "status": "open",
                    "series_ticker": series,
                    "limit": 100,
                })
                all_raw_markets.extend(markets)
            except httpx.HTTPStatusError:
                continue

        # Deduplicate by event_ticker (each event has 2 markets: YES player A, YES player B)
        # We only need one per event — pick the one with the higher yes price (the favorite)
        events_seen = {}
        for market in all_raw_markets:
            event_ticker = market.get("event_ticker", "")
            price = _get_market_price(market) or 0

            if event_ticker not in events_seen or price > (_get_market_price(events_seen[event_ticker]) or 0):
                events_seen[event_ticker] = market

        # Parse unique markets
        matches = []
        for market in events_seen.values():
            match = _parse_market(market, rankings, tournament_db)
            if match:
                matches.append(match)

        return matches


async def debug_fetch(client: httpx.AsyncClient) -> dict:
    """Debug helper: returns raw data at each step."""
    debug_info = {
        "series_tried": [],
        "raw_markets_found": 0,
        "full_market_dump": None,
        "parsed_ok": 0,
        "parsed_matches": [],
        "parse_failures": [],
    }

    all_raw = []

    for series in TENNIS_SERIES:
        entry = {"series": series}
        try:
            markets = await _kalshi_get_all(client, "/markets", params={
                "status": "open",
                "series_ticker": series,
                "limit": 100,
            })
            entry["count"] = len(markets)

            # Count markets with actual prices
            with_prices = [m for m in markets if _get_market_price(m) is not None]
            entry["with_prices"] = len(with_prices)

            if with_prices:
                m = with_prices[0]
                entry["sample_with_price"] = {
                    "ticker": m.get("ticker"),
                    "title": m.get("title"),
                    "last_price": m.get("last_price"),
                    "yes_bid": m.get("yes_bid"),
                    "yes_ask": m.get("yes_ask"),
                    "volume": m.get("volume"),
                    "rules": m.get("rules_primary", "")[:200],
                }
            if markets:
                entry["first_title"] = markets[0].get("title", "")

            all_raw.extend(markets)
        except Exception as e:
            entry["error"] = str(e)
        debug_info["series_tried"].append(entry)

    if all_raw:
        debug_info["full_market_dump"] = all_raw[0]

    debug_info["raw_markets_found"] = len(all_raw)

    # Try parsing and show results
    for m in all_raw[:20]:
        price = _get_market_price(m)
        result = _parse_market(m, {}, {})
        if result:
            debug_info["parsed_ok"] += 1
            debug_info["parsed_matches"].append({
                "fav": result.player_fav.name,
                "dog": result.player_dog.name,
                "price": result.kalshi_price,
                "fav_pct": round(result.fav_probability * 100, 1),
                "tournament": result.tournament_name,
                "level": result.tournament_level.value,
            })
        else:
            debug_info["parse_failures"].append({
                "ticker": m.get("ticker"),
                "title": m.get("title"),
                "price": price,
                "last_price": m.get("last_price"),
                "yes_bid": m.get("yes_bid"),
                "yes_ask": m.get("yes_ask"),
                "reason": _debug_parse_failure(m),
            })

    return debug_info


def _debug_parse_failure(market: dict) -> str:
    """Explain why a market failed to parse."""
    price = _get_market_price(market)
    if price is None:
        return f"No price (last={market.get('last_price')}, bid={market.get('yes_bid')}, ask={market.get('yes_ask')})"

    title = market.get("title", "")
    rules = market.get("rules_primary", "")
    players = _extract_players_from_title(title)
    if not players:
        players = _extract_players_from_rules(rules)
    if not players:
        return f"Could not extract players. title='{title}'"

    return "Unknown"


def _parse_market(
    market: dict,
    rankings: dict,
    tournament_db: dict,
) -> Optional[MatchData]:
    """
    Parse a Kalshi market into our MatchData format.
    Uses yes_bid/yes_ask/last_price for pricing.
    Extracts players from title and rules_primary.
    """
    event_ticker = market.get("event_ticker", "")
    title = market.get("title", "")
    rules = market.get("rules_primary", "")
    volume = market.get("volume", 0) or 0

    # Get price
    price = _get_market_price(market)
    if price is None:
        return None

    # The "yes" player is named in yes_sub_title or extracted from title
    yes_player = market.get("yes_sub_title", "")

    # Extract both players from the title or rules
    players = _extract_players_from_title(title)
    if not players:
        players = _extract_players_from_rules(rules)
    if not players:
        return None

    player_a, player_b = players

    # Determine who the YES player is
    fav_prob = price / 100.0

    if fav_prob >= 0.50:
        # YES player is the favorite
        fav_name = yes_player if yes_player else player_a
        dog_name = player_b if player_a.lower() in (yes_player or "").lower() else player_a
        # Make sure dog != fav
        if fav_name.lower() == dog_name.lower():
            dog_name = player_b if fav_name.lower() == player_a.lower() else player_a
        kalshi_price = price
    else:
        # YES player is the underdog, flip
        fav_prob = 1.0 - fav_prob
        dog_name = yes_player if yes_player else player_a
        fav_name = player_b if player_a.lower() in (yes_player or "").lower() else player_a
        if fav_name.lower() == dog_name.lower():
            fav_name = player_b if dog_name.lower() == player_a.lower() else player_a
        kalshi_price = 100 - price

    # Look up rankings
    fav_ranking = _lookup_ranking(fav_name, rankings)
    dog_ranking = _lookup_ranking(dog_name, rankings)

    # Classify tournament — use rules_primary which has tournament info
    tournament_level, surface, tournament_name = _classify_tournament(
        f"{title} {rules} {event_ticker}", tournament_db
    )

    return MatchData(
        player_fav=PlayerInfo(name=fav_name, ranking=fav_ranking),
        player_dog=PlayerInfo(name=dog_name, ranking=dog_ranking),
        fav_probability=fav_prob,
        kalshi_price=kalshi_price,
        tournament_name=tournament_name,
        tournament_level=tournament_level,
        surface=surface,
        volume=volume,
        kalshi_ticker=market.get("ticker"),
        kalshi_event_ticker=event_ticker,
    )


def _extract_players_from_title(title: str) -> Optional[tuple[str, str]]:
    """
    Extract player names from Kalshi market title.

    Known format:
    "Will Hamad Medjedovic win the Medjedovic vs Basilashvili : Qualification Round 1 match?"

    We extract from the "X vs Y" part embedded in the title.
    """
    if not title:
        return None

    # Find "X vs Y" pattern anywhere in the text (before any colon)
    # e.g., "...the Medjedovic vs Basilashvili : Qualification..."
    match = re.search(r'(\w[\w\s\'-]+?)\s+vs\.?\s+(\w[\w\s\'-]+?)(?:\s*[:\-\?]|\s+match)', title, re.IGNORECASE)
    if match:
        p1 = match.group(1).strip().title()
        p2 = match.group(2).strip().title()
        if p1 and p2:
            return (p1, p2)

    # Simpler fallback: just find "X vs Y" anywhere
    for sep in [" vs. ", " vs "]:
        if sep in title.lower():
            idx = title.lower().index(sep)
            # Go backwards to find start of first name
            before = title[:idx].strip()
            after = title[idx + len(sep):].strip()

            # Clean up: take last 1-3 words before "vs" as player name
            p1_words = before.split()[-3:]  # last 3 words
            p1 = " ".join(p1_words).title()

            # Take first 1-3 words after "vs" as player name (stop at : or ?)
            after_clean = re.split(r'[:\?\-]', after)[0].strip()
            p2_words = after_clean.split()[:3]
            p2 = " ".join(p2_words).title()

            if p1 and p2:
                return (p1, p2)

    return None


def _extract_players_from_rules(rules: str) -> Optional[tuple[str, str]]:
    """
    Extract players from rules_primary field.
    Format: "If X wins the X vs Y professional tennis match in the 2026 ATP..."
    """
    if not rules:
        return None

    match = re.search(r'(\w[\w\s\'-]+?)\s+vs\.?\s+(\w[\w\s\'-]+?)\s+professional', rules, re.IGNORECASE)
    if match:
        p1 = match.group(1).strip().title()
        p2 = match.group(2).strip().title()
        if p1 and p2:
            return (p1, p2)

    return None


def _lookup_ranking(player_name: str, rankings: dict) -> Optional[int]:
    """Look up a player's ranking by trying multiple name formats."""
    if not rankings:
        return None

    name_lower = player_name.lower()

    if name_lower in rankings:
        return rankings[name_lower]

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
    """Classify tournament from any available text."""
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

    # Extract tournament name from rules (e.g., "2026 ATP Rotterdam Qualification")
    match = re.search(r'20\d{2}\s+(ATP|WTA)\s+([\w\s]+?)(?:Qualification|Round|Quarter|Semi|Final|match)', text)
    if match:
        tournament_name = f"{match.group(1)} {match.group(2).strip()}"
    else:
        tournament_name = text.split(" - ")[0] if " - " in text else "Unknown"

    return (level, surface, tournament_name.strip()[:60])
