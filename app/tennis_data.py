"""
Tennis data helpers — tournament database loader.
"""

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"


def load_tournament_db() -> dict:
    """
    Load tournament database (tournament name -> level + surface).
    Static JSON file we maintain.
    """
    path = DATA_DIR / "tournaments.json"
    if path.exists():
        return json.loads(path.read_text())
    return {}
