"""
Tennis data client — fetches rankings and tournament info from api-tennis.com

API docs: https://api-tennis.com/documentation
Base URL: https://api.api-tennis.com/tennis/
Auth: API key as query parameter (?APIkey=YOUR_KEY)
Endpoints used:
  - get_standings  (ATP/WTA rankings)
  - get_fixtures   (upcoming matches)
"""

import os
import json
import httpx
from pathlib import Path
from datetime import datetime, timedelta

API_TENNIS_KEY = os.getenv("API_TENNIS_KEY", "")
API_TENNIS_BASE = "https://api.api-tennis.com/tennis/"

DATA_DIR = Path(__file__).parent.parent / "data"
RANKINGS_CACHE = DATA_DIR / "rankings_cache.json"
CACHE_TTL_HOURS = 24  # rankings update weekly, cache for a day


def _base_params() -> dict:
    return {"APIkey": API_TENNIS_KEY}


async def fetch_rankings(force_refresh: bool = False) -> dict[str, int]:
    """
    Fetch ATP + WTA rankings. Returns dict of {player_name_lower: ranking}.
    Uses local cache to avoid burning API calls.
    """
    if not force_refresh and _cache_is_valid():
        return _load_cache()

    rankings = {}

    async with httpx.AsyncClient() as client:
        # Fetch both ATP and WTA
        for event_type in ["ATP", "WTA"]:
            result = await _fetch_standings(client, event_type)
            rankings.update(result)

    _save_cache(rankings)
    return rankings


async def _fetch_standings(client: httpx.AsyncClient, event_type: str) -> dict[str, int]:
    """Fetch rankings from api-tennis.com using get_standings."""
    rankings = {}

    try:
        params = _base_params()
        params["method"] = "get_standings"
        params["event_type"] = event_type

        resp = await client.get(
            API_TENNIS_BASE,
            params=params,
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()

        results = data.get("result", [])
        for entry in results:
            name = entry.get("player", "")
            ranking = entry.get("place", None)

            if not name or not ranking:
                continue

            try:
                rank_int = int(ranking)
            except (ValueError, TypeError):
                continue

            # Store by last name and full name (lowercase) for matching
            full_name = name.strip().lower()
            last_name = full_name.split()[-1] if full_name else ""

            if full_name:
                rankings[full_name] = rank_int
            if last_name:
                rankings[last_name] = rank_int

    except (httpx.HTTPError, KeyError, ValueError) as e:
        print(f"Warning: Could not fetch {event_type} rankings from api-tennis.com: {e}")
        if RANKINGS_CACHE.exists():
            return _load_cache()

    return rankings


async def fetch_fixtures(date_start: str, date_stop: str) -> list[dict]:
    """
    Fetch upcoming matches from api-tennis.com.
    Dates in yyyy-mm-dd format.
    Returns raw fixture list.
    """
    async with httpx.AsyncClient() as client:
        params = _base_params()
        params["method"] = "get_fixtures"
        params["date_start"] = date_start
        params["date_stop"] = date_stop

        try:
            resp = await client.get(
                API_TENNIS_BASE,
                params=params,
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("result", [])
        except (httpx.HTTPError, ValueError) as e:
            print(f"Warning: Could not fetch fixtures from api-tennis.com: {e}")
            return []


# --- Cache helpers ---

def _cache_is_valid() -> bool:
    if not RANKINGS_CACHE.exists():
        return False
    try:
        data = json.loads(RANKINGS_CACHE.read_text())
        cached_at = datetime.fromisoformat(data.get("cached_at", ""))
        return datetime.now() - cached_at < timedelta(hours=CACHE_TTL_HOURS)
    except (json.JSONDecodeError, ValueError):
        return False


def _load_cache() -> dict[str, int]:
    try:
        data = json.loads(RANKINGS_CACHE.read_text())
        return data.get("rankings", {})
    except (json.JSONDecodeError, FileNotFoundError):
        return {}


def _save_cache(rankings: dict[str, int]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    data = {
        "cached_at": datetime.now().isoformat(),
        "rankings": rankings,
    }
    RANKINGS_CACHE.write_text(json.dumps(data, indent=2))


def load_tournament_db() -> dict:
    """
    Load tournament database (tournament name -> level + surface).
    Static JSON file we maintain.
    """
    path = DATA_DIR / "tournaments.json"
    if path.exists():
        return json.loads(path.read_text())
    return {}
