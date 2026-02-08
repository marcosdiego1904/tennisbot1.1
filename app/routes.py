"""
API routes for the tennis trading dashboard.
"""

from fastapi import APIRouter, HTTPException
from app.kalshi_client import fetch_tennis_markets
from app.tennis_data import fetch_rankings, load_tournament_db
from app.engine import analyze_all, analyze_match
from app.models import (
    MatchData, AnalysisResult, Signal,
    PlayerInfo, TournamentLevel, Surface,
)

router = APIRouter(prefix="/api")


@router.get("/analyze")
async def analyze_markets():
    """
    Main endpoint: fetch Kalshi markets, enrich with rankings,
    run the engine, return analysis.
    """
    try:
        rankings = await fetch_rankings()
        tournament_db = load_tournament_db()
        matches = await fetch_tennis_markets(rankings, tournament_db)

        if not matches:
            return {
                "status": "ok",
                "message": "No open tennis markets found on Kalshi",
                "results": [],
                "summary": {"buy": 0, "wait": 0, "skip": 0, "total": 0},
            }

        results = analyze_all(matches)
        return _format_results(results)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analyze/manual")
async def analyze_manual(payload: dict):
    """
    Manual analysis endpoint — input match data directly.
    Useful for testing or when Kalshi API isn't available.
    """
    try:
        match = MatchData(
            player_fav=PlayerInfo(
                name=payload["fav_name"],
                ranking=payload.get("fav_ranking"),
            ),
            player_dog=PlayerInfo(
                name=payload["dog_name"],
                ranking=payload.get("dog_ranking"),
            ),
            fav_probability=payload["fav_probability"] / 100.0,
            kalshi_price=payload["kalshi_price"],
            tournament_name=payload.get("tournament_name", "Unknown"),
            tournament_level=TournamentLevel(payload.get("tournament_level", "ATP")),
            surface=Surface(payload.get("surface", "Hard")),
            volume=payload.get("volume", 50000),
        )

        result = analyze_match(match)
        return _format_single(result)

    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid input: {e}")


@router.get("/rankings")
async def get_rankings(refresh: bool = False):
    """Get current cached rankings. Pass ?refresh=true to force update."""
    rankings = await fetch_rankings(force_refresh=refresh)

    # Also show sample of player names for debugging matching issues
    sample = dict(list(rankings.items())[:30])

    return {
        "status": "ok",
        "count": len(rankings),
        "sample": sample,
        "rankings": rankings,
    }


@router.get("/debug/rankings")
async def debug_rankings():
    """Debug: show how Kalshi player names match against api-tennis rankings."""
    rankings = await fetch_rankings()
    tournament_db = load_tournament_db()

    # Get some matches from Kalshi
    try:
        matches = await fetch_tennis_markets(rankings, tournament_db)
    except Exception as e:
        matches = []

    debug = {
        "rankings_count": len(rankings),
        "rankings_sample": dict(list(rankings.items())[:20]),
        "match_lookups": [],
    }

    for m in matches[:20]:
        fav_name = m.player_fav.name
        dog_name = m.player_dog.name
        fav_lower = fav_name.lower()
        dog_lower = dog_name.lower()
        fav_last = fav_lower.split()[-1] if fav_lower.split() else ""
        dog_last = dog_lower.split()[-1] if dog_lower.split() else ""

        debug["match_lookups"].append({
            "fav_name": fav_name,
            "dog_name": dog_name,
            "fav_full_match": rankings.get(fav_lower),
            "fav_last_match": rankings.get(fav_last),
            "dog_full_match": rankings.get(dog_lower),
            "dog_last_match": rankings.get(dog_last),
            "fav_ranking": m.player_fav.ranking,
            "dog_ranking": m.player_dog.ranking,
        })

    return debug


@router.get("/debug/kalshi")
async def debug_kalshi():
    """
    Debug endpoint: shows raw Kalshi data at each step —
    which series return data, raw market fields, and parse results.
    """
    import httpx
    from app.kalshi_client import debug_fetch

    try:
        async with httpx.AsyncClient() as client:
            return await debug_fetch(client)
    except Exception as e:
        return {"error": str(e)}


@router.get("/health")
async def health():
    return {"status": "ok", "service": "tennisbot"}


def _format_results(results: list[AnalysisResult]) -> dict:
    buy = [r for r in results if r.signal == Signal.BUY]
    wait = [r for r in results if r.signal == Signal.WAIT]
    skip = [r for r in results if r.signal == Signal.SKIP]

    return {
        "status": "ok",
        "results": [_format_single(r) for r in results],
        "summary": {
            "buy": len(buy),
            "wait": len(wait),
            "skip": len(skip),
            "total": len(results),
        },
    }


def _format_single(result: AnalysisResult) -> dict:
    m = result.match
    return {
        "signal": result.signal.value,
        "fav_name": m.player_fav.name,
        "dog_name": m.player_dog.name,
        "fav_probability": round(m.fav_probability * 100, 1),
        "kalshi_price": m.kalshi_price,
        "target_price": round(result.target_price * 100, 1) if result.target_price else None,
        "factor": result.factor,
        "ranking_gap": result.ranking_gap,
        "edge": round(result.edge * 100, 1) if result.edge else None,
        "tournament": m.tournament_name,
        "tournament_level": m.tournament_level.value,
        "surface": m.surface.value,
        "volume": m.volume,
        "skip_reason": result.skip_reason,
        "summary": result.summary,
        "ticker": m.kalshi_ticker,
    }
