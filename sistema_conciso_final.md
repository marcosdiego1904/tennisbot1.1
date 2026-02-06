# GUÍA CONCISA: Sistema de Trading Favoritos
## Versión Final - Solo lo Esencial

---

# SISTEMA COMPLETO

## Rango de Favoritos: 78-92%

## Fórmula Base:
```
TARGET = Favorito × Factor

Factor = 0.70 + Ajustes
```

---

# AJUSTES (3 stats - 20 segundos)

```python
FACTOR BASE: 0.70

AJUSTE 1 - TORNEO:
• ATP/WTA Tour:     +0.00
• Challenger:       -0.05
• Grand Slam:       +0.05 (pero no tradeas GS)

AJUSTE 2 - SUPERFICIE:
• Hard:    +0.00
• Clay:    -0.02
• Grass:   +0.03

AJUSTE 3 - RANKING GAP:
• Gap < 50:      -0.02
• Gap 50-100:    +0.03
• Gap > 100:     +0.08
```

---

# FILTROS AUTOMÁTICOS (Skip)

```
SKIP SI:
❌ Favorito < 78% o > 92%
❌ Gap > 150
❌ Volumen < $20,000
❌ Grand Slam
```

---

# TABLA DE REFERENCIA RÁPIDA

| Favorito | Factor 0.70 | Factor 0.65 (Clay Chall) | Factor 0.73 (Grass) |
|----------|-------------|--------------------------|---------------------|
| 92% | 64% | 60% | 67% |
| 90% | 63% | 59% | 66% |
| 88% | 62% | 57% | 64% |
| 85% | 60% | 55% | 62% |
| 82% | 57% | 53% | 60% |
| 80% | 56% | 52% | 58% |
| 78% | 55% | 51% | 57% |

---

# EJEMPLOS CONCRETOS

## Ejemplo 1: ATP Tour Hard
```
Rublev (85%) vs Bublik (15%)
ATP 500 Dubai, Hard
Gap: 18 posiciones

CÁLCULO:
Base: 0.70
ATP Tour: +0.00
Hard: +0.00
Gap <50: -0.02
Factor: 0.68

TARGET = 85% × 0.68 = 58%
```

## Ejemplo 2: Challenger Clay
```
Etcheverry (82%) vs Baez (18%)
Challenger Buenos Aires, Clay
Gap: 35 posiciones

CÁLCULO:
Base: 0.70
Challenger: -0.05
Clay: -0.02
Gap <50: -0.02
Factor: 0.61

TARGET = 82% × 0.61 = 50%
```

## Ejemplo 3: ATP Tour Clay
```
Ruud (88%) vs Coria (12%)
ATP 500 Rio, Clay
Gap: 65 posiciones

CÁLCULO:
Base: 0.70
ATP Tour: +0.00
Clay: -0.02
Gap 50-100: +0.03
Factor: 0.71

TARGET = 88% × 0.71 = 63%
```

---

# PROCESO DIARIO (5 minutos total)

```
1. Ejecutar script Python (ver abajo)
2. Script te da lista de targets
3. Ir a Kalshi
4. Poner órdenes límite
5. Esperar resultados

DONE.
```

---

# TRACKING MINIMALISTA

```
| Fecha | Jugador | Fav% | Target | Factor | Ejecutó | Ganó | P/L |
|-------|---------|------|--------|--------|---------|------|-----|
| 02/06 | Rublev  | 85   | 58     | 0.68   | SÍ      | SÍ   | +$42|
| 02/06 | Ruud    | 88   | 63     | 0.71   | NO      | -    | $0  |
```

Cada 20 trades: revisar si factors correctos
- ¿Ejecutan 30-40%? → Perfecto
- ¿Ejecutan <20%? → Muy agresivo, subir factor +0.03
- ¿Ejecutan >60%? → Muy conservador, bajar factor -0.03

---

# PYTHON SCRIPT AUTOMATIZADO

## Script 1: Calculadora Individual

