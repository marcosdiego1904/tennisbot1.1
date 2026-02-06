"""
TennisBot — Tennis trading analysis dashboard.
Entry point for the FastAPI application.
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

load_dotenv()

from app.routes import router

app = FastAPI(
    title="TennisBot",
    description="Tennis favorites trading analysis system",
    version="1.0.0",
)

# API routes
app.include_router(router)

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
