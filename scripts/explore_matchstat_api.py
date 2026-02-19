"""
Script de exploración de la Matchstat API (via RapidAPI).

Ejecuta este script EN TU MÁQUINA LOCAL (no en el servidor):
    python scripts/explore_matchstat_api.py

Lo que hace:
  1. Prueba endpoints comunes para encontrar cuál devuelve predicciones/ganadores
  2. Imprime el JSON completo de cada respuesta exitosa
  3. Te dice qué campos contienen win probability / ganador predicho

Una vez que sepas cuál endpoint funciona y qué estructura devuelve,
actualiza app/matchstat_client.py con ese endpoint y el parsing correcto.
"""

import http.client
import json
import sys

API_KEY = "563ea39f66msh0512ba3143fdccfp1bb39fjsnde81db4498b1"
HOST = "tennis-api-atp-wta-itf.p.rapidapi.com"

HEADERS = {
    "x-rapidapi-key": API_KEY,
    "x-rapidapi-host": HOST,
}


def get(path: str) -> tuple[int, dict | list | None]:
    """Make a GET request and return (status_code, parsed_json)."""
    conn = http.client.HTTPSConnection(HOST, timeout=10)
    try:
        conn.request("GET", path, headers=HEADERS)
        res = conn.getresponse()
        raw = res.read().decode("utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"raw": raw[:500]}
        return res.status, data
    except Exception as e:
        return 0, {"error": str(e)}
    finally:
        conn.close()


def pretty(data, max_chars=3000):
    txt = json.dumps(data, indent=2, ensure_ascii=False)
    if len(txt) > max_chars:
        txt = txt[:max_chars] + "\n  ... (truncated)"
    return txt


def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# 1. Known endpoint — search
# ---------------------------------------------------------------------------
section("1. /tennis/v2/search?search=Sinner")
status, data = get("/tennis/v2/search?search=Sinner")
print(f"Status: {status}")
print(pretty(data))

# ---------------------------------------------------------------------------
# 2. Discover available endpoints — common patterns
# ---------------------------------------------------------------------------
CANDIDATES = [
    # Predictions / odds
    "/tennis/v2/predictions",
    "/tennis/v2/odds",
    "/tennis/v2/predictions/today",
    "/tennis/v2/matches/predictions",

    # Matches / schedule
    "/tennis/v2/matches",
    "/tennis/v2/matches/today",
    "/tennis/v2/schedule",
    "/tennis/v2/fixtures",
    "/tennis/v2/live",

    # Tournament / tour
    "/tennis/v2/atp",
    "/tennis/v2/wta",
    "/tennis/v2/tours",

    # Stats & head-to-head
    "/tennis/v2/h2h",
    "/tennis/v2/players",
    "/tennis/v2/rankings",

    # Root discovery
    "/tennis/v2/",
    "/tennis/",
    "/",
]

section("2. Buscando endpoints disponibles...")
working = []
for path in CANDIDATES:
    status, data = get(path)
    ok = status == 200
    print(f"  {'✓' if ok else '✗'} [{status}] {path}")
    if ok:
        working.append((path, data))

# ---------------------------------------------------------------------------
# 3. Print full responses for working endpoints
# ---------------------------------------------------------------------------
if working:
    section("3. Respuestas completas de endpoints exitosos")
    for path, data in working:
        print(f"\n--- {path} ---")
        print(pretty(data))
else:
    print("\n⚠️  Ningún endpoint candidato respondió con 200.")
    print("   Prueba inspeccionar la pestaña 'Endpoints' en RapidAPI:")
    print(f"   https://rapidapi.com/jjrm365-kIFr3Nx_odV/api/tennis-api-atp-wta-itf")

# ---------------------------------------------------------------------------
# 4. If we found match data, search for prediction-related fields
# ---------------------------------------------------------------------------
section("4. Análisis — campos con probabilidades o ganadores")

def find_probability_fields(obj, path="", results=None):
    """Recursively search for fields that look like win probabilities."""
    if results is None:
        results = []
    keywords = ["prob", "pct", "percent", "win", "predict", "odd", "chance", "favorite", "winner"]
    if isinstance(obj, dict):
        for k, v in obj.items():
            full_path = f"{path}.{k}" if path else k
            if any(kw in k.lower() for kw in keywords):
                results.append(f"  FIELD: {full_path} = {repr(v)[:100]}")
            find_probability_fields(v, full_path, results)
    elif isinstance(obj, list) and obj:
        find_probability_fields(obj[0], f"{path}[0]", results)
    return results

for path, data in working:
    fields = find_probability_fields(data)
    if fields:
        print(f"\n  Endpoint: {path}")
        for f in fields[:20]:
            print(f)

print("\n" + "=" * 60)
print("  PRÓXIMOS PASOS")
print("=" * 60)
print("""
1. Identifica qué endpoint devuelve los partidos de hoy con ganador predicho
2. Anota el path exacto (ej: /tennis/v2/predictions) y la estructura JSON
3. Actualiza en app/matchstat_client.py:
   - MATCHSTAT_ENDPOINT en .env → el path correcto
   - La función _parse_win_probability() → extrae el % del JSON real
4. Ejecuta el bot en dry-run para verificar que funciona

Si no ves el endpoint correcto arriba, ve a:
https://rapidapi.com/jjrm365-kIFr3Nx_odV/api/tennis-api-atp-wta-itf
→ Pestaña "Endpoints" → prueba cada uno interactivamente
""")