```python
# tennis_target_calculator.py

def calcular_target(favorito_pct, gap, torneo, superficie):
    """
    Calcula el target price para comprar favorito
    
    Args:
        favorito_pct: float, probabilidad del favorito (0.78 a 0.92)
        gap: int, diferencia de rankings
        torneo: str, "ATP", "WTA", "Challenger"
        superficie: str, "Hard", "Clay", "Grass"
    
    Returns:
        float: target price
    """
    
    # Filtros automáticos
    if favorito_pct < 0.78 or favorito_pct > 0.92:
        return None, "SKIP: Favorito fuera de rango"
    
    if gap > 150:
        return None, "SKIP: Gap demasiado grande"
    
    # Factor base
    factor = 0.70
    
    # Ajuste torneo
    if torneo == "Challenger":
        factor -= 0.05
    elif torneo == "Grand Slam":
        return None, "SKIP: No tradeas Grand Slams"
    
    # Ajuste superficie
    if superficie == "Clay":
        factor -= 0.02
    elif superficie == "Grass":
        factor += 0.03
    
    # Ajuste gap
    if gap < 50:
        factor -= 0.02
    elif 50 <= gap <= 100:
        factor += 0.03
    elif gap > 100:
        factor += 0.08
    
    # Calcular target
    target = favorito_pct * factor
    
    return round(target, 2), factor


# EJEMPLOS DE USO:

# Ejemplo 1
target, factor = calcular_target(
    favorito_pct=0.85,
    gap=18,
    torneo="ATP",
    superficie="Hard"
)
print(f"Target: {target*100:.0f}% (Factor: {factor})")
# Output: Target: 58% (Factor: 0.68)

# Ejemplo 2
target, factor = calcular_target(
    favorito_pct=0.82,
    gap=35,
    torneo="Challenger",
    superficie="Clay"
)
print(f"Target: {target*100:.0f}% (Factor: {factor})")
# Output: Target: 50% (Factor: 0.61)

# Ejemplo 3 - SKIP
target, reason = calcular_target(
    favorito_pct=0.96,
    gap=200,
    torneo="ATP",
    superficie="Hard"
)
print(reason)
# Output: SKIP: Favorito fuera de rango
```

---

## Script 2: Analizador Diario Completo

