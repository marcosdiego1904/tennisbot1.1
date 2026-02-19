"""
Matchstat API client — confirma señales BUY usando historial H2H vía RapidAPI.

Flujo para cada partido:
  1. search(player_fav)  → player_id del favorito
  2. search(player_dog)  → player_id del underdog
  3. h2h_stats(id1, id2) → historial de partidos directos
  4. win_pct = partidos_ganados_por_fav / total_h2h
  5. Si win_pct >= MATCHSTAT_MIN_WIN_PCT → confirmar BUY

Endpoints usados:
  GET /tennis/v2/search?search={nombre}
  GET /tennis/v2/atp/h2h/stats/{id1}/{id2}/
  GET /tennis/v2/wta/h2h/stats/{id1}/{id2}/   (para partidas WTA)

Configuración (.env):
  MATCHSTAT_API_KEY            Tu x-rapidapi-key
  MATCHSTAT_MIN_WIN_PCT=0.60   Win% mínimo en H2H para confirmar (default 60%)
  MATCHSTAT_MIN_H2H_MATCHES=3  Mínimo de partidos H2H para confiar en el dato (default 3)

NOTA: Los parsers _parse_player_id() y _parse_h2h_wins() tienen TODOs marcados.
      Ejecuta scripts/explore_matchstat_api.py localmente, pega el JSON resultante
      y actualizamos esas funciones con el parsing exacto.
"""

import os
import httpx
import logging
from typing import Optional
from app.models import TournamentLevel

logger = logging.getLogger(__name__)

MATCHSTAT_HOST    = "tennis-api-atp-wta-itf.p.rapidapi.com"
MATCHSTAT_BASE    = f"https://{MATCHSTAT_HOST}"

MATCHSTAT_MIN_WIN_PCT    = float(os.getenv("MATCHSTAT_MIN_WIN_PCT", "0.60"))
MATCHSTAT_MIN_H2H        = int(os.getenv("MATCHSTAT_MIN_H2H_MATCHES", "3"))

# Cache de player IDs para evitar searches repetidos en un mismo ciclo
_player_id_cache: dict[str, int | None] = {}


