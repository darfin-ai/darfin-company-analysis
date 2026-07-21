"""Embedded scheduler — 이 프로세스(FastAPI 앱) 안에서 daily scan과 llm worker를
직접 돌린다. 예전에는 외부 cron이 scripts/run_daily_scan.py, scripts/run_llm_worker.py를
각각 호출했지만, 이제 main.py의 lifespan이 start_scheduler()/stop_scheduler()로
이 모듈을 켜고 끈다.

- daily scan: APScheduler BackgroundScheduler, 06:00/18:00 KST cron.
  BackgroundScheduler를 쓰는 이유 — 파이프라인 호출은 전부 동기 블로킹
  I/O(pymysql, requests, google-genai sync client)라서 AsyncIOScheduler로
  돌리면 uvicorn의 asyncio 루프(즉 /health 등 다른 요청 처리)가 그동안
  멈춘다. BackgroundScheduler는 별도 스레드 풀에서 돌아 이벤트 루프와
  분리된다.
- llm worker: APScheduler job이 아니라 daemon thread N개(LLM_WORKER_CONCURRENCY)가
  각자 무한 루프를 돌며 run_llm_worker.main()을 반복 호출한다. main() 자체가 이미
  TIME_BUDGET_SECONDS=50으로 자기 실행 시간을 제한하고 큐가 비면 바로
  리턴하므로, 루프+짧은 sleep이 예전의 "cron이 1분마다" 방식을 대체한다.
  스레드를 늘려도 안전한 이유 — db.claim_next_job()이 SELECT ... FOR UPDATE로
  행을 잠그므로 동시에 호출해도 서로 다른 job을 집어가고(같은 job 중복 처리
  없음), db.connection()도 호출마다 독립된 pymysql 커넥션을 새로 열어 스레드 간
  공유 상태가 없다. job은 회사(corp_code) 단위라 회사가 다르면 완전히 독립적으로
  처리된다 — 병목은 "한 회사 안에서 filing을 순차로 봐야 하는" 부분이라 회사 간
  병렬화만으로도 대기열 전체 처리량이 늘어난다.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from scripts.run_daily_scan import main as run_daily_scan_main
from scripts.run_llm_worker import main as run_llm_worker_main

LLM_WORKER_POLL_SECONDS = 12
# Gemini 호출은 job(회사) 단위로 독립적이라 스레드를 늘리면 대기열 처리량이
# 그대로 늘어난다. 너무 높이면 Gemini rate limit(429)만 늘려 재시도 오버헤드가
# 커지므로 기본값은 보수적으로 잡고 필요하면 배포 환경변수로 조정한다.
LLM_WORKER_CONCURRENCY = int(os.environ.get("LLM_WORKER_CONCURRENCY", "3"))

daily_scan_lock = threading.Lock()

llm_worker_status: dict = {
    "running": False,
    "active_workers": 0,
    "last_started": None,
    "last_finished": None,
    "last_result": None,
}
_llm_worker_status_lock = threading.Lock()

_scheduler: BackgroundScheduler | None = None
_llm_threads: list[threading.Thread] = []
_llm_stop_event = threading.Event()


def _run_daily_scan_job() -> None:
    if not daily_scan_lock.acquire(blocking=False):
        print("daily scan 이미 실행 중 — 이번 트리거는 건너뜀")
        return
    try:
        run_daily_scan_main()
    except Exception as e:  # noqa: BLE001 — 스케줄러 스레드가 죽지 않게
        print(f"daily scan 실행 중 실패: {e}")
    finally:
        daily_scan_lock.release()


def _llm_worker_loop(stop_event: threading.Event, worker_id: int) -> None:
    while not stop_event.is_set():
        with _llm_worker_status_lock:
            llm_worker_status["running"] = True
            llm_worker_status["active_workers"] += 1
            llm_worker_status["last_started"] = time.time()
        try:
            result = run_llm_worker_main()
            with _llm_worker_status_lock:
                llm_worker_status["last_result"] = result
        except Exception as e:  # noqa: BLE001 — 워커 스레드가 죽지 않게
            print(f"llm worker#{worker_id} 루프 실패: {e}")
        finally:
            with _llm_worker_status_lock:
                llm_worker_status["active_workers"] -= 1
                llm_worker_status["running"] = llm_worker_status["active_workers"] > 0
                llm_worker_status["last_finished"] = time.time()
        stop_event.wait(LLM_WORKER_POLL_SECONDS)


def start_scheduler() -> None:
    global _scheduler, _llm_threads

    _scheduler = BackgroundScheduler(timezone="Asia/Seoul")
    _scheduler.add_job(
        _run_daily_scan_job,
        CronTrigger(hour="6,18", minute=0, timezone="Asia/Seoul"),
        id="daily_scan",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()

    _llm_stop_event.clear()
    _llm_threads = [
        threading.Thread(target=_llm_worker_loop, args=(_llm_stop_event, i), daemon=True)
        for i in range(LLM_WORKER_CONCURRENCY)
    ]
    for t in _llm_threads:
        t.start()

    print(
        f"scheduler started: daily_scan 06:00,18:00 KST; "
        f"llm_worker loop active x{LLM_WORKER_CONCURRENCY}"
    )


def stop_scheduler() -> None:
    global _scheduler

    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None

    _llm_stop_event.set()
    for t in _llm_threads:
        t.join(timeout=5)
