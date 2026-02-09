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

# Fallback series tickers (used if dynamic discovery fails)
_FALLBACK_SERIES = ["KXATPMATCH", "KXWTAMATCH"]

# Cache for dynamically discovered tennis series tickers
_tennis_series_cache: list[str] = []
_tennis_series_cache_ts: float = 0
_SERIES_CACHE_TTL = 3600  # re-discover every 1 hour

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


async def _discover_tennis_series(client: httpx.AsyncClient) -> list[str]:
    """
    Dynamically discover ALL tennis series tickers from Kalshi.

    Strategy:
    1. GET /search/filters_by_sport — lists sports and their competitions
    2. GET /search/tags_by_categories — find category/tag for tennis
    3. GET /series?category=...&tags=... — fetch all tennis series
    4. Fallback to known tickers if discovery fails

    Results are cached for 1 hour.
    """
    import time
    global _tennis_series_cache, _tennis_series_cache_ts

    now = time.time()
    if _tennis_series_cache and (now - _tennis_series_cache_ts) < _SERIES_CACHE_TTL:
        return _tennis_series_cache

    discovered = set()

    # Step 1: Use /search/filters_by_sport to find tennis competitions
    try:
        data = await _kalshi_get(client, "/search/filters_by_sport")
        filters = data.get("filters_by_sports", {})

        # Match "Tennis" exactly — NOT "Table Tennis"
        tennis_filters = None
        for sport_name, sport_data in filters.items():
            name_lower = sport_name.lower().strip()
            if name_lower == "tennis":
                tennis_filters = sport_data
                break

        if tennis_filters:
            # competitions can be a dict (name -> data) or a list
            competitions = tennis_filters.get("competitions", {})
            if isinstance(competitions, dict):
                for comp_name, comp_data in competitions.items():
                    if isinstance(comp_data, dict):
                        for scope in comp_data.get("scopes", []):
                            if isinstance(scope, str) and scope.startswith("KX"):
                                discovered.add(scope)
            elif isinstance(competitions, list):
                for comp in competitions:
                    if isinstance(comp, dict):
                        for scope in comp.get("scopes", []):
                            if isinstance(scope, str) and scope.startswith("KX"):
                                discovered.add(scope)

            # Also check top-level scopes
            for scope in tennis_filters.get("scopes", []):
                if isinstance(scope, str) and scope.startswith("KX"):
                    discovered.add(scope)
    except Exception:
        pass

    # Step 2: Use /search/tags_by_categories to find the right category/tags for tennis
    tennis_category = None
    tennis_tags = []
    try:
        data = await _kalshi_get(client, "/search/tags_by_categories")
        tags_by_cat = data.get("tags_by_categories", {})

        for cat_name, tags in tags_by_cat.items():
            if not tags or not isinstance(tags, list):
                continue
            for tag in tags:
                if not isinstance(tag, str):
                    continue
                tag_lower = tag.lower().strip()
                # Match "Tennis" but not "Table Tennis"
                if tag_lower == "tennis" or tag_lower.startswith("atp") or tag_lower.startswith("wta"):
                    tennis_category = cat_name
                    tennis_tags.append(tag)
    except Exception:
        pass

    # Step 3: Fetch series using discovered category/tags
    if tennis_tags:
        for tag in tennis_tags:
            try:
                params = {"tags": tag}
                if tennis_category:
                    params["category"] = tennis_category
                data = await _kalshi_get(client, "/series", params=params)
                for s in data.get("series", []):
                    ticker = s.get("ticker", "")
                    if ticker:
                        discovered.add(ticker)
            except Exception:
                pass

    # Step 4: Also try fetching series with category alone if we found one
    if tennis_category and not discovered:
        try:
            data = await _kalshi_get(client, "/series", params={"category": tennis_category})
            for s in data.get("series", []):
                ticker = s.get("ticker", "")
                title = s.get("title", "").lower()
                if ticker and ("tennis" in title or "atp" in title or "wta" in title or "match" in title):
                    discovered.add(ticker)
        except Exception:
            pass

    # Use discovered tickers, or fallback
    if discovered:
        _tennis_series_cache = list(discovered)
    else:
        _tennis_series_cache = list(_FALLBACK_SERIES)

    _tennis_series_cache_ts = now
    return _tennis_series_cache


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
    tournament_db: Optional[dict] = None,
) -> list[MatchData]:
    """
    Main entry point: fetch all open tennis markets from Kalshi.
    Uses paginated fetch to get ALL markets, not just first 20.
    """
    tournament_db = tournament_db or {}

    async with httpx.AsyncClient() as client:
        # Dynamically discover all tennis series tickers
        all_series = await _discover_tennis_series(client)

        # For trading, only fetch match-winner markets (MATCH series)
        # This filters out game markets, futures, field markets, etc.
        match_series = [s for s in all_series if "MATCH" in s]
        if not match_series:
            match_series = list(_FALLBACK_SERIES)

        all_raw_markets = []

        for series in match_series:
            try:
                markets = await _kalshi_get_all(client, "/markets", params={
                    "status": "open",
                    "series_ticker": series,
                    "limit": 100,
                })
                # Tag each market with its series ticker for classification
                for m in markets:
                    m["_series_ticker"] = series
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
            match = _parse_market(market, tournament_db)
            if match:
                matches.append(match)

        return matches


