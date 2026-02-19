"""
Matchstat API client — fetches tennis match predictions to confirm BUY signals.

API available via RapidAPI:
  https://rapidapi.com/jjrm365-kIFr3Nx_odV/api/tennis-api-atp-wta-itf

Setup:
  1. Create account at https://rapidapi.com
  2. Subscribe to tennis-api-atp-wta-itf
  3. Copy your X-RapidAPI-Key to MATCHSTAT_API_KEY in .env
  4. Check available endpoints in RapidAPI console and update
     MATCHSTAT_PREDICTIONS_ENDPOINT if needed

What we need from Matchstat: win probability % for a player in an upcoming match.
"""

import os
import httpx
import logging
from typing import Optional

logger = logging.getLogger(__name__)

MATCHSTAT_HOST = "tennis-api-atp-wta-itf.p.rapidapi.com"
MATCHSTAT_BASE_URL = f"https://{MATCHSTAT_HOST}"

# Minimum win probability from Matchstat to confirm a BUY signal
MATCHSTAT_MIN_WIN_PCT = float(os.getenv("MATCHSTAT_MIN_WIN_PCT", "0.65"))

# Endpoint to call — update after checking your RapidAPI subscription's endpoints.
# Go to: https://rapidapi.com/jjrm365-kIFr3Nx_odV/api/tennis-api-atp-wta-itf
# Click "Endpoints" → look for predictions, odds, or match data endpoints.
MATCHSTAT_ENDPOINT = os.getenv("MATCHSTAT_PREDICTIONS_ENDPOINT", "/tennis/")


async def get_player_win_probability(player_name: str) -> Optional[float]:
    """
    Fetch win probability for a player from Matchstat API.

    Returns:
        float (0.0–1.0): win probability if found
        None: if API unavailable, key missing, or player not found

    NOTE: _parse_win_probability() below is a placeholder.
    After testing the endpoint in RapidAPI console, update that function
    with the actual JSON structure the API returns.
    """
    api_key = os.getenv("MATCHSTAT_API_KEY")
    if not api_key:
        logger.warning("MATCHSTAT_API_KEY not set — skipping Matchstat confirmation")
        return None

    headers = {
        "X-RapidAPI-Key": api_key,
        "X-RapidAPI-Host": MATCHSTAT_HOST,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{MATCHSTAT_BASE_URL}{MATCHSTAT_ENDPOINT}",
                headers=headers,
                params={"player": player_name},  # adjust params per actual API docs
            )
            response.raise_for_status()
            data = response.json()
            logger.debug(f"Matchstat raw response for '{player_name}': {str(data)[:500]}")
            return _parse_win_probability(data, player_name)

    except httpx.HTTPStatusError as e:
        logger.error(
            f"Matchstat API {e.response.status_code} for '{player_name}': {e.response.text[:200]}"
        )
        return None
    except Exception as e:
        logger.error(f"Matchstat request failed for '{player_name}': {e}")
        return None


def _parse_win_probability(data: dict | list, player_name: str) -> Optional[float]:
    """
    Parse win probability from Matchstat API response.

    !! UPDATE THIS FUNCTION once you know the actual response structure !!

    Steps to find out the structure:
      1. Go to RapidAPI → test the endpoint in the console
      2. Look at the JSON response
      3. Identify the field with win probability (usually 0–100 or 0.0–1.0)
      4. Update the parsing below

    Common patterns in tennis prediction APIs:
      data["win_probability"]               → direct
      data["predictions"][0]["win_pct"]     → nested array
      data[0]["player1_win_chance"]         → root list
      data["match"]["home_win_prob"]        → nested dict

    Once identified, log the raw response once for confirmation:
      logger.info(f"Raw: {data}")
    """
    # Log raw response to help debug during initial integration
    logger.info(f"Matchstat raw response for '{player_name}': {str(data)[:1000]}")

    # --- Replace the block below with actual parsing ---
    #
    # Example (update to match your API's actual response):
    #
    # if isinstance(data, list) and data:
    #     for match in data:
    #         if player_name.lower() in match.get("player1", "").lower():
    #             pct = match.get("player1_win_pct", 0)
    #             return pct / 100.0 if pct > 1 else pct
    #         if player_name.lower() in match.get("player2", "").lower():
    #             pct = match.get("player2_win_pct", 0)
    #             return pct / 100.0 if pct > 1 else pct
    #
    # if isinstance(data, dict):
    #     pct = data.get("win_probability")
    #     if pct is not None:
    #         return pct / 100.0 if pct > 1 else float(pct)
    #
    # ---------------------------------------------------

    return None  # Remove this line once parsing is implemented


def confirms_signal(win_probability: Optional[float]) -> bool:
    """Return True if Matchstat's win probability meets the minimum threshold."""
    if win_probability is None:
        return False
    return win_probability >= MATCHSTAT_MIN_WIN_PCT
