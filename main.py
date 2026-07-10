"""
Darfin company-analysis service.

dartOverview real-time serving has moved to darfin-main (Spring Boot) — see
docs/dartoverview-migration in the workspace root. This app now only hosts
the health check; batch pipeline scripts run independently via scripts/.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="Darfin Company Analysis Service",
    version="0.2.0",
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


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
