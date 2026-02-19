"""
Background scheduler — runs the automation cycle on a configurable interval.

Uses APScheduler's AsyncIOScheduler so it integrates cleanly with FastAPI/asyncio.
Controlled at runtime via /api/automation/start and /api/automation/stop.

Config:
  AUTOMATION_INTERVAL_MINUTES=10   How often to run (default: 10 minutes)
  AUTOMATION_AUTOSTART=false       Start automatically on server boot (default: false)
"""

import os
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from app.automation import run_automation_cycle, init_db

logger = logging.getLogger(__name__)

INTERVAL_MINUTES = int(os.getenv("AUTOMATION_INTERVAL_MINUTES", "10"))
AUTOSTART = os.getenv("AUTOMATION_AUTOSTART", "false").lower() == "true"

_scheduler = AsyncIOScheduler()
JOB_ID = "tennis_automation"


async def setup_scheduler():
    """
    Initialize the DB and start the scheduler.
    Called once from main.py on server startup.
    Does NOT add the automation job yet — that requires calling start_automation().
    Set AUTOMATION_AUTOSTART=true to start automatically.
    """
    await init_db()

    if not _scheduler.running:
        _scheduler.start()
        logger.info("APScheduler started")

    if AUTOSTART:
        await start_automation()
        logger.info(f"Automation auto-started (interval: {INTERVAL_MINUTES} min)")


async def start_automation() -> dict:
    """Add (or replace) the automation job and run one cycle immediately."""
    if not _scheduler.running:
        _scheduler.start()

    _scheduler.add_job(
        run_automation_cycle,
        trigger=IntervalTrigger(minutes=INTERVAL_MINUTES),
        id=JOB_ID,
        name="Tennis automation cycle",
        replace_existing=True,
        misfire_grace_time=60,
    )

    # Run once immediately so we don't wait for the first interval
    await run_automation_cycle()

    logger.info(f"Automation started — running every {INTERVAL_MINUTES} minutes")
    return {
        "status": "started",
        "interval_minutes": INTERVAL_MINUTES,
        "next_run": _next_run(),
    }


def stop_automation() -> dict:
    """Remove the automation job."""
    if _scheduler.get_job(JOB_ID):
        _scheduler.remove_job(JOB_ID)
        logger.info("Automation stopped")
    return {"status": "stopped"}


def is_running() -> bool:
    return _scheduler.running and _scheduler.get_job(JOB_ID) is not None


def _next_run() -> str | None:
    job = _scheduler.get_job(JOB_ID)
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


def scheduler_state() -> dict:
    return {
        "running": is_running(),
        "interval_minutes": INTERVAL_MINUTES,
        "next_run": _next_run(),
    }
