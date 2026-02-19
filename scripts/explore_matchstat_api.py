"""
Script de exploración de la Matchstat API (via RapidAPI).

Ejecuta este script EN TU MÁQUINA LOCAL:
    python scripts/explore_matchstat_api.py

Prueba los endpoints conocidos con datos reales y muestra el JSON completo
para que podamos mapear el parsing correcto en matchstat_client.py.
"""

import http.client
import json

API_KEY  = "563ea39f66msh0512ba3143fdccfp1bb39fjsnde81db4498b1"
HOST     = "tennis-api-atp-wta-itf.p.rapidapi.com"
HEADERS  = {
    "x-rapidapi-key":  API_KEY,
    "x-rapidapi-host": HOST,
}


# ─────────────────────────────────────────────────────────────────────────────
def get(path: str) -> tuple[int, dict | list | None]:
    conn = http.client.HTTPSConnection(HOST, timeout=10)
    try:
        conn.request("GET", path, headers=HEADERS)
        res  = conn.getresponse()
        raw  = res.read().decode("utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"_raw": raw[:500]}
        return res.status, data
    except Exception as e:
        return 0, {"_error": str(e)}
    finally:
        conn.close()


def section(title: str):
    print(f"\n{'='*65}")
    print(f"  {title}")
    print("=" * 65)


def show(data, max_chars=4000):
    txt = json.dumps(data, indent=2, ensure_ascii=False)
    if len(txt) > max_chars:
        txt = txt[:max_chars] + "\n  ...(truncado)"
    print(txt)


# ─────────────────────────────────────────────────────────────────────────────
# 1. SEARCH — obtener player IDs para dos jugadores reales
# ─────────────────────────────────────────────────────────────────────────────
section("1. SEARCH — buscar jugadores para obtener sus IDs")

# Cambia estos nombres por los del partido que estés analizando ahora
PLAYER_FAV = "Sinner"
PLAYER_DOG = "Medvedev"

player_ids: dict[str, int | None] = {PLAYER_FAV: None, PLAYER_DOG: None}

for name in [PLAYER_FAV, PLAYER_DOG]:
    status, data = get(f"/tennis/v2/search?search={name}")
    print(f"\n  Search '{name}' → status {status}")
    show(data)

    # Intentar extraer player ID automáticamente
    # (ajusta el path una vez que veas la estructura real)
    if isinstance(data, list) and data:
        for item in data:
            if isinstance(item, dict):
                # Buscar campo que parezca un ID de jugador
                for id_field in ["id", "player_id", "playerId", "ID"]:
                    if id_field in item:
                        player_ids[name] = item[id_field]
                        print(f"  → ID detectado para '{name}': {item[id_field]}")
                        break
                if player_ids[name]:
                    break
    elif isinstance(data, dict):
        results = data.get("results") or data.get("players") or data.get("data") or []
        if isinstance(results, list) and results:
            first = results[0]
            for id_field in ["id", "player_id", "playerId"]:
                if id_field in first:
                    player_ids[name] = first[id_field]
                    print(f"  → ID detectado para '{name}': {first[id_field]}")
                    break

print(f"\n  IDs extraídos: {player_ids}")

# ─────────────────────────────────────────────────────────────────────────────
# 2. H2H STATS — historial entre los dos jugadores
# ─────────────────────────────────────────────────────────────────────────────
section("2. H2H STATS — historial directo entre los dos jugadores")

# IDs de ejemplo de la documentación (5992 y 677) — usa los reales si los tienes
id1 = player_ids.get(PLAYER_FAV) or 5992
id2 = player_ids.get(PLAYER_DOG) or 677

status, data = get(f"/tennis/v2/atp/h2h/stats/{id1}/{id2}/")
print(f"  /tennis/v2/atp/h2h/stats/{id1}/{id2}/ → status {status}")
show(data)

# También probar WTA por si acaso
section("2b. H2H STATS WTA (si es partida femenina)")
status_wta, data_wta = get(f"/tennis/v2/wta/h2h/stats/{id1}/{id2}/")
print(f"  /tennis/v2/wta/h2h/stats/{id1}/{id2}/ → status {status_wta}")
if status_wta == 200:
    show(data_wta)
else:
    print(f"  (no disponible: {data_wta})")

# ─────────────────────────────────────────────────────────────────────────────
# 3. H2H MATCH STATS — stats de un partido específico
# ─────────────────────────────────────────────────────────────────────────────
section("3. H2H MATCH STATS — detalle de una partida puntual (IDs ejemplo)")

status, data = get("/tennis/v2/atp/h2h/match-stats/19400/5992/677")
print(f"  /tennis/v2/atp/h2h/match-stats/19400/5992/677 → status {status}")
show(data)

# ─────────────────────────────────────────────────────────────────────────────
# 4. Buscar endpoint con partidos de HOY (necesitamos match IDs actuales)
# ─────────────────────────────────────────────────────────────────────────────
section("4. SCHEDULE / FIXTURES — buscar partidos de hoy")

SCHEDULE_CANDIDATES = [
    "/tennis/v2/atp/schedule",
    "/tennis/v2/atp/fixtures",
    "/tennis/v2/atp/matches",
    "/tennis/v2/atp/matches/today",
    "/tennis/v2/atp/live",
    "/tennis/v2/atp/live-scores",
    "/tennis/v2/schedule",
    "/tennis/v2/matches",
    "/tennis/v2/live",
    "/tennis/v2/atp/results",
    "/tennis/v2/atp/draws",
]

schedule_working = []
for path in SCHEDULE_CANDIDATES:
    s, d = get(path)
    ok = s == 200
    print(f"  {'✓' if ok else '✗'} [{s:3d}] {path}")
    if ok:
        schedule_working.append((path, d))

if schedule_working:
    section("4b. Respuestas de schedule disponibles")
    for path, d in schedule_working:
        print(f"\n  --- {path} ---")
        show(d)

# ─────────────────────────────────────────────────────────────────────────────
# 5. ANÁLISIS AUTOMÁTICO — extraer campos de win probability del H2H
# ─────────────────────────────────────────────────────────────────────────────
section("5. ANÁLISIS — campos de win% en la respuesta H2H")

def find_win_fields(obj, path="", out=None):
    if out is None:
        out = []
    keywords = ["win", "pct", "prob", "percent", "ratio", "total", "count",
                "won", "lost", "stat", "match"]
    if isinstance(obj, dict):
        for k, v in obj.items():
            fp = f"{path}.{k}" if path else k
            if any(kw in str(k).lower() for kw in keywords):
                out.append(f"  {fp} = {repr(v)[:80]}")
            find_win_fields(v, fp, out)
    elif isinstance(obj, list) and obj:
        find_win_fields(obj[0], f"{path}[0]", out)
    return out

_, h2h_data = get(f"/tennis/v2/atp/h2h/stats/{id1}/{id2}/")
fields = find_win_fields(h2h_data)
if fields:
    print(f"\n  Campos relevantes en H2H stats:")
    for f in fields[:30]:
        print(f)
else:
    print("  (no se detectaron campos de win% — revisa la respuesta completa en sección 2)")

# ─────────────────────────────────────────────────────────────────────────────
# RESUMEN
# ─────────────────────────────────────────────────────────────────────────────
section("PRÓXIMOS PASOS")
print(f"""
IDs encontrados:
  {PLAYER_FAV}: {player_ids.get(PLAYER_FAV) or '⚠️  no detectado — revisa respuesta de search'}
  {PLAYER_DOG}: {player_ids.get(PLAYER_DOG) or '⚠️  no detectado — revisa respuesta de search'}

Con esos datos pégame en el chat:
  1. El JSON de search (sección 1) para un jugador — para saber cómo extraer el ID
  2. El JSON de H2H stats (sección 2) — para saber qué campo da el win count/pct
  3. Si encontraste un endpoint de schedule (sección 4) que funcione

Con eso actualizo matchstat_client.py para que el parser sea exacto.
""")
