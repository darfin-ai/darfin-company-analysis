"""LLM 처리 대기열 워커 CLI — cron이 1~2분마다 호출한다.

`llm_jobs`에서 가장 급한(priority 낮고 오래된) 1건을 집어, 그 회사의 밀린
filing을 시간순으로 순회하며 `dart_pipeline.fast_path.process_filing_concurrent`
로 처리한다(filing 안의 LLM 호출 4개는 동시 실행, filing 간은 baseline
체이닝 때문에 순차). 호출당 최대 1개 job만 처리 — cron 주기가 자연스러운
Gemini rate limit 역할을 한다.

예:
    python scripts/run_llm_worker.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google import genai

from dart_pipeline import db
from dart_pipeline.diff import order_filings, resolve_baselines
from dart_pipeline.fast_path import process_filing_concurrent


def main() -> int:
    gemini = genai.Client()

    with db.connection() as conn:
        job = db.claim_next_job(conn)
        # 이 블록이 끝나면 'running' 마킹이 바로 커밋된다(아래 LLM 처리는
        # 별도 커넥션) — 처리 도중 워커가 죽으면 job은 'running'에 머무르며,
        # db.claim_next_job()이 15분 넘은 'running' job을 방치된 것으로
        # 보고 다시 집어가는 방식으로 복구한다.

    if job is None:
        print("대기 중인 job 없음")
        return 0

    corp_code = job["corp_code"]
    print(f"job #{job['id']} 처리 시작: corp_code={corp_code}")

    overview_cache: dict[str, dict] = {}
    all_ok = True
    detail = ""

    with db.connection() as conn:
        raw = db.filings_for_overview(conn, corp_code)
        is_target = {r["rcept_no"] for r in raw if r["is_target"]}
        ordered = order_filings(raw)

    for f in ordered:
        rcept_no, bsns_year, reprt_code = f["rcept_no"], f["bsns_year"], f["reprt_code"]

        baseline = resolve_baselines(ordered, rcept_no)["QoQ"]
        baseline_overview = None
        if baseline is not None:
            if baseline["rcept_no"] in overview_cache:
                baseline_overview = overview_cache[baseline["rcept_no"]]
            else:
                with db.connection() as conn:
                    baseline_overview = db.overview_for_filing(conn, baseline["rcept_no"])

        if rcept_no not in is_target:
            if baseline_overview is None:
                with db.connection() as conn:
                    cached = db.overview_for_filing(conn, rcept_no)
                if cached is not None:
                    overview_cache[rcept_no] = cached
            continue

        result = process_filing_concurrent(gemini, corp_code, rcept_no, bsns_year, reprt_code, baseline_overview)
        print(f"  {rcept_no}: {result.action}" + (f" ({result.detail})" if result.detail else ""))

        if result.action != "processed":
            all_ok = False
            detail = result.detail
            break  # 이후 filing은 baseline이 끊겨 의미가 없으므로 중단

        with db.connection() as conn:
            overview_cache[rcept_no] = db.overview_for_filing(conn, rcept_no)

    with db.connection() as conn:
        if all_ok:
            db.mark_job_done(conn, job["id"])
            print(f"job #{job['id']} 완료")
        else:
            db.mark_job_failed(conn, job["id"], detail)
            print(f"job #{job['id']} 실패: {detail}")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
