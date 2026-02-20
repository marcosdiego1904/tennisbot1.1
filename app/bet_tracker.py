"""
Bet Tracker — manual bet tracking with outcome recording.

Flow:
  Moment 1 (automatic): User clicks "Track" on a card → snapshot saved
  Moment 2 (manual): After match, user enters lowest_price + match_outcome
  Calculated: order_filled, fill_price, edge, pnl derived automatically

Table: tracked_bets (separate from placed_orders which is for bot automation)
"""

import aiosqlite
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "orders.db"


# ---------------------------------------------------------------------------
# DB Init
# ---------------------------------------------------------------------------

async def init_bets_db():
    """Create tracked_bets table if it doesn't exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tracked_bets (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                -- Snapshot at moment of tracking (Moment 1)
                event_ticker         TEXT,
                player_fav           TEXT    NOT NULL,
                player_dog           TEXT    NOT NULL,
                tournament           TEXT    NOT NULL,
                tournament_level     TEXT    NOT NULL,
                surface              TEXT    NOT NULL,
                fav_probability      REAL    NOT NULL,
                kalshi_price         INTEGER NOT NULL,
                target_price         INTEGER NOT NULL,
                tracked_at           TEXT    NOT NULL,
                -- Outcome data entered by user (Moment 2)
                contracts            INTEGER,
                lowest_price_reached INTEGER,
                match_outcome        TEXT,
                -- Derived fields (calculated after Moment 2)
                order_filled         INTEGER,
                fill_price           INTEGER,
                edge                 INTEGER,
                pnl                  REAL,
                -- Status
                status               TEXT    NOT NULL DEFAULT 'pending'
            )
        """)
        await db.commit()


# ---------------------------------------------------------------------------
# Moment 1 — Save snapshot when user clicks Track
# ---------------------------------------------------------------------------

async def track_bet(
    event_ticker: Optional[str],
    player_fav: str,
    player_dog: str,
    tournament: str,
    tournament_level: str,
    surface: str,
    fav_probability: float,
    kalshi_price: int,
    target_price: int,
) -> dict:
    """
    Save a snapshot of the match at the moment the user clicks Track.
    Returns the saved record as a dict.
    """
    tracked_at = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO tracked_bets
                (event_ticker, player_fav, player_dog, tournament,
                 tournament_level, surface, fav_probability, kalshi_price,
                 target_price, tracked_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                event_ticker, player_fav, player_dog, tournament,
                tournament_level, surface, fav_probability, kalshi_price,
                target_price, tracked_at,
            ),
        )
        await db.commit()
        bet_id = cursor.lastrowid

    return await get_bet_by_id(bet_id)


# ---------------------------------------------------------------------------
# Moment 2 — Record outcome (manual user input)
# ---------------------------------------------------------------------------

def _calculate_outcome(
    target_price: int,
    lowest_price_reached: int,
    match_outcome: str,
    contracts: int,
) -> dict:
    """
    Given the two manual inputs + contracts, derive all calculated fields.

    order_filled : lowest_price_reached <= target_price
    fill_price   : target_price if filled, else None
    edge         : lowest_price_reached - target_price
                   (negative = price went below target, good)
                   (positive = price never reached target, didn't fill)
    pnl          : profit/loss in dollars
                   filled + fav_won  → (100 - fill_price) * contracts / 100
                   filled + fav_lost → -(fill_price * contracts) / 100
                   not filled        → 0.0
    """
    order_filled = 1 if lowest_price_reached <= target_price else 0
    fill_price = target_price if order_filled else None
    edge = lowest_price_reached - target_price

    if order_filled and contracts:
        if match_outcome == "fav_won":
            pnl = round((100 - fill_price) * contracts / 100, 2)
        else:  # fav_lost
            pnl = round(-(fill_price * contracts) / 100, 2)
    else:
        pnl = 0.0

    return {
        "order_filled": order_filled,
        "fill_price": fill_price,
        "edge": edge,
        "pnl": pnl,
    }


