"""
Decision engine — implements the trading system from sistema_conciso_final.md

System:
  TARGET = favorite_probability × Factor
  Factor = 0.70 + adjustments(tournament, surface)

  TARGET is the limit order price to place on Kalshi's order book.
  If match passes all filters → BUY (place limit order at TARGET)
  Various conditions → SKIP
"""

from app.models import (
    MatchData, AnalysisResult, Signal,
    TournamentLevel, Surface,
)

# --- Configuration (tweak these as you gather data) ---

BASE_FACTOR = 0.70

TOURNAMENT_ADJ = {
    TournamentLevel.ATP: 0.00,
    TournamentLevel.WTA: 0.00,
    TournamentLevel.CHALLENGER: -0.05,
    TournamentLevel.GRAND_SLAM: 0.05,
}

SURFACE_ADJ = {
    Surface.HARD: 0.00,
    Surface.CLAY: -0.02,
    Surface.GRASS: 0.03,
}

# Filters
MIN_FAVORITE_PCT = 0.70
MAX_FAVORITE_PCT = 0.92
MIN_VOLUME = 100


def calculate_factor(
    tournament: TournamentLevel,
    surface: Surface,
) -> float:
    """Calculate the multiplier factor from base + adjustments."""
    factor = BASE_FACTOR
    factor += TOURNAMENT_ADJ.get(tournament, 0.0)
    factor += SURFACE_ADJ.get(surface, 0.0)
    return round(factor, 2)


def analyze_match(match: MatchData) -> AnalysisResult:
    """
    Run the full decision engine on a single match.
    TARGET = the limit order price to set on Kalshi.
    If match passes filters → BUY (place limit order at TARGET).
    """

    # --- Filters (SKIP conditions) ---
    if match.tournament_level == TournamentLevel.GRAND_SLAM:
        return AnalysisResult(
            match=match, signal=Signal.SKIP,
            target_price=None, factor=None,
            skip_reason="Grand Slam — not traded",
        )

    if match.fav_probability < MIN_FAVORITE_PCT:
        return AnalysisResult(
            match=match, signal=Signal.SKIP,
            target_price=None, factor=None,
            skip_reason=f"Favorite {match.fav_probability*100:.0f}% < {MIN_FAVORITE_PCT*100:.0f}% minimum",
        )

    if match.fav_probability > MAX_FAVORITE_PCT:
        return AnalysisResult(
            match=match, signal=Signal.SKIP,
            target_price=None, factor=None,
            skip_reason=f"Favorite {match.fav_probability*100:.0f}% > {MAX_FAVORITE_PCT*100:.0f}% maximum",
        )

    if match.volume < MIN_VOLUME:
        return AnalysisResult(
            match=match, signal=Signal.SKIP,
            target_price=None, factor=None,
            skip_reason=f"Volume ${match.volume:,.0f} < ${MIN_VOLUME:,.0f} minimum",
        )

    # --- Calculate target (limit order price) ---
    factor = calculate_factor(match.tournament_level, match.surface)
    target = round(match.fav_probability * factor, 2)

    # Edge = how far the current market is from our limit order
    kalshi_decimal = match.kalshi_price / 100.0
    edge = kalshi_decimal - target  # positive = market above our order

    # All matches passing filters get BUY signal (place limit order at TARGET)
    return AnalysisResult(
        match=match,
        signal=Signal.BUY,
        target_price=target,
        factor=factor,
        edge=edge,
    )


def analyze_all(matches: list[MatchData]) -> list[AnalysisResult]:
    """Analyze a batch of matches. BUY first (sorted by tightest spread), then SKIP."""
    results = [analyze_match(m) for m in matches]
    order = {Signal.BUY: 0, Signal.WAIT: 1, Signal.SKIP: 2}
    results.sort(key=lambda r: (order.get(r.signal, 3), r.edge if r.edge is not None else 999))
    return results
