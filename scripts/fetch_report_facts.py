"""DART 정기보고서 주요정보(report_facts) 적재 CLI.

예:
    python scripts/fetch_report_facts.py --stock 005930
    python scripts/fetch_report_facts.py --stock 005930 --force
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dart_pipeline import DartClient
from dart_pipeline.report_facts_ingest import QuotaExceededError, fetch_report_facts_for_stock

REPRT_LABELS = {"11011": "사업", "11012": "반기", "11013": "1분기", "11014": "3분기"}


def main() -> int:
    ap = argparse.ArgumentParser(description="DART 정기보고서 주요정보(report_facts) 적재")
    ap.add_argument("--stock", required=True, help="종목코드, 예: 005930")
    ap.add_argument("--force", action="store_true", help="이미 적재된 키도 다시 받아 교체")
    args = ap.parse_args()

    client = DartClient()
    try:
        results = fetch_report_facts_for_stock(client, args.stock, force=args.force)
    except QuotaExceededError as e:
        print(f"일일 쿼터 초과 — 중단: {e}")
        return 2

    if not results:
        print("적재 대상 없음 (모든 키가 이미 report_facts에 있거나 filings가 없음)")
        return 0

    print(f"\n{'연도':6} {'유형':6} {'api_id':28} {'액션':10} {'건수':6}")
    for r in results:
        label = REPRT_LABELS.get(r.reprt_code, "-")
        print(
            f"{r.bsns_year:6} {label:6} {r.api_id:28} {r.action:10} {r.n_rows:<6}"
            + (f" ({r.detail})" if r.detail else "")
        )

    counts: dict[str, int] = {}
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1
    print(f"\n합계: {counts}")

    return 1 if counts.get("failed", 0) else 0


if __name__ == "__main__":
    sys.exit(main())
