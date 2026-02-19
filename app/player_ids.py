"""
Static mapping of ATP/WTA player names (lowercase) to their numeric IDs
in the tennis-api-atp-wta-itf.p.rapidapi.com API.

The search endpoint does NOT return player IDs, and no name→ID lookup
endpoint exists. IDs must be discovered via:

  GET /api/debug/matchstat/scan?start=5000&count=100

Then add entries here manually.

Known IDs (verified via /tennis/v2/atp/player/profile/{id}):
  677   = Rafael Nadal     (inferred from Djokovic H2H: Nadal leads clay 20-9)
  5992  = Novak Djokovic   (confirmed via player profile)
"""

ATP_PLAYER_IDS: dict[str, int] = {
    "novak djokovic": 5992,
    "rafael nadal": 677,
    # Populate more via GET /api/debug/matchstat/scan
}

WTA_PLAYER_IDS: dict[str, int] = {
    # Populate via GET /api/debug/matchstat/scan (use wta=true param)
}


def find_player_id(name: str, is_wta: bool = False) -> int | None:
    """
    Find player ID by name (case-insensitive).
    Tries exact match, then substring match, then last-name match.
    Returns None if not found.
    """
    lookup = WTA_PLAYER_IDS if is_wta else ATP_PLAYER_IDS
    name_lower = name.strip().lower()

    # 1. Exact match
    if name_lower in lookup:
        return lookup[name_lower]

    # 2. Stored key is contained in searched name, or vice-versa
    for key, pid in lookup.items():
        if key in name_lower or name_lower in key:
            return pid

    # 3. Last-name match
    parts = name_lower.split()
    last = parts[-1] if parts else ""
    if last:
        for key, pid in lookup.items():
            key_parts = key.split()
            if key_parts and key_parts[-1] == last:
                return pid

    return None
