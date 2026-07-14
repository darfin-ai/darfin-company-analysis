"""섹션 diff 적재 CLI — Stage 3: PARSED filings → section_diffs.

이미 파싱된 text_chunks와 financial_facts 캐시를 비교하므로 DART API 호출 없이 오프라인으로
동작한다 (corp_code 조회용 corpCode.xml 캐시만 필요).

예:
    python scripts/diff_filings.py --stock 005930
    python scripts/diff_filings.py --stock 005930 --force   # 이미 DIFFED인 것도 재처리
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dart_pipeline import DartClient
from dart_pipeline.diff_ingest import diff_filings_for_stock

REPRT_LABELS = {"11011": "사업", "11012": "반기", "11013": "1분기", "11014": "3분기"}


def main() -> int:
    ap = argparse.ArgumentParser(description="DART 공시 섹션 diff 적재")
    ap.add_argument("--stock", required=True, help="종목코드, 예: 005930")
    ap.add_argument("--force", action="store_true", help="이미 DIFFED인 filings도 재처리")
    args = ap.parse_args()

    client = DartClient()
    results = diff_filings_for_stock(client, args.stock, force=args.force)

    if not results:
        print("diff 대상 없음 (PARSED 상태의 filings가 없음)")
        return 0

    print(f"\n{'rcept_no':16} {'연도':6} {'유형':6} {'액션':12} {'diff 수':8}")
    for r in results:
        label = REPRT_LABELS.get(r.reprt_code, "-")
        print(f"{r.rcept_no:16} {r.bsns_year:6} {label:6} {r.action:12} {r.n_entries:<8}" + (f" ({r.detail})" if r.detail else ""))

    counts: dict[str, int] = {}
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1
    print(f"\n합계: {counts}")

    return 1 if counts.get("failed", 0) else 0


if __name__ == "__main__":
    sys.exit(main())
