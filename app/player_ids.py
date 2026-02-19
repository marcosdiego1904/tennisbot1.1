"""
Static mapping of ATP/WTA player names (lowercase) to their numeric IDs
in the tennis-api-atp-wta-itf.p.rapidapi.com API.

The search endpoint does NOT return player IDs, and no name→ID lookup
endpoint exists. IDs must be discovered via:

  GET /api/debug/matchstat/scan?start=5000&count=100

Then add entries here manually.

ID structure findings (from scanning):
  - Range 5900-5999: ~96 players found — era 2003 players, very dense
  - Range 600-699:   only 1 player (ID 603) — most of this range is empty
  - Ranges 3000-3099 and 7000-7099: completely empty
  - Conclusion: IDs are NOT sequential across eras; players from different
    periods were assigned IDs in separate batches.
  - Modern players (Sinner, Alcaraz, Tiafoe) are likely in a higher range.
    Try: /api/debug/matchstat/scan?start=6000&count=100
         /api/debug/matchstat/scan?start=6500&count=100
         /api/debug/matchstat/scan?start=8000&count=100
         /api/debug/matchstat/scan?start=10000&count=100

Known IDs:
  5917  = Gael Monfils    (confirmed Active via scan 5900-5999)
  5992  = Novak Djokovic  (confirmed Active via player profile endpoint)
  677   = Rafael Nadal    (UNCONFIRMED via profile — no profile found in scan
                           600-699, but H2H endpoint 5992 vs 677 returns stats
                           consistent with Djokovic/Nadal; keep for H2H use)
"""

ATP_PLAYER_IDS: dict[str, int] = {
    "novak djokovic": 5992,
    "gael monfils":   5917,
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
