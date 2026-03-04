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
from app.scheduler import (
    start_automation, stop_automation, scheduler_state,
    start_pivot_scanner, stop_pivot_scanner,
)
from app.pivot_trade import scan_and_pivot, get_pivot_status, get_pivot_history
from app.bet_tracker import (
    track_bet, get_all_bets, get_bet_by_id, update_outcome, get_stats,
)

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

        # 2. Endpoints confirmados en la documentación de RapidAPI
        doc_probes = {
            # Player profile por ID conocido (5992 del ejemplo H2H)
            # → Vemos si devuelve nombre + si podemos hacer lookup inverso
            "player_profile_5992":          "/tennis/v2/atp/player/profile/5992",
            # H2H info (vs stats) — puede devolver nombres de jugadores
            "h2h_info_5992_677":            "/tennis/v2/atp/h2h/info/5992/677/",
            # Player titles — {player} puede ser ID o slug
            f"titles_id_5992":              "/tennis/v2/atp/player/titles/5992",
            f"titles_slug_fav":             f"/tennis/v2/atp/player/titles/{fav_slug}",
            f"titles_slug_dog":             f"/tennis/v2/atp/player/titles/{dog_slug}",
            # Alternativa: apellido solo
            f"titles_surname_fav":          f"/tennis/v2/atp/player/titles/{fav.lower().split()[-1]}",
            f"titles_surname_dog":          f"/tennis/v2/atp/player/titles/{dog.lower().split()[-1]}",
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
# Auto-sell bot toggle endpoints
# ---------------------------------------------------------------------------

async def _get_bot_enabled() -> bool:
    import aiosqlite, os
    db_path = os.getenv("DB_PATH", "data/orders.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            )
        """)
        await db.execute(
            "INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('bot_enabled', 'true')"
        )
        await db.commit()
        async with db.execute(
            "SELECT value FROM bot_settings WHERE key='bot_enabled'"
        ) as cur:
            row = await cur.fetchone()
            return row is None or row[0] == "true"


async def _set_bot_enabled(enabled: bool) -> None:
    import aiosqlite, os
    db_path = os.getenv("DB_PATH", "data/orders.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('bot_enabled', ?)",
            ("true" if enabled else "false",),
        )
        await db.commit()


@router.get("/bot/status")
async def bot_status():
    """Return whether the auto-sell bot is enabled or paused."""
    enabled = await _get_bot_enabled()
    return {"enabled": enabled, "status": "running" if enabled else "paused"}


@router.post("/bot/enable")
async def bot_enable():
    """Enable the auto-sell bot (resumes position scanning)."""
    await _set_bot_enabled(True)
    return {"enabled": True, "status": "running"}


@router.post("/bot/disable")
async def bot_disable():
    """Pause the auto-sell bot (stops position scanning without killing the process)."""
    await _set_bot_enabled(False)
    return {"enabled": False, "status": "paused"}


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


# ---------------------------------------------------------------------------
# Pivot Trade endpoints
# ---------------------------------------------------------------------------

@router.get("/pivot/status")
async def pivot_status():
    """
    Returns current pivot scanner state:
    - Whether the scanner is running and config (stop-loss %, momentum threshold)
    - Last scan summary (positions checked, pivots executed, stop-losses)
    """
    return {
        "scheduler": scheduler_state().get("pivot_scanner", {}),
        "pivot": get_pivot_status(),
    }


@router.post("/pivot/start")
async def pivot_start():
    """
    Start the pivot trade scanner.
    Scans open positions every PIVOT_SCAN_SECONDS (default: 30s).
    Runs one scan immediately, then repeats on interval.
    """
    try:
        result = await start_pivot_scanner()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/pivot/stop")
async def pivot_stop():
    """Stop the pivot scanner (does not affect existing positions)."""
    return stop_pivot_scanner()


@router.post("/pivot/scan")
async def pivot_scan_once():
    """
    Manually trigger one pivot scan without affecting the scheduler.
    Useful for testing the logic before enabling automatic scanning.
    """
    try:
        summary = await scan_and_pivot()
        return summary
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pivot/history")
async def pivot_history():
    """Return the log of all pivot trades and stop-loss exits."""
    try:
        history = await get_pivot_history()
        return {"trades": history, "count": len(history)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Bet Tracker endpoints
# ---------------------------------------------------------------------------

@router.post("/bets/track")
async def bets_track(payload: dict):
    """
    Save a snapshot of a match card when the user clicks Track.
    Receives the card data as a JSON snapshot — values are frozen at click time.
    """
    try:
        bet = await track_bet(
            event_ticker=payload.get("ticker"),
            player_fav=payload["fav_name"],
            player_dog=payload["dog_name"],
            tournament=payload["tournament"],
            tournament_level=payload["tournament_level"],
            surface=payload["surface"],
            fav_probability=float(payload["fav_probability"]),
            kalshi_price=int(payload["kalshi_price"]),
            target_price=int(payload["target_price"]) if payload.get("target_price") is not None else 0,
        )
        return {"status": "ok", "bet": bet}
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid payload: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/bets")
async def bets_list(status: str = None):
    """
    Return all tracked bets.
    Optional ?status=pending or ?status=completed filter.
    """
    try:
        bets = await get_all_bets(status=status)
        return {"bets": bets, "count": len(bets)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/bets/{bet_id}/outcome")
async def bets_update_outcome(bet_id: int, payload: dict):
    """
    Record the outcome for a tracked bet.
    Accepts: lowest_price_reached (int cents), match_outcome ('fav_won'|'fav_lost'), contracts (int).
    Auto-calculates: order_filled, fill_price, edge, pnl.
    """
    try:
        lowest = int(payload["lowest_price_reached"])
        outcome = payload["match_outcome"]
        contracts = int(payload.get("contracts", 0))

        if outcome not in ("fav_won", "fav_lost"):
            raise HTTPException(status_code=400, detail="match_outcome must be 'fav_won' or 'fav_lost'")

        bet = await update_outcome(
            bet_id=bet_id,
            lowest_price_reached=lowest,
            match_outcome=outcome,
            contracts=contracts,
        )
        if not bet:
            raise HTTPException(status_code=404, detail="Bet not found")

        return {"status": "ok", "bet": bet}
    except HTTPException:
        raise
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid payload: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/bets/{bet_id}")
async def bets_delete(bet_id: int):
    """Delete a tracked bet by ID."""
    import aiosqlite
    from app.bet_tracker import DB_PATH
    bet = await get_bet_by_id(bet_id)
    if not bet:
        raise HTTPException(status_code=404, detail="Bet not found")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM tracked_bets WHERE id = ?", (bet_id,))
        await db.commit()
    return {"status": "ok", "deleted_id": bet_id}


@router.get("/bets/stats")
async def bets_stats():
    """Return analytics over all completed tracked bets."""
    try:
        stats = await get_stats()
        return {"status": "ok", "stats": stats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Debug: Live Scores (Pivot Trade)
# ---------------------------------------------------------------------------

@router.get("/debug/live-scores")
async def debug_live_scores(fav: str = "Sinner", dog: str = "Medvedev"):
    """
    Debug endpoint for live scores API.
    Tests TennisApi1 (RapidAPI) endpoints and shows the raw schema.

    Ejemplo:
      GET /api/debug/live-scores?fav=Sinner&dog=Medvedev
      GET /api/debug/live-scores?fav=Tiafoe&dog=Svajda
    """
    import httpx
    import os
    import json

    api_key = os.getenv("TENNISAPI_KEY", "")
    if not api_key:
        return {
            "error": "TENNISAPI_KEY not set in environment",
            "note": "Set TENNISAPI_KEY=your_key in .env to test",
        }

    host = "tennisapi1.p.rapidapi.com"
    headers = {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": host,
    }

    result = {
        "request": {"fav": fav, "dog": dog},
        "endpoints_tested": {},
        "errors": [],
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Test 1: Live events endpoint
        try:
            r = await client.get(
                f"https://{host}/api/tennis/events/live",
                headers=headers,
            )
            live_data = r.json() if r.status_code == 200 else {}
            result["endpoints_tested"]["events/live"] = {
                "status": r.status_code,
                "live_events_count": len(live_data.get("events", live_data)) if isinstance(live_data, (dict, list)) else 0,
                "preview": json.dumps(live_data, indent=2)[:1500] if live_data else "No data",
            }
        except Exception as e:
            result["errors"].append(f"events/live: {e}")

        # Test 2: Point-by-point (using a known event ID or placeholder)
        test_event_id = "14232981"  # fallback ID
        try:
            r = await client.get(
                f"https://{host}/api/tennis/event/{test_event_id}/point-by-point",
                headers=headers,
            )
            point_data = r.json() if r.status_code == 200 else {}
            result["endpoints_tested"][f"event/{test_event_id}/point-by-point"] = {
                "status": r.status_code,
                "data_keys": list(point_data.keys()) if isinstance(point_data, dict) else type(point_data).__name__,
                "preview": json.dumps(point_data, indent=2)[:1500] if point_data else "No data",
            }
        except Exception as e:
            result["errors"].append(f"event/{test_event_id}/point-by-point: {e}")

    # Test 3: Parse all live events and show them
    try:
        from app.live_scores import fetch_live_events, _parse_live_score, find_live_score

        events = await fetch_live_events()
        parsed_all = []
        for ev in events:
            s = _parse_live_score(ev)
            if s:
                parsed_all.append({
                    "event_id": s.event_id,
                    "home": s.home_player,
                    "away": s.away_player,
                    "status": s.status,
                    "sets": f"{s.home_sets}-{s.away_sets}",
                    "games": f"{s.home_games}-{s.away_games}",
                    "set": s.current_set,
                })
        result["all_live_matches"] = parsed_all

        # Search for specific players
        score_result = await find_live_score(fav, dog)
        if score_result:
            score, fav_is_home = score_result
            result["live_score_found"] = {
                "status": "found",
                "home": score.home_player,
                "away": score.away_player,
                "home_sets": score.home_sets,
                "away_sets": score.away_sets,
                "current_set": score.current_set,
                "home_games": score.home_games,
                "away_games": score.away_games,
                "fav_is_home": fav_is_home,
                "momentum_score": score.momentum_score(fav_is_home),
            }
        else:
            result["live_score_found"] = {"status": "not_found", "note": "No live match for these players right now"}
    except Exception as e:
        result["live_score_error"] = str(e)

    return result


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