async def debug_fetch(client: httpx.AsyncClient) -> dict:
    """Debug helper: shows discovery results and raw market data."""
    import time
    global _tennis_series_cache_ts

    debug_info = {
        "discovery": {},
        "series_tried": [],
        "raw_markets_found": 0,
        "full_market_dump": None,
        "parsed_ok": 0,
        "parsed_matches": [],
        "parse_failures": [],
    }

    # Step 1: Show raw discovery endpoint responses
    try:
        data = await _kalshi_get(client, "/search/filters_by_sport")
        filters = data.get("filters_by_sports", {})

        # Show ALL sports so we can see what tennis is called
        debug_info["discovery"]["all_sports"] = list(filters.keys())

        # Show tennis-related entries (exact match and partial)
        tennis_entries = {}
        for sport_name, sport_data in filters.items():
            name_lower = sport_name.lower().strip()
            if "tennis" in name_lower:
                tennis_entries[sport_name] = sport_data
        debug_info["discovery"]["filters_by_sport"] = tennis_entries if tennis_entries else "No tennis-related sport found"
    except Exception as e:
        debug_info["discovery"]["filters_by_sport_error"] = str(e)

    try:
        data = await _kalshi_get(client, "/search/tags_by_categories")
        tags_by_cat = data.get("tags_by_categories", {})
        # Show tennis-related tags, safely handling None values
        tennis_info = {}
        all_cats_preview = {}
        for cat, tags in tags_by_cat.items():
            if not tags or not isinstance(tags, list):
                all_cats_preview[cat] = f"({type(tags).__name__})"
                continue
            safe_tags = [t for t in tags if isinstance(t, str)]
            tennis_tags = [t for t in safe_tags if "tennis" in t.lower() or "atp" in t.lower() or "wta" in t.lower() or "challenger" in t.lower()]
            if tennis_tags:
                tennis_info[cat] = tennis_tags
            all_cats_preview[cat] = safe_tags[:5]
        debug_info["discovery"]["tags_by_categories"] = tennis_info if tennis_info else {
            "note": "No tennis tags found",
            "all_categories": all_cats_preview,
        }
    except Exception as e:
        debug_info["discovery"]["tags_by_categories_error"] = str(e)

    # Step 2: Force fresh discovery (bypass cache)
    _tennis_series_cache_ts = 0
    tennis_series = await _discover_tennis_series(client)
    debug_info["discovery"]["discovered_series"] = tennis_series

    # Step 3: Fetch markets for each discovered series
    all_raw = []

    for series in tennis_series:
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
                    "close_time": m.get("close_time"),
                    "expiration_time": m.get("expiration_time"),
                    "expected_expiration_time": m.get("expected_expiration_time"),
                    "open_time": m.get("open_time"),
                    "all_keys": list(m.keys()),
                }
            if markets:
                entry["first_title"] = markets[0].get("title", "")

            all_raw.extend(markets)
        except Exception as e:
            entry["error"] = str(e)
        debug_info["series_tried"].append(entry)

    if all_raw:
        debug_info["full_market_dump"] = all_raw[0]
        # Show all time-related fields from first market
        first = all_raw[0]
        debug_info["time_fields"] = {
            k: v for k, v in first.items()
            if any(t in k.lower() for t in ["time", "date", "expir", "close", "open", "sched"])
        }

    debug_info["raw_markets_found"] = len(all_raw)

    # Try parsing and show results
    for m in all_raw[:100]:
        price = _get_market_price(m)
        result = _parse_market(m, {})
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

    # Extract both player last names from the title "the X vs Y :"
    players = _extract_players_from_title(title)
    if not players:
        players = _extract_players_from_rules(rules)
    if not players:
        return None

    player_a, player_b = players

    # yes_sub_title has the full name of the YES player in this market
    yes_player = market.get("yes_sub_title", "")

    # Figure out which extracted name matches the YES player
    # The YES player's last name should appear in one of player_a or player_b
    yes_is_a = _name_matches(yes_player, player_a)

    # The price is for the YES player winning
    fav_prob = price / 100.0

    if fav_prob >= 0.50:
        # YES player is the favorite
        fav_name = yes_player or player_a
        dog_name = player_b if yes_is_a else player_a
        kalshi_price = price
    else:
        # YES player is the underdog — flip
        fav_prob = 1.0 - fav_prob
        fav_name = player_b if yes_is_a else player_a
        dog_name = yes_player or (player_a if yes_is_a else player_b)
        kalshi_price = 100 - price

    # Classify tournament — use rules_primary + series ticker for tournament info
    series_ticker = market.get("_series_ticker", "")
    tournament_level, surface, tournament_name = _classify_tournament(
        f"{title} {rules} {event_ticker}", tournament_db, series_ticker
    )

    return MatchData(
        player_fav=PlayerInfo(name=fav_name),
        player_dog=PlayerInfo(name=dog_name),
        fav_probability=fav_prob,
        kalshi_price=kalshi_price,
        tournament_name=tournament_name,
        tournament_level=tournament_level,
        surface=surface,
        volume=volume,
        kalshi_ticker=market.get("ticker"),
        kalshi_event_ticker=event_ticker,
        close_time=market.get("expected_expiration_time") or market.get("expiration_time") or market.get("close_time"),
    )


