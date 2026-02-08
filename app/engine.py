"""
Decision engine — implements the trading system from sistema_conciso_final.md

System:
  TARGET = favorite_probability × Factor
  Factor = 0.70 + adjustments(tournament, surface, ranking_gap)

  If Kalshi price < TARGET → BUY
  If Kalshi price >= TARGET → WAIT
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

# Ranking gap thresholds
GAP_SMALL = 50
GAP_MEDIUM = 100
GAP_MAX = 150

GAP_ADJ_SMALL = -0.02    # gap < 50
GAP_ADJ_MEDIUM = 0.03    # 50 <= gap <= 100
GAP_ADJ_LARGE = 0.08     # gap > 100

# Filters
MIN_FAVORITE_PCT = 0.70
MAX_FAVORITE_PCT = 0.92
MIN_VOLUME = 100


def calculate_factor(
    tournament: TournamentLevel,
    surface: Surface,
    ranking_gap: int,
) -> float:
    """Calculate the multiplier factor from base + adjustments."""
    factor = BASE_FACTOR
    factor += TOURNAMENT_ADJ.get(tournament, 0.0)
    factor += SURFACE_ADJ.get(surface, 0.0)

    if ranking_gap < GAP_SMALL:
        factor += GAP_ADJ_SMALL
    elif ranking_gap <= GAP_MEDIUM:
        factor += GAP_ADJ_MEDIUM
    else:
        factor += GAP_ADJ_LARGE

    return round(factor, 2)


def analyze_match(match: MatchData) -> AnalysisResult:
    """
    Run the full decision engine on a single match.
    Returns an AnalysisResult with signal, target, and reasoning.
    """

    # --- Filters (SKIP conditions) ---
    if match.tournament_level == TournamentLevel.GRAND_SLAM:
        return AnalysisResult(
            match=match, signal=Signal.SKIP,
            target_price=None, factor=None, ranking_gap=None,
            skip_reason="Grand Slam — not traded",
        )

    if match.fav_probability < MIN_FAVORITE_PCT:
        return AnalysisResult(
            match=match, signal=Signal.SKIP,
            target_price=None, factor=None, ranking_gap=None,
            skip_reason=f"Favorite {match.fav_probability*100:.0f}% < {MIN_FAVORITE_PCT*100:.0f}% minimum",
        )

    if match.fav_probability > MAX_FAVORITE_PCT:
        return AnalysisResult(
            match=match, signal=Signal.SKIP,
            target_price=None, factor=None, ranking_gap=None,
            skip_reason=f"Favorite {match.fav_probability*100:.0f}% > {MAX_FAVORITE_PCT*100:.0f}% maximum",
        )

    ranking_gap = _compute_gap(match)

    if ranking_gap is not None and ranking_gap > GAP_MAX:
        return AnalysisResult(
            match=match, signal=Signal.SKIP,
            target_price=None, factor=None, ranking_gap=ranking_gap,
            skip_reason=f"Ranking gap {ranking_gap} > {GAP_MAX} maximum",
        )

    if match.volume < MIN_VOLUME:
        return AnalysisResult(
            match=match, signal=Signal.SKIP,
            target_price=None, factor=None, ranking_gap=ranking_gap,
            skip_reason=f"Volume ${match.volume:,.0f} < ${MIN_VOLUME:,.0f} minimum",
        )

    # --- Calculate target ---
    gap_for_calc = ranking_gap if ranking_gap is not None else 0
    factor = calculate_factor(match.tournament_level, match.surface, gap_for_calc)
    target = round(match.fav_probability * factor, 4)

    # --- Signal ---
    kalshi_decimal = match.kalshi_price / 100.0  # cents → decimal
    edge = target - kalshi_decimal

    if kalshi_decimal <= target:
        signal = Signal.BUY
    else:
        signal = Signal.WAIT

    return AnalysisResult(
        match=match,
        signal=signal,
        target_price=target,
        factor=factor,
        ranking_gap=ranking_gap,
        edge=edge,
    )


def analyze_all(matches: list[MatchData]) -> list[AnalysisResult]:
    """Analyze a batch of matches. Returns list sorted: BUY first, then WAIT, then SKIP."""
    results = [analyze_match(m) for m in matches]
    order = {Signal.BUY: 0, Signal.WAIT: 1, Signal.SKIP: 2}
    results.sort(key=lambda r: order.get(r.signal, 3))
    return results


def _compute_gap(match: MatchData) -> int | None:
    r1 = match.player_fav.ranking
    r2 = match.player_dog.ranking
    if r1 is not None and r2 is not None:
        return abs(r1 - r2)
    return None
