"""정기공시 수집 CLI.

예:
    python scripts/ingest_filings.py --stock 005930 --from 20230101
    python scripts/ingest_filings.py --stock 005930 --from 20230101 --dry-run
    python scripts/ingest_filings.py --stock 005930 --from 20230101 --smoke-parse
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dart_pipeline import DartClient, ingest_company

REPRT_LABELS = {"11011": "사업", "11012": "반기", "11013": "1분기", "11014": "3분기"}


def main() -> int:
    ap = argparse.ArgumentParser(description="DART 정기공시 수집")
    ap.add_argument("--stock", required=True, help="종목코드, 예: 005930")
    ap.add_argument("--from", dest="bgn_de", required=True, help="시작일 YYYYMMDD")
    ap.add_argument("--to", dest="end_de", default=date.today().strftime("%Y%m%d"), help="종료일 YYYYMMDD (기본: 오늘)")
    ap.add_argument("--dry-run", action="store_true", help="다운로드/DB 기록 없이 대상만 나열")
    ap.add_argument("--force", action="store_true", help="이미 수집된 공시도 재다운로드")
    ap.add_argument("--smoke-parse", action="store_true", help="수집된 파일을 dart_parser로 파싱 검증")
    args = ap.parse_args()

    client = DartClient()
    results = ingest_company(client, args.stock, args.bgn_de, args.end_de, dry_run=args.dry_run, force=args.force)

    print(f"\n{'rcept_no':16} {'보고서':28} {'유형':6} {'액션'}")
    for r in results:
        label = REPRT_LABELS.get(r.reprt_code, "-")
        print(f"{r.rcept_no:16} {r.report_nm[:26]:28} {label:6} {r.action}" + (f"  ({r.detail})" if r.detail and r.action == "failed" else ""))

    counts: dict[str, int] = {}
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1
    print(f"\n합계: {counts}")

    failed = counts.get("failed", 0)

    if args.smoke_parse:
        from dart_parser import parse_filing
        from dart_parser.canonical import CANONICAL_LABELS
        from dart_pipeline.config import RAW_DIR
        from dart_pipeline.corp_codes import load_corp_codes

        corp = load_corp_codes(client).by_stock_code(args.stock)
        print("\n파싱 스모크 테스트:")
        for r in results:
            if r.action not in ("ingested", "skipped_exists"):
                continue
            path = RAW_DIR / corp.corp_code / f"{r.rcept_no}.xml"
            try:
                filing = parse_filing(path)
                mapped = sum(1 for lb in CANONICAL_LABELS if filing.sections_by_canonical(lb))
                flag = "✓" if mapped == 12 else "✗"
                print(f"  {flag} {r.report_nm[:26]:28} 섹션 {len(filing.sections):4}  표준 {mapped}/12  fact {len(filing.facts):5}  경고 {len(filing.warnings)}")
                if mapped != 12:
                    failed += 1
            except Exception as e:
                failed += 1
                print(f"  ✗ {r.report_nm[:26]:28} 파싱 실패: {e}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