def _extract_players_from_title(title: str) -> Optional[tuple[str, str]]:
    """
    Extract player names from Kalshi market title.

    Known format:
    "Will Hamad Medjedovic win the Medjedovic vs Basilashvili : Qualification Round 1 match?"

    The key is: "the {LastName1} vs {LastName2} :" — always anchored by "the" before and ":" after.
    """
    if not title:
        return None

    # Primary pattern: "the LastName1 vs LastName2 :"
    # Names are 1-3 words, no spaces-greedy issue because we anchor on "the" and ":"
    match = re.search(
        r'\bthe\s+([\w\'-]+(?:\s+[\w\'-]+){0,2})\s+vs\.?\s+([\w\'-]+(?:\s+[\w\'-]+){0,2})\s*[:\-]',
        title, re.IGNORECASE
    )
    if match:
        p1 = match.group(1).strip().title()
        p2 = match.group(2).strip().title()
        if p1 and p2:
            return (p1, p2)

    # Secondary: "the LastName1 vs LastName2 ... match?"
    match = re.search(
        r'\bthe\s+([\w\'-]+(?:\s+[\w\'-]+){0,2})\s+vs\.?\s+([\w\'-]+(?:\s+[\w\'-]+){0,2})\s',
        title, re.IGNORECASE
    )
    if match:
        p1 = match.group(1).strip().title()
        p2 = match.group(2).strip().title()
        if p1 and p2:
            return (p1, p2)

    # Fallback: find "vs" and take last 1-2 words before, first 1-2 words after
    for sep in [" vs. ", " vs "]:
        if sep in title.lower():
            idx = title.lower().index(sep)
            before = title[:idx].strip()
            after = title[idx + len(sep):].strip()

            p1_words = before.split()[-2:]
            p1 = " ".join(p1_words).title()

            after_clean = re.split(r'[:\?\-]', after)[0].strip()
            p2_words = after_clean.split()[:2]
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


def _name_matches(full_name: str, last_name: str) -> bool:
    """Check if a full name matches a last name (case-insensitive)."""
    if not full_name or not last_name:
        return False
    return last_name.lower() in full_name.lower()



def _classify_tournament(
    text: str,
    tournament_db: dict,
    series_ticker: str = "",
) -> tuple[TournamentLevel, Surface, str]:
    """Classify tournament from any available text."""
    title_lower = text.lower()
    ticker_upper = series_ticker.upper()

    # Check tournament_db first
    for name, info in tournament_db.items():
        if name.lower() in title_lower:
            return (
                TournamentLevel(info.get("level", "ATP")),
                Surface(info.get("surface", "Hard")),
                name,
            )

    # Use series ticker for reliable level detection
    # Tickers: KXATPMATCH, KXWTAMATCH, KXATPCHALLENGERMATCH, KXWTACHALLENGERMATCH, etc.
    level = TournamentLevel.ATP
    if "CHALLENGER" in ticker_upper:
        level = TournamentLevel.CHALLENGER
    elif "WTA" in ticker_upper:
        level = TournamentLevel.WTA
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
