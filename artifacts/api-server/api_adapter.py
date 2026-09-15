"""Expose the imported FastAPI application under Replit's /api service path."""

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from marketsignalos_api.main import app as imported_app


app = FastAPI(title="MarketSignalOS API")


@app.get("/api/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


app.mount("/api", imported_app)