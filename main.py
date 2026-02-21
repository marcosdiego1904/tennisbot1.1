"""
TennisBot — Tennis trading analysis dashboard.
Entry point for the FastAPI application.
"""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

load_dotenv()

from app.routes import router
from app.scheduler import setup_scheduler
from app.bet_tracker import init_bets_db, DB_PATH

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tennisbot")

app = FastAPI(
    title="TennisBot",
    description="Tennis favorites trading analysis system",
    version="2.0.0",
)

# API routes
app.include_router(router)


@app.on_event("startup")
async def on_startup():
    """Initialize DBs and scheduler on server start."""
    await setup_scheduler()
    await init_bets_db()

    # Volume / persistence check — visible in Railway logs
    db_exists = DB_PATH.exists()
    db_size   = DB_PATH.stat().st_size if db_exists else 0
    if db_exists and db_size > 0:
        log.info("✅ DB OK — %s (%.1f KB) — data persisted from previous deploy", DB_PATH, db_size / 1024)
    elif db_exists:
        log.info("🆕 DB created — %s — first run or empty volume", DB_PATH)
    else:
        log.warning("⚠️  DB NOT FOUND at %s — volume may not be mounted", DB_PATH)

# Serve static frontend
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def root():
    return FileResponse(str(static_dir / "index.html"))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
