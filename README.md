# TennisBot 1.1 — Tennis Favorites Trading Dashboard

A real-time trading analysis system for tennis match-winner markets on [Kalshi](https://kalshi.com). It calculates **limit order prices** for betting on heavy favorites, showing you exactly where to place your orders on Kalshi's order book.

## How It Works

### The Core Idea

When a player is a heavy favorite (70-92% implied probability), the system calculates a discounted **limit order price** using:

```
TARGET = Favorite_Probability × Factor
```

- **TARGET** is the price (in cents) you place as a limit order on Kalshi
- **Factor** starts at 0.70 and adjusts for tournament level and surface
- The **Spread** shows how far the current market price is from your limit order

**Example**: If Sinner is an 80% favorite, TARGET = 0.80 × 0.70 = 56¢. If the market is at 80¢, the spread is 24¢. You place a limit buy at 56¢ and wait for the price to come to you.

### Factor Adjustments

| Adjustment     | Value   | Reasoning                                      |
|----------------|---------|------------------------------------------------|
| **Base**       | 0.70    | Core discount factor                           |
| ATP Tour       | +0.00   | Baseline                                       |
| WTA Tour       | +0.00   | Baseline                                       |
| Challenger     | -0.05   | More volatile → bigger discount                |
| Grand Slam     | +0.05   | Most predictable (but currently filtered out)  |
| Hard Court     | +0.00   | Baseline surface                               |
| Clay Court     | -0.02   | More upsets → bigger discount                  |
| Grass Court    | +0.03   | Favors favorites → smaller discount            |

### Filters (SKIP Conditions)

A match is **SKIP**ped if any of these are true:
- Favorite probability < 70% (not a clear enough favorite)
- Favorite probability > 92% (odds too short, not enough value)
- Volume < $100 (market too thin)
- Grand Slam tournament (currently excluded from trading)

All matches passing filters get a **BUY** signal, meaning: "place a limit order at TARGET."

---

## Architecture

```
tennisbot1.1/
├── main.py                  # FastAPI entry point, serves frontend
├── app/
│   ├── models.py            # Data classes: MatchData, AnalysisResult, Signal, etc.
│   ├── kalshi_client.py     # Kalshi API client: auth, series discovery, market parsing
│   ├── engine.py            # Decision engine: factor calculation, filters, signals
│   ├── tennis_data.py       # Tournament database loader
│   └── routes.py            # API endpoints: /api/analyze, /api/debug/kalshi, etc.
├── static/
│   ├── index.html           # Dashboard HTML
│   ├── app.js               # Frontend logic: fetch, render, filters, auto-refresh
│   └── styles.css           # Dark theme, grid layout, responsive design
├── data/
│   └── tournaments.json     # Static tournament → level/surface mapping
├── requirements.txt
└── .env.example
```

### Tech Stack
- **Backend**: Python 3.11+ / FastAPI / uvicorn
- **Frontend**: Vanilla HTML/JS/CSS (no build step)
- **Data Source**: Kalshi API v2 (RSA-PSS authentication)
- **Deployment**: Railway

---

## File-by-File Documentation

### `app/models.py` — Data Models

Defines the core data structures used throughout the system:

- **`TournamentLevel`** (enum): `ATP`, `WTA`, `Challenger`, `Grand Slam`
- **`Surface`** (enum): `Hard`, `Clay`, `Grass`
- **`Signal`** (enum): `BUY`, `WAIT`, `SKIP`
- **`PlayerInfo`**: Player name
- **`MatchData`**: All raw data for one match — players, probability, price, tournament info, volume, ticker, and `close_time` (ISO 8601 from Kalshi's `expected_expiration_time`)
- **`AnalysisResult`**: Engine output — signal, target price, factor, edge, skip reason

### `app/kalshi_client.py` — Kalshi API Client

The most complex file. Handles everything Kalshi:

#### Authentication (lines 41-87)
- Loads RSA private key from `KALSHI_API_SECRET` env var
- Signs each request with RSA-PSS SHA256: `{timestamp}{method}{path}`
- Sends headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP`
- The private key in env vars uses literal `\n` for newlines

#### Dynamic Series Discovery (lines 121-233)
Instead of hardcoding series tickers, the system dynamically discovers ALL tennis series:

1. **`GET /search/filters_by_sport`** — finds "Tennis" (exact match, not "Table Tennis") and its competitions
2. **`GET /search/tags_by_categories`** — finds `{"Sports": ["Tennis"]}` category/tag pair
3. **`GET /series?category=Sports&tags=Tennis`** — fetches all tennis series tickers
4. Falls back to `["KXATPMATCH", "KXWTAMATCH"]` if discovery fails
5. Results cached for 1 hour (`_SERIES_CACHE_TTL = 3600`)

**Known match-winner series** (contain "MATCH" in ticker):
- `KXATPMATCH` — ATP Tour matches (~78 markets)
- `KXWTAMATCH` — WTA Tour matches (~32 markets)
- `KXATPCHALLENGERMATCH` — ATP Challenger matches (~52 markets)
- `KXWTACHALLENGERMATCH` — WTA Challenger matches (~10 markets)
- `KXDAVISCUPMATCH`, `KXUNITEDCUPMATCH`, `KXSIXKINGSMATCH` — other events

Only "MATCH" series are queried for the trading engine (not games, futures, or field markets).

#### Market Fetching (lines 265-317)
- Paginates through all markets for each MATCH series (up to 10 pages × 100 per page)
- Tags each market with `_series_ticker` for tournament classification
- Deduplicates by `event_ticker` — each match has 2 markets (one per player), keeps the one with the higher price (the favorite)

#### Market Parsing — `_parse_market()` (lines 477-545)
Converts raw Kalshi market JSON into `MatchData`:

1. **Price extraction** (`_get_market_price`): Uses `last_price` → midpoint of `yes_bid/yes_ask` → `yes_ask` → `yes_bid`
2. **Player extraction**: Parses title format `"Will [FullName] win the [LastName1] vs [LastName2] : [Round] match?"` using regex. Falls back to `rules_primary` field
3. **Favorite detection**: If YES player's price ≥ 50, they're the favorite. Otherwise flip
4. **Tournament classification** (`_classify_tournament`): Uses series ticker first (reliable — `KXWTAMATCH` → WTA), then text matching for surface/name
5. **Time field**: Uses `expected_expiration_time` (approximate match end time) → `expiration_time` → `close_time` as fallback chain

#### Important Kalshi API Gotchas
- **Price fields**: `yes_bid`, `yes_ask`, `last_price` — there is NO `yes_price`
- **`/events` has no `search` param** — it's silently ignored
- **`/series` uses `category` and `tags` params** — not `search`
- **Competitions field** can be dict OR list — code handles both
- **`tags_by_categories`** — some categories have `None` values, must check before iterating
- **`expected_expiration_time`** — approximate match resolution time (NOT match start). `close_time` is when trading ends (can be 14 days later)

### `app/engine.py` — Decision Engine

Simple, stateless function: takes a `MatchData`, returns an `AnalysisResult`.

**Flow**:
1. Check SKIP filters (Grand Slam, probability out of range, low volume)
2. Calculate factor: `BASE_FACTOR (0.70) + tournament_adj + surface_adj`
3. Calculate target: `fav_probability × factor`, rounded to whole cents
4. Calculate spread: `kalshi_price - target` (positive = market above your order)
5. Return BUY signal with all data

**Sorting** (`analyze_all`): BUY first (sorted by tightest spread), then SKIP.

### `app/routes.py` — API Endpoints

| Endpoint              | Method | Description                                        |
|-----------------------|--------|----------------------------------------------------|
| `GET /api/analyze`    | GET    | Main endpoint: fetch markets, run engine, return results |
| `POST /api/analyze/manual` | POST | Manual calculator: input match data, get analysis |
| `GET /api/debug/kalshi` | GET  | Debug: shows series discovery, raw markets, parse results, time fields |
| `GET /api/health`     | GET    | Health check                                       |

### `app/tennis_data.py` — Tournament Database

Minimal file — loads `data/tournaments.json` which maps tournament names to level + surface. Used by `_classify_tournament` as a first-pass lookup before falling back to text matching.

### `static/app.js` — Frontend Logic

**State management**:
- `allResults` — all analysis results from the API
- `activeFilters` — `{ BUY: true, WAIT: true, SKIP: false }` (SKIP hidden by default)
- Auto-refresh: 60-second interval with countdown timer

**Key features**:
- **Signal filter toggles**: Click summary cards (BUY/WAIT/SKIP) to show/hide those matches
- **Tournament grouping**: Cards grouped under tournament headers with level badges
- **Date proximity sorting**: Within each signal group, matches sorted by soonest first (using `close_time` from `expected_expiration_time`)
- **Time countdown**: Each card shows estimated time to match resolution (e.g., "5h 3m", "1d 2h", "Live")
- **Manual calculator**: Collapsible panel for testing with custom inputs
- **Debug panel**: Shows raw Kalshi discovery chain, series counts, parse results

**Card display**:
- Signal badge (BUY/SKIP)
- Player names (favorite highlighted in blue)
- Time countdown badge (top-right)
- Meta tags: surface, tournament level, factor
- Detail line: favorite %, tournament name
- Price section: Market (current Kalshi price), Limit Order (your target), Spread (difference)

### `data/tournaments.json` — Tournament Database

Static mapping of ~60 tournament names to their level and surface. Used for:
- Accurate surface detection (Halle → Grass, Barcelona → Clay)
- Tournament level classification (used as a first-pass before series ticker classification)

---

## Environment Variables

| Variable           | Required | Description                                   |
|--------------------|----------|-----------------------------------------------|
| `KALSHI_API_KEY`   | Yes      | Kalshi API public key                         |
| `KALSHI_API_SECRET`| Yes      | Kalshi RSA private key (PEM format, `\n` for newlines) |
| `KALSHI_BASE_URL`  | No       | Defaults to `https://api.elections.kalshi.com/trade-api/v2` |
| `PORT`             | No       | Server port, defaults to `8000`               |

### RSA Key Setup
The `KALSHI_API_SECRET` env var contains your RSA private key in PEM format. In Railway (or any env var), replace actual newlines with the literal string `\n`:

```
KALSHI_API_SECRET=-----BEGIN RSA PRIVATE KEY-----\nMIIEowI...\n-----END RSA PRIVATE KEY-----
```

---

## Running Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Set up environment
cp .env.example .env
# Edit .env with your Kalshi API credentials

# Run
python main.py
# or
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000` and click **Refresh Markets**.

## Deploying to Railway

1. Connect GitHub repo to Railway
2. Set environment variables (`KALSHI_API_KEY`, `KALSHI_API_SECRET`)
3. Railway auto-detects Python + `requirements.txt`
4. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`

---

## Data Flow

```
User clicks "Refresh Markets"
        │
        ▼
GET /api/analyze
        │
        ▼
fetch_tennis_markets()
        │
        ├── _discover_tennis_series()     ← Finds all tennis series tickers
        │       ├── /search/filters_by_sport
        │       ├── /search/tags_by_categories
        │       └── /series?category=Sports&tags=Tennis
        │
        ├── For each MATCH series:        ← Only series with "MATCH" in ticker
        │       └── /markets?series_ticker=KXATPMATCH&status=open
        │
        ├── Deduplicate by event_ticker   ← Each match has 2 markets, keep favorite
        │
        └── _parse_market() for each      ← Extract players, price, tournament, time
                │
                ▼
        analyze_all(matches)
                │
                ├── For each match:
                │       ├── Check SKIP filters
                │       ├── Calculate factor (base + tournament + surface)
                │       ├── TARGET = probability × factor
                │       ├── Spread = market_price - target
                │       └── Return BUY or SKIP
                │
                └── Sort: BUY first (by spread), then SKIP
                        │
                        ▼
                JSON response → Frontend renders cards grouped by tournament
```

---

## Key Design Decisions

1. **Limit orders, not market orders**: TARGET is the price to SET on the order book, not a signal to buy immediately. Every passing match gets a BUY signal.

2. **Dynamic series discovery**: Instead of hardcoding tickers, we query Kalshi's discovery endpoints. This automatically picks up new series (e.g., when Challengers were added).

3. **MATCH-only filtering**: Of ~73 tennis series, only ~6 contain "MATCH" (match-winner markets). The rest are game markets, futures, field markets, etc. We only trade match-winners.

4. **Deduplication by event_ticker**: Each Kalshi match event has 2 markets (Player A wins YES, Player B wins YES). We keep the one with the higher price (the favorite's market).

5. **Series ticker for classification**: WTA tournaments were showing as "ATP" because text matching is unreliable. Using the series ticker (`KXWTAMATCH` → WTA, `KXATPCHALLENGERMATCH` → Challenger) is authoritative.

6. **`expected_expiration_time` for match timing**: Kalshi's `close_time` is when trading ends (~14 days out). `expected_expiration_time` is when the market expects to resolve (close to match end time). Note: this is approximate match end time, NOT match start time.

7. **Whole-cent target prices**: Kalshi's order book works in whole cents. Targets are rounded to the nearest cent.

8. **No external data dependencies**: Rankings were removed. The system uses only Kalshi market data. Factor adjustments come from tournament level and surface only.
