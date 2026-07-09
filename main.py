"""
Darfin company-analysis read API (dartOverview read-through cache).

Run: make dev-company-api  (or uvicorn main:app --port 8003)
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from dart_pipeline.dart_overview_rt import get_dart_overview

app = FastAPI(
    title="Darfin Company Analysis Read API",
    description="On-demand DART dartOverview with report_facts read-through cache",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["GET"],
    allow_headers=["*"],
)


class DartOverviewResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    meta: dict = Field(default_factory=dict)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/dart/overview/{corp_code}", response_model=None)
async def dart_overview(corp_code: str, force: bool = False) -> dict:
    """Return DartOverview JSON. Cache refreshes when the latest filing's
    rcept_no changes (new report or 정정공시); force=true re-fetches everything."""
    if len(corp_code) != 8 or not corp_code.isdigit():
        raise HTTPException(status_code=400, detail="corp_code must be 8 digits")
    try:
        return await get_dart_overview(corp_code, force=force)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