async def update_outcome(
    bet_id: int,
    lowest_price_reached: int,
    match_outcome: str,
    contracts: int,
) -> Optional[dict]:
    """
    Update a tracked bet with outcome data entered by the user.
    Automatically calculates order_filled, fill_price, edge, and pnl.
    Returns the updated record or None if not found.
    """
    bet = await get_bet_by_id(bet_id)
    if not bet:
        return None

    derived = _calculate_outcome(
        target_price=bet["target_price"],
        lowest_price_reached=lowest_price_reached,
        match_outcome=match_outcome,
        contracts=contracts,
    )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE tracked_bets
            SET contracts            = ?,
                lowest_price_reached = ?,
                match_outcome        = ?,
                order_filled         = ?,
                fill_price           = ?,
                edge                 = ?,
                pnl                  = ?,
                status               = 'completed'
            WHERE id = ?
            """,
            (
                contracts,
                lowest_price_reached,
                match_outcome,
                derived["order_filled"],
                derived["fill_price"],
                derived["edge"],
                derived["pnl"],
                bet_id,
            ),
        )
        await db.commit()

    return await get_bet_by_id(bet_id)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

async def get_bet_by_id(bet_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM tracked_bets WHERE id = ?", (bet_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_all_bets(status: Optional[str] = None) -> list[dict]:
    """
    Return tracked bets, newest first.
    status filter: 'pending', 'completed', or None for all.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status:
            cursor = await db.execute(
                "SELECT * FROM tracked_bets WHERE status = ? ORDER BY tracked_at DESC",
                (status,),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM tracked_bets ORDER BY tracked_at DESC"
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Stats — analytics over completed bets
# ---------------------------------------------------------------------------

async def get_stats() -> dict:
    """
    Compute analytics over all completed bets.
    """
    bets = await get_all_bets()
    completed = [b for b in bets if b["status"] == "completed"]
    pending = [b for b in bets if b["status"] == "pending"]

    if not completed:
        return {
            "total_tracked": len(bets),
            "pending": len(pending),
            "completed": 0,
            "message": "No completed bets yet.",
        }

    filled = [b for b in completed if b["order_filled"]]
    won = [b for b in filled if b["match_outcome"] == "fav_won"]
    lost = [b for b in filled if b["match_outcome"] == "fav_lost"]
    not_filled = [b for b in completed if not b["order_filled"]]

    total_pnl = round(sum(b["pnl"] or 0 for b in completed), 2)
    fill_rate = round(len(filled) / len(completed) * 100, 1) if completed else 0
    win_rate = round(len(won) / len(filled) * 100, 1) if filled else 0

    # Breakdown by fav_probability bucket
    buckets = _bucket_stats(completed)

    # Breakdown by tournament_level
    by_level = _group_stats(completed, "tournament_level")

    # Breakdown by surface
    by_surface = _group_stats(completed, "surface")

    # Average edge (positive = missed, negative = filled with margin)
    edges = [b["edge"] for b in completed if b["edge"] is not None]
    avg_edge = round(sum(edges) / len(edges), 1) if edges else None

    return {
        "total_tracked": len(bets),
        "pending": len(pending),
        "completed": len(completed),
        "filled": len(filled),
        "not_filled": len(not_filled),
        "won": len(won),
        "lost": len(lost),
        "fill_rate_pct": fill_rate,
        "win_rate_pct": win_rate,
        "total_pnl": total_pnl,
        "avg_edge_cents": avg_edge,
        "by_prob_bucket": buckets,
        "by_level": by_level,
        "by_surface": by_surface,
    }


def _bucket_stats(bets: list[dict]) -> list[dict]:
    """Group completed bets by fav_probability range (5% buckets)."""
    bucket_ranges = [(70, 75), (75, 80), (80, 85), (85, 90), (90, 93)]
    results = []

    for lo, hi in bucket_ranges:
        group = [
            b for b in bets
            if b["fav_probability"] is not None and lo <= b["fav_probability"] < hi
        ]
        if not group:
            continue

        filled = [b for b in group if b["order_filled"]]
        won = [b for b in filled if b["match_outcome"] == "fav_won"]
        pnl = round(sum(b["pnl"] or 0 for b in group), 2)

        results.append({
            "bucket": f"{lo}-{hi}%",
            "count": len(group),
            "filled": len(filled),
            "won": len(won),
            "fill_rate_pct": round(len(filled) / len(group) * 100, 1),
            "win_rate_pct": round(len(won) / len(filled) * 100, 1) if filled else 0,
            "pnl": pnl,
        })

    return results


def _group_stats(bets: list[dict], field: str) -> list[dict]:
    """Group completed bets by a categorical field and compute stats."""
    groups: dict[str, list] = {}
    for b in bets:
        key = b.get(field) or "Unknown"
        groups.setdefault(key, []).append(b)

    results = []
    for key, group in sorted(groups.items()):
        filled = [b for b in group if b["order_filled"]]
        won = [b for b in filled if b["match_outcome"] == "fav_won"]
        pnl = round(sum(b["pnl"] or 0 for b in group), 2)

        results.append({
            "label": key,
            "count": len(group),
            "filled": len(filled),
            "won": len(won),
            "fill_rate_pct": round(len(filled) / len(group) * 100, 1),
            "win_rate_pct": round(len(won) / len(filled) * 100, 1) if filled else 0,
            "pnl": pnl,
        })

    return results
