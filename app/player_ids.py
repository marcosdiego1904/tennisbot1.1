"""
Static mapping of ATP/WTA player names (lowercase) to their numeric IDs
in the tennis-api-atp-wta-itf.p.rapidapi.com API.

The search endpoint does NOT return player IDs, and no name→ID lookup
endpoint exists. IDs must be discovered via:

  GET /api/debug/matchstat/scan?start=5000&count=100

Then add entries here manually.

ID structure findings (from scanning):
  Scanned so far:
    600-699   → 1 player  (ID 603, Inactive) — almost empty
    3000-3099 → 0 players — completely empty
    5900-5999 → 96 players — era ~2003, very dense (Djokovic, Monfils)
    6000-6099 → 99 players — era ~2003-2004 (Robin Haase 6081)
    6500-6599 → 98 players — era ~2004-2005 (Jamie Murray 6508, all Inactive singles)
    7000-7099 → 0 players — completely empty

  Pattern: IDs ~5900-6600 = players who entered ~2003-2006, then a hard stop.
  The database appears to have been seeded in one batch for that era, then a
  gap, then a new batch for modern players at an unknown higher range.

  Next ranges to try to find Tiafoe (pro 2015), Sinner (pro 2018), etc.:
    /api/debug/matchstat/scan?start=15000&count=100
    /api/debug/matchstat/scan?start=20000&count=100
    /api/debug/matchstat/scan?start=30000&count=100
    /api/debug/matchstat/scan?start=50000&count=100

Known IDs:
  5917  = Gael Monfils    (confirmed Active, scan 5900-5999)
  5992  = Novak Djokovic  (confirmed Active, player profile endpoint)
  6081  = Robin Haase     (confirmed Active, scan 6000-6099)
  677   = Rafael Nadal    (UNCONFIRMED via profile — no profile found in scan
                           600-699, but H2H endpoint 5992 vs 677 returns stats
                           consistent with Djokovic/Nadal; keep for H2H use)
"""

ATP_PLAYER_IDS: dict[str, int] = {
    "novak djokovic": 5992,
    "gael monfils":   5917,
    "robin haase":    6081,
    "rafael nadal":   677,   # H2H-verified; no standalone profile endpoint
    # Add more after running /api/debug/matchstat/scan at higher ranges
}

WTA_PLAYER_IDS: dict[str, int] = {
    # Populate via GET /api/debug/matchstat/scan?wta=true
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
