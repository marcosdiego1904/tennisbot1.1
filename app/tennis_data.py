"""
Tennis data client — fetches rankings and tournament info from API-Sports.

API-Sports Tennis: https://api-sports.io/documentation/tennis/v1
Free tier: 100 requests/day.
Endpoints used:
  - /rankings (ATP/WTA rankings)
  - /seasons (available seasons)
"""

import os
import json
import httpx
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

API_SPORTS_KEY = os.getenv("API_SPORTS_KEY", "")
API_SPORTS_BASE = "https://v1.tennis.api-sports.io"

DATA_DIR = Path(__file__).parent.parent / "data"
RANKINGS_CACHE = DATA_DIR / "rankings_cache.json"
CACHE_TTL_HOURS = 24  # rankings update weekly, cache for a day


def _headers() -> dict:
    return {
        "x-apisports-key": API_SPORTS_KEY,
    }


async def fetch_rankings(force_refresh: bool = False) -> dict[str, int]:
    """
    Fetch ATP + WTA rankings. Returns dict of {player_name_lower: ranking}.
    Uses local cache to avoid burning API calls.
    """
    # Check cache first
    if not force_refresh and _cache_is_valid():
        return _load_cache()

    rankings = {}

    async with httpx.AsyncClient() as client:
        # Fetch ATP rankings
        atp_rankings = await _fetch_ranking_list(client)
        rankings.update(atp_rankings)

    # Save cache
    _save_cache(rankings)
    return rankings


async def _fetch_ranking_list(client: httpx.AsyncClient) -> dict[str, int]:
    """Fetch rankings from API-Sports."""
    rankings = {}

    try:
        resp = await client.get(
            f"{API_SPORTS_BASE}/rankings",
            headers=_headers(),
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()

        results = data.get("response", [])
        for entry in results:
            # API-Sports returns rankings in a nested structure
            player = entry.get("player", {})
            name = player.get("name", "")
            ranking = entry.get("position", entry.get("ranking"))

            if name and ranking:
                # Store by last name (lowercase) for matching with Kalshi
                last_name = name.split()[-1].lower() if name else ""
                full_name = name.lower()
                if last_name:
                    rankings[last_name] = int(ranking)
                if full_name:
                    rankings[full_name] = int(ranking)

    except (httpx.HTTPError, KeyError, ValueError) as e:
        print(f"Warning: Could not fetch rankings from API-Sports: {e}")
        # Fall back to cache even if expired
        if RANKINGS_CACHE.exists():
            return _load_cache()

    return rankings


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
    Load tournament database (tournament name → level + surface).
    This is a static JSON file we maintain.
    """
    path = DATA_DIR / "tournaments.json"
    if path.exists():
        return json.loads(path.read_text())
    return {}
