"""재무 수치(metrics) 적재 CLI — Stage 2: fnlttSinglAcntAll → metrics 테이블.

예:
    python scripts/fetch_metrics.py --stock 005930
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dart_pipeline import DartClient
from dart_pipeline.metrics_ingest import fetch_metrics_for_stock

REPRT_LABELS = {"11011": "사업", "11012": "반기", "11013": "1분기", "11014": "3분기"}


def main() -> int:
    ap = argparse.ArgumentParser(description="DART 재무 수치(metrics) 적재")
    ap.add_argument("--stock", required=True, help="종목코드, 예: 005930")
    args = ap.parse_args()

    client = DartClient()
    results = fetch_metrics_for_stock(client, args.stock)

    if not results:
        print("적재 대상 없음 (모든 filings에 이미 metrics가 있거나, filings가 없음)")
        return 0

    print(f"\n{'rcept_no':16} {'연도':6} {'유형':6} {'액션':10} {'건수':6}")
    for r in results:
        label = REPRT_LABELS.get(r.reprt_code, "-")
        print(f"{r.rcept_no:16} {r.bsns_year:6} {label:6} {r.action:10} {r.n_metrics:<6}" + (f" ({r.detail})" if r.detail else ""))

    counts: dict[str, int] = {}
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1
    print(f"\n합계: {counts}")

    return 1 if counts.get("failed", 0) else 0


if __name__ == "__main__":
    sys.exit(main())
