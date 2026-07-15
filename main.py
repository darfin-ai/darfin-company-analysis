"""
Darfin company-analysis service.

dartOverview real-time serving has moved to darfin-main (Spring Boot) — see
docs/dartoverview-migration in the workspace root. This process now also owns
the pipeline scheduling: dart_pipeline/scheduler.py runs the daily scan
(06:00/18:00 KST) and the LLM worker loop for the lifetime of this app, so
`uvicorn main:app` replaces the old external-cron setup. Run with exactly one
worker and without --reload — a second process would start a second
scheduler + worker loop, duplicating DART API calls and LLM jobs.
"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from dart_pipeline.scheduler import (
    daily_scan_lock,
    llm_worker_status,
    start_scheduler,
    stop_scheduler,
    _run_daily_scan_job,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(
    title="Darfin Company Analysis Service",
    version="0.2.0",
    lifespan=lifespan,
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


@app.post("/internal/trigger/daily-scan")
def trigger_daily_scan() -> dict:
    """수동 트리거 — 스케줄 기다리지 않고 daily scan을 즉시 실행(디버깅/로컬 테스트용)."""
    if daily_scan_lock.locked():
        return {"status": "already_running"}
    threading.Thread(target=_run_daily_scan_job, daemon=True).start()
    return {"status": "started"}


@app.post("/internal/trigger/llm-worker")
def trigger_llm_worker() -> dict:
    """워커 루프는 항상 돌고 있으므로(~12초 주기) '트리거'는 현재 상태를
    보고하는 것으로 충분하다 — 강제로 한 번 더 돌리는 것보다 다음 루프
    반복을 기다리는 편이 단순하고, 최악의 경우도 대기 시간은 한 주기뿐이다."""
    return {"status": "worker_loop_active", **llm_worker_status}
