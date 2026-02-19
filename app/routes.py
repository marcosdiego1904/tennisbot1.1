"""
API routes for the tennis trading dashboard.
"""

from fastapi import APIRouter, HTTPException
from app.kalshi_client import fetch_tennis_markets
from app.tennis_data import load_tournament_db
from app.engine import analyze_all, analyze_match
from app.models import (
    MatchData, AnalysisResult, Signal,
    PlayerInfo, TournamentLevel, Surface,
)
from app.automation import run_automation_cycle, get_status, get_all_orders
from app.scheduler import start_automation, stop_automation, scheduler_state

router = APIRouter(prefix="/api")


@router.get("/analyze")
async def analyze_markets():
    """
    Main endpoint: fetch Kalshi markets, run the engine, return analysis.
    """
    try:
        tournament_db = load_tournament_db()
        matches = await fetch_tennis_markets(tournament_db)

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
            player_fav=PlayerInfo(name=payload["fav_name"]),
            player_dog=PlayerInfo(name=payload["dog_name"]),
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


# ---------------------------------------------------------------------------
# Automation endpoints
# ---------------------------------------------------------------------------

@router.get("/automation/status")
async def automation_status():
    """
    Returns current automation state:
    - Whether the scheduler is running and next run time
    - DRY_RUN mode
    - Last cycle summary (signals found, orders placed, Matchstat results)
    - Session order count
    """
    return {
        "scheduler": scheduler_state(),
        "automation": get_status(),
    }


@router.post("/automation/start")
async def automation_start():
    """
    Start the automated workflow.
    Runs one cycle immediately, then repeats every AUTOMATION_INTERVAL_MINUTES.
    Safe to call multiple times — replaces existing job.
    """
    try:
        result = await start_automation()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/automation/stop")
async def automation_stop():
    """Stop the automation scheduler (does not cancel already-placed orders)."""
    return stop_automation()


@router.post("/automation/run")
async def automation_run_once():
    """
    Manually trigger one automation cycle without affecting the scheduler.
    Useful for testing before enabling the automatic schedule.
    """
    try:
        summary = await run_automation_cycle()
        return summary
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/automation/orders")
async def automation_orders():
    """
    Return the log of all processed orders (placed, simulated, and rejected).
    Newest first, max 200 records.
    """
    try:
        orders = await get_all_orders()
        return {"orders": orders, "count": len(orders)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
        "target_price": int(round(result.target_price * 100)) if result.target_price else None,
        "factor": result.factor,
        "edge": round(result.edge * 100, 1) if result.edge else None,
        "tournament": m.tournament_name,
        "tournament_level": m.tournament_level.value,
        "surface": m.surface.value,
        "volume": m.volume,
        "close_time": m.close_time,
        "skip_reason": result.skip_reason,
        "summary": result.summary,
        "ticker": m.kalshi_ticker,
    }
