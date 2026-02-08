"""
Tennis data client — fetches rankings from Jeff Sackmann's open-source
GitHub repositories (tennis_atp / tennis_wta).

Data source: https://github.com/JeffSackmann/tennis_atp
             https://github.com/JeffSackmann/tennis_wta

CSV format (rankings):  ranking_date, rank, player_id, points
CSV format (players):   player_id, name_first, name_last, hand, dob, ioc, height, wikidata_id
"""

import csv
import io
import json
import httpx
from pathlib import Path
from datetime import datetime, timedelta

# GitHub raw URLs for Sackmann data
SACKMANN_ATP_RANKINGS = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_rankings_current.csv"
SACKMANN_ATP_PLAYERS = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_players.csv"
SACKMANN_WTA_RANKINGS = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_rankings_current.csv"
SACKMANN_WTA_PLAYERS = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_players.csv"

DATA_DIR = Path(__file__).parent.parent / "data"
RANKINGS_CACHE = DATA_DIR / "rankings_cache.json"
CACHE_TTL_HOURS = 24  # rankings update weekly, cache for a day


async def fetch_rankings(force_refresh: bool = False) -> dict[str, int]:
    """
    Fetch ATP + WTA rankings from Sackmann GitHub CSVs.
    Returns dict of {player_name_lower: ranking}.
    Uses local cache to avoid re-downloading on every request.
    """
    if not force_refresh and _cache_is_valid():
        return _load_cache()

    rankings = {}

    async with httpx.AsyncClient() as client:
        # Fetch ATP
        atp = await _fetch_sackmann_rankings(client, SACKMANN_ATP_RANKINGS, SACKMANN_ATP_PLAYERS)
        rankings.update(atp)

        # Fetch WTA
        wta = await _fetch_sackmann_rankings(client, SACKMANN_WTA_RANKINGS, SACKMANN_WTA_PLAYERS)
        rankings.update(wta)

    if rankings:
        _save_cache(rankings)

    return rankings


async def _fetch_sackmann_rankings(
    client: httpx.AsyncClient,
    rankings_url: str,
    players_url: str,
) -> dict[str, int]:
    """
    Download rankings + players CSVs from GitHub, join them,
    and return {name_lower: rank} dict.
    """
    rankings = {}

    try:
        # Fetch both files in sequence (players first, then rankings)
        players_resp = await client.get(players_url, timeout=20.0)
        players_resp.raise_for_status()

        rankings_resp = await client.get(rankings_url, timeout=20.0)
        rankings_resp.raise_for_status()

        # Parse players CSV → {player_id: "first last"}
        player_names = {}
        reader = csv.DictReader(io.StringIO(players_resp.text))
        for row in reader:
            pid = row.get("player_id", "").strip()
            first = row.get("name_first", "").strip()
            last = row.get("name_last", "").strip()
            if pid and last:
                full = f"{first} {last}".strip()
                player_names[pid] = full

        # Parse rankings CSV — get the most recent date's rankings
        reader = csv.DictReader(io.StringIO(rankings_resp.text))
        rows = list(reader)

        if not rows:
            return rankings

        # Find the most recent ranking_date
        latest_date = max(row.get("ranking_date", "") for row in rows)

        # Filter to only the latest date
        for row in rows:
            if row.get("ranking_date") != latest_date:
                continue

            pid = row.get("player", "").strip()
            rank_str = row.get("rank", "").strip()

            if not pid or not rank_str:
                continue

            try:
                rank_int = int(rank_str)
            except (ValueError, TypeError):
                continue

            name = player_names.get(pid, "")
            if not name:
                continue

            # Store by full name and last name (lowercase) for flexible matching
            full_lower = name.lower()
            last_lower = name.split()[-1].lower() if name.split() else ""

            if full_lower:
                rankings[full_lower] = rank_int
            if last_lower:
                rankings[last_lower] = rank_int

    except (httpx.HTTPError, KeyError, ValueError) as e:
        label = "ATP" if "atp" in rankings_url else "WTA"
        print(f"Warning: Could not fetch {label} rankings from Sackmann GitHub: {e}")
        if RANKINGS_CACHE.exists():
            return _load_cache()

    return rankings


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