def _headers() -> dict:
    api_key = os.getenv("MATCHSTAT_API_KEY", "")
    return {
        "x-rapidapi-key":  api_key,
        "x-rapidapi-host": MATCHSTAT_HOST,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Paso 1: buscar player ID por nombre
# ─────────────────────────────────────────────────────────────────────────────

async def _search_player_id(name: str) -> Optional[int]:
    """
    Llama a /tennis/v2/search?search={name} y extrae el player ID.

    !! Actualiza _parse_player_id() con la estructura JSON real !!
    Ejecuta explore_matchstat_api.py para ver el response de search.
    """
    if name in _player_id_cache:
        return _player_id_cache[name]

    api_key = os.getenv("MATCHSTAT_API_KEY")
    if not api_key:
        return None

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                f"{MATCHSTAT_BASE}/tennis/v2/search",
                params={"search": name},
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            logger.debug(f"Search '{name}': {str(data)[:400]}")
            player_id = _parse_player_id(data, name)
            _player_id_cache[name] = player_id
            return player_id

    except httpx.HTTPStatusError as e:
        logger.error(f"Search error {e.response.status_code} for '{name}': {e.response.text[:200]}")
        return None
    except Exception as e:
        logger.error(f"Search failed for '{name}': {e}")
        return None


def _parse_player_id(data: dict | list, name: str) -> Optional[int]:
    """
    Extrae el player ID del response de /tennis/v2/search.

    !! ACTUALIZA ESTE BLOQUE con la estructura JSON real del endpoint !!

    Para saber qué estructura tiene el response:
      - Ejecuta scripts/explore_matchstat_api.py
      - Mira la sección 1 (SEARCH) y copia el JSON aquí

    Patrones comunes en tennis APIs:
      [{"id": 123, "name": "Jannik Sinner", "type": "player"}, ...]
      {"players": [{"player_id": 123, "full_name": "Sinner J."}]}
      {"results": [{"id": 123, "label": "Sinner, Jannik"}]}
    """
    # Log para depuración inicial — quita esta línea una vez que funcione
    logger.info(f"Search raw response for '{name}': {str(data)[:600]}")

    # --- BLOQUE A ACTUALIZAR ---
    # Reemplaza esto con el parsing real según el JSON que veas en el script.
    #
    # Ejemplo (ajusta los field names al response real):
    #
    # # Si data es una lista directa de resultados:
    # if isinstance(data, list):
    #     for item in data:
    #         if not isinstance(item, dict):
    #             continue
    #         item_name = (item.get("name") or item.get("full_name") or
    #                      item.get("label") or "").lower()
    #         if name.lower() in item_name or item_name in name.lower():
    #             for id_field in ["id", "player_id", "playerId"]:
    #                 if item.get(id_field):
    #                     return int(item[id_field])
    #
    # # Si data es un dict con una clave de resultados:
    # if isinstance(data, dict):
    #     for key in ["results", "players", "data", "items"]:
    #         results = data.get(key, [])
    #         if isinstance(results, list):
    #             for item in results:
    #                 if not isinstance(item, dict):
    #                     continue
    #                 for id_field in ["id", "player_id", "playerId"]:
    #                     if item.get(id_field):
    #                         return int(item[id_field])

    return None  # ← Quita esto una vez implementado el parsing real


# ─────────────────────────────────────────────────────────────────────────────
# Paso 2: obtener H2H histórico entre dos jugadores
# ─────────────────────────────────────────────────────────────────────────────

async def _get_h2h_win_pct(
    id_fav: int,
    id_dog: int,
    is_wta: bool = False,
) -> tuple[Optional[float], int]:
    """
    Llama a /tennis/v2/{tour}/h2h/stats/{id_fav}/{id_dog}/
    y devuelve (win_pct_del_favorito, total_partidos_h2h).

    !! Actualiza _parse_h2h_wins() con la estructura JSON real !!
    """
    tour = "wta" if is_wta else "atp"
    path = f"/tennis/v2/{tour}/h2h/stats/{id_fav}/{id_dog}/"

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                f"{MATCHSTAT_BASE}{path}",
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            logger.debug(f"H2H {id_fav} vs {id_dog}: {str(data)[:400]}")
            wins_fav, total = _parse_h2h_wins(data, id_fav)
            if total == 0:
                return None, 0
            return wins_fav / total, total

    except httpx.HTTPStatusError as e:
        logger.error(f"H2H error {e.response.status_code} ({path}): {e.response.text[:200]}")
        return None, 0
    except Exception as e:
        logger.error(f"H2H failed ({path}): {e}")
        return None, 0


def _parse_h2h_wins(data: dict | list, id_fav: int) -> tuple[int, int]:
    """
    Extrae (partidos_ganados_por_fav, total_partidos) del response H2H.

    Estructura real del endpoint /tennis/v2/atp/h2h/stats/{id1}/{id2}/:
    {
      "data": {
        "matchesCount": "60",
        "player1Stats": {"id": "5992", "matchesWon": 31, ...},
        "player2Stats": {"id": "677",  "matchesWon": 29, ...}
      }
    }
    """
    if not isinstance(data, dict):
        return 0, 0
    inner = data.get("data", {})
    if not isinstance(inner, dict):
        return 0, 0
    try:
        total = int(inner.get("matchesCount", 0))
    except (TypeError, ValueError):
        return 0, 0
    if total == 0:
        return 0, 0
    p1 = inner.get("player1Stats", {}) or {}
    p2 = inner.get("player2Stats", {}) or {}
    if str(p1.get("id", "")) == str(id_fav):
        return int(p1.get("matchesWon", 0)), total
    elif str(p2.get("id", "")) == str(id_fav):
        return int(p2.get("matchesWon", 0)), total
    logger.warning(f"H2H: fav_id={id_fav} no encontrado en p1.id={p1.get('id')} / p2.id={p2.get('id')}")
    return 0, 0


# ─────────────────────────────────────────────────────────────────────────────
# API pública — usada por automation.py
# ─────────────────────────────────────────────────────────────────────────────

async def get_player_win_probability(
    player_fav: str,
    player_dog: str,
    tournament_level: Optional[TournamentLevel] = None,
) -> Optional[float]:
    """
    Devuelve la probabilidad de victoria del favorito según el H2H histórico
    entre player_fav y player_dog (0.0–1.0), o None si no hay datos.

    Usado por automation.py para confirmar señales BUY antes de poner órdenes.
    """
    api_key = os.getenv("MATCHSTAT_API_KEY")
    if not api_key:
        logger.warning("MATCHSTAT_API_KEY no configurada — skip confirmación Matchstat")
        return None

    # Buscar IDs de ambos jugadores
    id_fav = await _search_player_id(player_fav)
    id_dog = await _search_player_id(player_dog)

    if id_fav is None or id_dog is None:
        logger.warning(
            f"No se pudo obtener player ID — fav='{player_fav}'({id_fav})"
            f" dog='{player_dog}'({id_dog})"
        )
        return None

    # Determinar si es WTA
    is_wta = tournament_level == TournamentLevel.WTA

    # Obtener H2H histórico
    win_pct, total = await _get_h2h_win_pct(id_fav, id_dog, is_wta=is_wta)

    if win_pct is None:
        logger.info(f"H2H sin datos para {player_fav} vs {player_dog}")
        return None

    if total < MATCHSTAT_MIN_H2H:
        logger.info(
            f"H2H insuficiente: {player_fav} vs {player_dog} — "
            f"{total} partidos (mínimo {MATCHSTAT_MIN_H2H})"
        )
        return None  # Pocos datos → no confirmamos ni rechazamos

    logger.info(
        f"H2H {player_fav} vs {player_dog}: "
        f"win_pct={win_pct:.0%} ({total} partidos)"
    )
    return win_pct


def confirms_signal(win_probability: Optional[float]) -> bool:
    """True si el H2H win% del favorito supera el umbral mínimo."""
    if win_probability is None:
        return False
    return win_probability >= MATCHSTAT_MIN_WIN_PCT
