"""재무제표 캐시 워밍 CLI — fnlttSinglAcntAll → financial_facts (darfin-main과 공유).

scripts/fetch_metrics.py(metrics 테이블, 2026-07-13 폐기)의 후신. 서빙은
darfin-main의 read-through가 전담하고, 이 CLI는 diff 입력 준비와 과거
히스토리 채움 용도로만 캐시를 미리 덥힌다.

예:
    python scripts/warm_financial_facts.py --stock 005930
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dart_pipeline import DartClient
from dart_pipeline.financial_facts_ingest import warm_financial_facts_for_stock

REPRT_LABELS = {"11011": "사업", "11012": "반기", "11013": "1분기", "11014": "3분기"}


def main() -> int:
    ap = argparse.ArgumentParser(description="DART 재무제표 원본 캐시(financial_facts) 워밍")
    ap.add_argument("--stock", required=True, help="종목코드, 예: 005930")
    ap.add_argument("--force", action="store_true", help="이미 캐시된 기간도 다시 받아 교체")
    args = ap.parse_args()

    client = DartClient()
    results = warm_financial_facts_for_stock(client, args.stock, force=args.force)

    if not results:
        print("워밍 대상 없음 (모든 기간이 이미 캐시돼 있거나, filings가 없음)")
        return 0

    print(f"\n{'rcept_no':16} {'연도':6} {'유형':6} {'액션':10} {'행수':6}")
    for r in results:
        label = REPRT_LABELS.get(r.reprt_code, "-")
        print(f"{r.rcept_no:16} {r.bsns_year:6} {label:6} {r.action:10} {r.n_rows:<6}" + (f" ({r.detail})" if r.detail else ""))

    counts: dict[str, int] = {}
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1
    print(f"\n합계: {counts}")

    return 1 if counts.get("failed", 0) else 0


if __name__ == "__main__":
    sys.exit(main())
