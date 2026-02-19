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


@router.get("/debug/matchstat")
async def debug_matchstat(fav: str = "Sinner", dog: str = "Medvedev"):
    """
    Debug endpoint — muestra el JSON crudo de los endpoints de RapidAPI H2H
    e intenta distintos métodos para obtener player IDs.

    Ejemplo:
      GET /api/debug/matchstat?fav=Tiafoe&dog=Svajda
      GET /api/debug/matchstat?fav=Sinner&dog=Medvedev
    """
    import httpx
    import os

    host    = "tennis-api-atp-wta-itf.p.rapidapi.com"
    api_key = os.getenv("MATCHSTAT_API_KEY", "")
    headers = {
        "x-rapidapi-key":  api_key,
        "x-rapidapi-host": host,
    }

    results: dict = {
        "players_searched": {},
        "doc_endpoints": {},
        "errors": [],
    }

    async def _get(client, path, params=None, label=""):
        try:
            r = await client.get(f"https://{host}{path}", params=params, headers=headers)
            return {
                "status": r.status_code,
                "json": r.json() if r.status_code == 200 else r.text[:400],
            }
        except Exception as e:
            results["errors"].append(f"{label}: {e}")
            return {"error": str(e)}

    # Slugs para probar en endpoints que usan nombre
    fav_slug  = fav.lower().replace(" ", "-")
    dog_slug  = dog.lower().replace(" ", "-")

    async with httpx.AsyncClient(timeout=15.0) as client:
        # 1. Search — sabemos que no devuelve IDs pero lo mantenemos
        for name in [fav, dog]:
            results["players_searched"][name] = await _get(
                client, "/tennis/v2/search", params={"search": name}, label=f"search({name})"
            )

        # 2. Endpoints confirmados y alternativos
        fav_surname  = fav.lower().split()[-1]
        dog_surname  = dog.lower().split()[-1]
        doc_probes = {
            # ── Perfiles por ID conocido ──────────────────────────────────
            "player_profile_5992":     "/tennis/v2/atp/player/profile/5992",
            "h2h_info_5992_677":       "/tennis/v2/atp/h2h/info/5992/677/",
            "titles_id_5992":          "/tennis/v2/atp/player/titles/5992",
            # ── Rankings (devolvería jugadores actuales con IDs) ──────────
            "rankings_atp_v2":         "/tennis/v2/atp/rankings",
            "rankings_atp_v2_slash":   "/tennis/v2/atp/rankings/",
            "rankings_v2":             "/tennis/v2/rankings",
            "rankings_atp_v1":         "/tennis/v1/atp/rankings",
            "race_atp":                "/tennis/v2/atp/race",
            # ── Live / schedule (puede incluir player IDs) ────────────────
            "live_v2":                 "/tennis/v2/live",
            "live_atp":                "/tennis/v2/atp/live",
            "schedule_atp":            "/tennis/v2/atp/schedule",
            # ── H2H por nombre / slug (si la API lo soporta) ─────────────
            f"h2h_slug":               f"/tennis/v2/atp/h2h/stats/{fav_slug}/{dog_slug}/",
            f"h2h_surname":            f"/tennis/v2/atp/h2h/stats/{fav_surname}/{dog_surname}/",
            # ── Player lookup por nombre / slug ───────────────────────────
            f"profile_slug_fav":       f"/tennis/v2/atp/player/profile/{fav_slug}",
            f"profile_surname_fav":    f"/tennis/v2/atp/player/profile/{fav_surname}",
        }
        results["doc_endpoints"] = {}
        for label, path in doc_probes.items():
            results["doc_endpoints"][label] = await _get(client, path, label=label)

    return results


@router.get("/debug/matchstat/scan")
async def debug_matchstat_scan(start: int = 5000, count: int = 50, wta: bool = False):
    """
    Escanea un rango de player IDs y devuelve los jugadores encontrados.
    Úsalo para descubrir IDs y añadirlos a app/player_ids.py.

    Ejemplos:
      GET /api/debug/matchstat/scan?start=5900&count=100
      GET /api/debug/matchstat/scan?start=600&count=100
      GET /api/debug/matchstat/scan?start=10000&count=50

    La respuesta incluye 'add_to_player_ids_py' con el dict listo para copiar.
    """
    import httpx
    import os
    import asyncio

    count = min(count, 100)  # hard cap — evitar rate limiting
    tour  = "wta" if wta else "atp"
    host  = "tennis-api-atp-wta-itf.p.rapidapi.com"
    headers = {
        "x-rapidapi-key":  os.getenv("MATCHSTAT_API_KEY", ""),
        "x-rapidapi-host": host,
    }

    found: list[dict] = []
    error_count = 0

    async def probe(client: httpx.AsyncClient, player_id: int) -> None:
        nonlocal error_count
        try:
            r = await client.get(
                f"https://{host}/tennis/v2/{tour}/player/profile/{player_id}",
                headers=headers,
                timeout=8.0,
            )
            if r.status_code == 200:
                data = r.json().get("data", {})
                if data.get("name"):
                    found.append({
                        "id":      player_id,
                        "name":    data["name"],
                        "country": data.get("countryAcr", ""),
                        "status":  data.get("playerStatus", ""),
                    })
        except Exception:
            error_count += 1

    async with httpx.AsyncClient() as client:
        ids = list(range(start, start + count))
        # Process in batches of 10 with a short pause between batches
        for i in range(0, len(ids), 10):
            batch = ids[i : i + 10]
            await asyncio.gather(*[probe(client, pid) for pid in batch])
            if i + 10 < len(ids):
                await asyncio.sleep(0.3)

    found.sort(key=lambda x: x["id"])
    active = [p for p in found if p.get("status") == "Active"]

    return {
        "range":    f"{start}–{start + count - 1}",
        "scanned":  count,
        "found":    len(found),
        "errors":   error_count,
        "players":  found,
        # Copy-paste ready dict for app/player_ids.py
        "add_to_player_ids_py": {
            p["name"].lower(): p["id"] for p in active
        },
    }


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