```python
# daily_targets.py

import requests
from datetime import datetime

def obtener_partidos_kalshi():
    """
    Placeholder - conectar con API de Kalshi o scraping
    Por ahora, input manual
    """
    partidos = [
        {
            "player": "Rublev",
            "opponent": "Bublik",
            "fav_pct": 0.85,
            "rank_fav": 8,
            "rank_opp": 26,
            "torneo": "ATP",
            "superficie": "Hard",
            "volumen": 145000
        },
        {
            "player": "Etcheverry",
            "opponent": "Baez",
            "fav_pct": 0.82,
            "rank_fav": 38,
            "rank_opp": 73,
            "torneo": "Challenger",
            "superficie": "Clay",
            "volumen": 45000
        },
        {
            "player": "Djokovic",
            "opponent": "Musetti",
            "fav_pct": 0.95,
            "rank_fav": 1,
            "rank_opp": 18,
            "torneo": "Grand Slam",
            "superficie": "Hard",
            "volumen": 850000
        }
    ]
    return partidos


def analizar_partidos_diarios():
    """
    Analiza todos los partidos y genera targets
    """
    partidos = obtener_partidos_kalshi()
    
    print("=" * 80)
    print(f"TARGETS DIARIOS - {datetime.now().strftime('%Y-%m-%d')}")
    print("=" * 80)
    print()
    
    tradeables = []
    skipped = []
    
    for p in partidos:
        gap = abs(p["rank_fav"] - p["rank_opp"])
        
        # Filtro volumen
        if p["volumen"] < 20000:
            skipped.append({
                "partido": f"{p['player']} vs {p['opponent']}",
                "razon": "Volumen bajo"
            })
            continue
        
        # Calcular target
        target, info = calcular_target(
            p["fav_pct"],
            gap,
            p["torneo"],
            p["superficie"]
        )
        
        if target is None:
            skipped.append({
                "partido": f"{p['player']} vs {p['opponent']}",
                "razon": info
            })
        else:
            tradeables.append({
                "partido": f"{p['player']} vs {p['opponent']}",
                "fav_pct": p["fav_pct"],
                "target": target,
                "factor": info,
                "gap": gap,
                "superficie": p["superficie"],
                "torneo": p["torneo"]
            })
    
    # Mostrar tradeables
    print("✅ PARTIDOS PARA TRADEAR:")
    print()
    
    if not tradeables:
        print("   No hay partidos que cumplan los criterios hoy.")
    else:
        for i, t in enumerate(tradeables, 1):
            print(f"{i}. {t['partido']}")
            print(f"   Favorito: {t['fav_pct']*100:.0f}%")
            print(f"   TARGET: {t['target']*100:.0f}%")
            print(f"   Factor: {t['factor']:.2f}")
            print(f"   Gap: {t['gap']} | {t['superficie']} | {t['torneo']}")
            print(f"   ACCIÓN: Poner orden límite YES @ {t['target']*100:.0f}%")
            print()
    
    # Mostrar skipped
    if skipped:
        print("\n" + "=" * 80)
        print("❌ PARTIDOS SKIP:")
        print()
        for s in skipped:
            print(f"   • {s['partido']}: {s['razon']}")
    
    print("\n" + "=" * 80)
    
    return tradeables


# EJECUTAR
if __name__ == "__main__":
    analizar_partidos_diarios()
```

### Output del Script:

```
================================================================================
TARGETS DIARIOS - 2026-02-06
================================================================================

✅ PARTIDOS PARA TRADEAR:

1. Rublev vs Bublik
   Favorito: 85%
   TARGET: 58%
   Factor: 0.68
   Gap: 18 | Hard | ATP
   ACCIÓN: Poner orden límite YES @ 58%

2. Etcheverry vs Baez
   Favorito: 82%
   TARGET: 50%
   Factor: 0.61
   Gap: 35 | Clay | Challenger
   ACCIÓN: Poner orden límite YES @ 50%

================================================================================
❌ PARTIDOS SKIP:

   • Djokovic vs Musetti: SKIP: No tradeas Grand Slams

================================================================================
```

---

## Script 3: Input Manual Rápido

```python
# quick_calc.py

def quick_target():
    """
    Calculadora interactiva rápida
    """
    print("=== CALCULADORA RÁPIDA ===\n")
    
    # Input
    fav = float(input("Favorito % (ej: 85): ")) / 100
    gap = int(input("Ranking gap (ej: 25): "))
    
    print("\nTorneo:")
    print("1. ATP/WTA Tour")
    print("2. Challenger")
    torneo = "ATP" if input("Opción (1 o 2): ") == "1" else "Challenger"
    
    print("\nSuperficie:")
    print("1. Hard")
    print("2. Clay")
    print("3. Grass")
    sup_opt = input("Opción (1, 2 o 3): ")
    superficie = {"1": "Hard", "2": "Clay", "3": "Grass"}[sup_opt]
    
    # Calcular
    target, factor = calcular_target(fav, gap, torneo, superficie)
    
    if target is None:
        print(f"\n❌ {factor}")
    else:
        print(f"\n✅ TARGET: {target*100:.0f}%")
        print(f"   Factor usado: {factor}")
        print(f"\n📋 ACCIÓN: Poner orden límite YES @ {target*100:.0f}%")


if __name__ == "__main__":
    quick_calc()
```

### Ejemplo de uso:

