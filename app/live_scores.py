"""
Live tennis score client — TennisApi1 via RapidAPI.

Provides real-time match score data for the pivot trade logic.
The bot uses this to confirm whether an underdog is actually winning
before executing a pivot trade (buying the underdog after selling the favorite).

Endpoints used:
  GET /api/tennis/events/live          → all live matches
  GET /api/tennis/event/{id}/point-by-point → detailed score

Configuration (.env):
  TENNISAPI_KEY   Your x-rapidapi-key for tennisapi1.p.rapidapi.com

NOTE: Field names in this module are based on the typical SofaScore/TennisApi1
schema. Run scripts/test_live_scores_api.py to verify the actual field names
and adjust the _parse_* functions if needed.
"""

import os
import time
import logging
import httpx
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

TENNISAPI_HOST = "tennisapi1.p.rapidapi.com"
TENNISAPI_BASE = f"https://{TENNISAPI_HOST}"
TENNISAPI_KEY  = os.getenv("TENNISAPI_KEY", "")

# Cache: avoid hitting the API on every 10s scan for the same match
_score_cache: dict[str, tuple[float, "LiveScore"]] = {}
CACHE_TTL = 15  # seconds — scores are cached for 15s max


@dataclass
class LiveScore:
    """Parsed live score for a tennis match."""
    event_id: str
    status: str                     # "inprogress", "finished", "notstarted"
    home_player: str                # Player 1 name
    away_player: str                # Player 2 name
    home_sets: int                  # Sets won by player 1
    away_sets: int                  # Sets won by player 2
    home_games: int                 # Games in current set for player 1
    away_games: int                 # Games in current set for player 2
    current_set: int                # Which set is being played (1, 2, 3...)
    home_serving: Optional[bool]    # True if player 1 is serving

    @property
    def sets_leader(self) -> Optional[str]:
        """Who is leading in sets? Returns 'home', 'away', or None if tied."""
        if self.home_sets > self.away_sets:
            return "home"
        elif self.away_sets > self.home_sets:
            return "away"
        return None

    @property
    def is_dominant(self) -> bool:
        """Is the sets leader also winning the current set?"""
        leader = self.sets_leader
        if leader == "home":
            return self.home_games > self.away_games
        elif leader == "away":
            return self.away_games > self.home_games
        return False

    def underdog_is_winning(self, fav_is_home: bool) -> bool:
        """Check if the underdog (not the favorite) is leading in sets."""
        if fav_is_home:
            return self.away_sets > self.home_sets
        else:
            return self.home_sets > self.away_sets

    def momentum_score(self, fav_is_home: bool) -> int:
        """
        Returns a momentum score for the underdog (0-3):
          0 = favorite is winning
          1 = tied in sets
          2 = underdog leads by 1 set
          3 = underdog leads by 1 set AND is up a break in current set

        Higher score = stronger signal for pivot.
        """
        if fav_is_home:
            dog_sets, fav_sets = self.away_sets, self.home_sets
            dog_games, fav_games = self.away_games, self.home_games
        else:
            dog_sets, fav_sets = self.home_sets, self.away_sets
            dog_games, fav_games = self.home_games, self.away_games

        if fav_sets > dog_sets:
            return 0  # Favorite leading — no pivot
        if fav_sets == dog_sets:
            # Tied in sets — check current set
            if dog_games > fav_games + 1:
                return 1  # Dog has a break advantage in current set
            return 0
        # dog_sets > fav_sets
        if dog_games > fav_games:
            return 3  # Dominant: up a set AND winning current set
        return 2  # Up a set but current set is close


def _headers() -> dict:
    key = TENNISAPI_KEY or os.getenv("TENNISAPI_KEY", "")
    if not key:
        logger.warning("TENNISAPI_KEY not set — live scores unavailable")
    return {
        "x-rapidapi-key": key,
        "x-rapidapi-host": TENNISAPI_HOST,
    }


