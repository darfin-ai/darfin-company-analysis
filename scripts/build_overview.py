"""company_overview 적재 CLI — Stage 4(마지막 조각): segments/products/regions/
shareholders/dividend를 tables_json에서 결정론적으로 뽑고, risks는 위험요인
프로즈에서 LLM으로 추출한다. customers는 DART가 공시하지 않아 빈 배열.

filing을 시간순으로 처리해야 added/existing 상태와 regions delta가 정확하다 —
--limit은 "최근 N건"이 아니라 "처리 대상 중 앞에서부터 N건"이라 처음 실행 시
가장 오래된 filing부터 채워진다.

예:
    python scripts/build_overview.py --stock 005930 --limit 1   # 소규모 검증
    python scripts/build_overview.py --stock 005930             # 전체
    python scripts/build_overview.py --stock 005930 --force     # 기존 overview도 재생성
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google import genai
from dart_pipeline.llm_runtime import create_client

from dart_pipeline import DartClient
from dart_pipeline.overview_ingest import build_overview_for_stock

REPRT_LABELS = {"11011": "사업", "11012": "반기", "11013": "1분기", "11014": "3분기"}


def main() -> int:
    ap = argparse.ArgumentParser(description="DART 공시 company_overview 적재")
    ap.add_argument("--stock", required=True, help="종목코드, 예: 005930")
    ap.add_argument("--force", action="store_true", help="이미 overview가 있는 filings도 재처리")
    ap.add_argument("--limit", type=int, default=None, help="처리할 filings 수 상한 (비용 통제용)")
    args = ap.parse_args()

    client = DartClient()
    gemini = create_client()
    results = build_overview_for_stock(client, gemini, args.stock, force=args.force, limit=args.limit)

    if not results:
        print("overview 대상 없음 (처리할 filings가 없음)")
        return 0

    print(f"\n{'rcept_no':16} {'연도':6} {'유형':6} {'액션':10}")
    for r in results:
        label = REPRT_LABELS.get(r.reprt_code, "-")
        print(f"{r.rcept_no:16} {r.bsns_year:6} {label:6} {r.action:10}" + (f" ({r.detail})" if r.detail else ""))

    counts: dict[str, int] = {}
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1

    print(f"\n합계: {counts}")

    return 1 if counts.get("failed", 0) else 0


if __name__ == "__main__":
    sys.exit(main())