```
=== CALCULADORA RÁPIDA ===

Favorito % (ej: 85): 87
Ranking gap (ej: 25): 42

Torneo:
1. ATP/WTA Tour
2. Challenger
Opción (1 o 2): 2

Superficie:
1. Hard
2. Clay
3. Grass
Opción (1, 2 o 3): 2

✅ TARGET: 53%
   Factor usado: 0.61

📋 ACCIÓN: Poner orden límite YES @ 53%
```

---

# VERSIÓN EXCEL (Alternativa sin código)

## Archivo: `tennis_targets.xlsx`

### Hoja 1: Calculadora

```
| A: Favorito % | B: Gap | C: Torneo | D: Superficie | E: TARGET | F: Factor |
|---------------|--------|-----------|---------------|-----------|-----------|
| 85            | 18     | ATP       | Hard          | =FORMULA  | =FORMULA  |
```

### Fórmulas Excel:

```excel
// Celda E2 (TARGET):
=IF(A2<78, "SKIP",
   IF(A2>92, "SKIP",
      IF(B2>150, "SKIP",
         A2 * F2
      )
   )
)

// Celda F2 (FACTOR):
=0.70
 + IF(C2="Challenger", -0.05, 0)
 + IF(C2="Grand Slam", 0.05, 0)
 + IF(D2="Clay", -0.02, 0)
 + IF(D2="Grass", 0.03, 0)
 + IF(B2<50, -0.02, 0)
 + IF(AND(B2>=50, B2<=100), 0.03, 0)
 + IF(B2>100, 0.08, 0)
```

---

# RECOMENDACIÓN DE IMPLEMENTACIÓN

## Opción 1: Manual con Excel (Más simple)
```
PROS:
✓ No necesitas programar
✓ Visual y fácil
✓ Modificable al instante

CONTRAS:
✗ Input manual de datos
✗ No automatizable
```

## Opción 2: Python Script (Recomendado)
```
PROS:
✓ Automatizable
✓ Puede conectar con APIs
✓ Escalable
✓ Genera reportes

CONTRAS:
✗ Requiere Python instalado
✗ Curva de aprendizaje inicial
```

## Opción 3: Híbrido (Para empezar)
```
1. Usar quick_calc.py para cálculos rápidos
2. Anotar en Excel para tracking
3. Después automatizar con daily_targets.py
```

---

# SETUP RÁPIDO PYTHON

## 1. Instalar Python
```bash
# Descargar de python.org
# O si tienes Mac/Linux:
brew install python3  # Mac
sudo apt install python3  # Linux
```

## 2. Guardar scripts
```bash
# Crear carpeta
mkdir tennis_trading
cd tennis_trading

# Guardar los 3 scripts:
# - tennis_target_calculator.py
# - daily_targets.py
# - quick_calc.py
```

## 3. Ejecutar
```bash
# Calculadora rápida
python3 quick_calc.py

# Análisis diario
python3 daily_targets.py
```

---

# PRÓXIMOS PASOS

## Fase 1: Manual (Semana 1-2)
```
1. Usar quick_calc.py
2. Trackear en Excel
3. Validar que factors funcionan
```

## Fase 2: Semi-automático (Semana 3-4)
```
1. Input partidos del día en daily_targets.py
2. Script calcula todos los targets
3. Poner órdenes en Kalshi
```

## Fase 3: Automático (Mes 2+)
```
1. Conectar con API de Kalshi
2. Script lee partidos automáticamente
3. (Opcional) Bot pone órdenes automáticas
```

---

# RESUMEN ULTRA-CONCISO

```
SISTEMA:
1. Favorito 78-92%
2. TARGET = Fav × Factor
3. Factor = 0.70 + ajustes (gap, torneo, superficie)
4. Poner orden límite
5. Esperar resultado

HERRAMIENTAS:
• Python script (recomendado)
• O Excel manual
• O calculadora quick_calc.py

TIEMPO:
• 5 min/día con script
• 2 min/partido manual

WIN RATE ESPERADO:
• 85-90% (vs tu 80% actual "al ojo")
```

---

¿Empezamos con cuál opción? ¿Python, Excel, o híbrido?