async def fetch_live_events() -> list[dict]:
    """Fetch all currently live tennis events."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{TENNISAPI_BASE}/api/tennis/events/live",
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()

            # The response might be {"events": [...]} or just [...]
            if isinstance(data, dict):
                return data.get("events", [])
            if isinstance(data, list):
                return data
            return []

    except Exception as e:
        logger.error(f"Failed to fetch live events: {e}")
        return []


def _parse_live_score(event: dict) -> Optional[LiveScore]:
    """
    Parse a live event into a LiveScore object.

    NOTE: These field names are based on the typical TennisApi1/SofaScore schema.
    If the actual API returns different field names, update this function.
    After running scripts/test_live_scores_api.py, adjust as needed.
    """
    try:
        event_id = str(event.get("id", event.get("eventId", "")))

        # Status
        status_raw = event.get("status", {})
        if isinstance(status_raw, dict):
            status = status_raw.get("type", "unknown").lower()
        else:
            status = str(status_raw).lower()

        # Players
        home = event.get("homeTeam", event.get("home", {}))
        away = event.get("awayTeam", event.get("away", {}))
        home_name = home.get("name", home.get("shortName", "Unknown")) if isinstance(home, dict) else str(home)
        away_name = away.get("name", away.get("shortName", "Unknown")) if isinstance(away, dict) else str(away)

        # Score — try multiple common structures
        home_score = event.get("homeScore", {})
        away_score = event.get("awayScore", {})

        if isinstance(home_score, dict):
            # SofaScore-style: {"current": 1, "period1": 6, "period2": 3, ...}
            home_sets = home_score.get("current", 0) or 0
            away_sets = away_score.get("current", 0) or 0

            # Current set games — find the latest period
            current_set = home_sets + away_sets + 1
            period_key = f"period{current_set}"
            home_games = home_score.get(period_key, 0) or 0
            away_games = away_score.get(period_key, 0) or 0
        elif isinstance(home_score, (int, float)):
            # Simple score
            home_sets = int(home_score)
            away_sets = int(away_score) if isinstance(away_score, (int, float)) else 0
            home_games = 0
            away_games = 0
            current_set = home_sets + away_sets + 1
        else:
            home_sets = away_sets = home_games = away_games = 0
            current_set = 1

        # Serving
        home_serving = None
        # Some APIs have a "servicePlayer" or "serving" field
        # Adjust after testing

        return LiveScore(
            event_id=event_id,
            status=status,
            home_player=home_name,
            away_player=away_name,
            home_sets=home_sets,
            away_sets=away_sets,
            home_games=home_games,
            away_games=away_games,
            current_set=current_set,
            home_serving=home_serving,
        )
    except Exception as e:
        logger.error(f"Failed to parse live score: {e} — event: {str(event)[:200]}")
        return None


def _normalize_name(name: str) -> str:
    """Normalize player name for matching: lowercase, strip accents/periods."""
    import unicodedata
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    return name.lower().strip().replace(".", "").replace("-", " ")


def _names_match(name_a: str, name_b: str) -> bool:
    """
    Fuzzy match two player names.
    Handles: "Sinner J." vs "Jannik Sinner", "C. Alcaraz" vs "Carlos Alcaraz"
    """
    a = _normalize_name(name_a)
    b = _normalize_name(name_b)

    # Exact match
    if a == b:
        return True

    # Last name match (most common case)
    a_parts = a.split()
    b_parts = b.split()

    # Compare last name of each
    if a_parts and b_parts:
        if a_parts[-1] == b_parts[-1]:
            return True
        # Also try first part (some APIs put last name first)
        if a_parts[0] == b_parts[-1] or a_parts[-1] == b_parts[0]:
            return True

    return False


async def find_live_score(player_fav: str, player_dog: str) -> Optional[tuple[LiveScore, bool]]:
    """
    Find the live score for a match between player_fav and player_dog.

    Returns (LiveScore, fav_is_home) or None if match not found.
    fav_is_home: True if the favorite is the home player in the API data.

    Uses a 15-second cache to avoid hammering the API.
    """
    cache_key = f"{player_fav}|{player_dog}"

    # Check cache
    if cache_key in _score_cache:
        cached_time, cached_score = _score_cache[cache_key]
        if time.time() - cached_time < CACHE_TTL:
            # Determine fav_is_home from cached data
            fav_is_home = _names_match(player_fav, cached_score.home_player)
            return cached_score, fav_is_home

    events = await fetch_live_events()
    if not events:
        return None

    for event in events:
        score = _parse_live_score(event)
        if score is None:
            continue

        # Check if this match involves our players
        fav_is_home = _names_match(player_fav, score.home_player)
        fav_is_away = _names_match(player_fav, score.away_player)
        dog_is_home = _names_match(player_dog, score.home_player)
        dog_is_away = _names_match(player_dog, score.away_player)

        if (fav_is_home and dog_is_away) or (fav_is_away and dog_is_home):
            fav_is_home = _names_match(player_fav, score.home_player)
            _score_cache[cache_key] = (time.time(), score)
            logger.info(
                f"Live score found: {score.home_player} vs {score.away_player} "
                f"— Sets: {score.home_sets}-{score.away_sets} "
                f"Games: {score.home_games}-{score.away_games}"
            )
            return score, fav_is_home

    logger.debug(f"No live match found for {player_fav} vs {player_dog}")
    return None
