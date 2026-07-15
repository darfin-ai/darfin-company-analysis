"""findings + score_history 적재 CLI — Stage 4(나머지): DIFFED/SUMMARIZED filings의
QoQ section_diffs + MD&A 텍스트로 추론 체인(findings)을 뽑고, 그 findings를
집계해 4개 컴포넌트 점수(score_history)를 계산한다.

DART API는 corp_code 조회에만 쓰이고(캐시 히트 시 호출 없음), 실제 비용이
드는 것은 Gemini 호출뿐이다(필링당 findings 추출 1회, score_history는 순수 계산).

예:
    python scripts/extract_findings.py --stock 005930 --limit 1   # 소규모 검증
    python scripts/extract_findings.py --stock 005930             # 전체
    python scripts/extract_findings.py --stock 005930 --force     # 기존 findings도 재생성
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google import genai
from dart_pipeline.llm_runtime import create_client

from dart_pipeline import DartClient
from dart_pipeline.findings_ingest import extract_findings_for_stock

REPRT_LABELS = {"11011": "사업", "11012": "반기", "11013": "1분기", "11014": "3분기"}


def main() -> int:
    ap = argparse.ArgumentParser(description="DART 공시 findings/score_history 적재")
    ap.add_argument("--stock", required=True, help="종목코드, 예: 005930")
    ap.add_argument("--force", action="store_true", help="이미 findings가 있는 filings도 재처리")
    ap.add_argument("--limit", type=int, default=None, help="처리할 filings 수 상한 (비용 통제용)")
    args = ap.parse_args()

    client = DartClient()
    gemini = create_client()
    results = extract_findings_for_stock(client, gemini, args.stock, force=args.force, limit=args.limit)

    if not results:
        print("findings 대상 없음 (DIFFED/SUMMARIZED 상태의 filings가 없음)")
        return 0

    print(f"\n{'rcept_no':16} {'연도':6} {'유형':6} {'액션':12} {'findings':8}")
    for r in results:
        label = REPRT_LABELS.get(r.reprt_code, "-")
        print(
            f"{r.rcept_no:16} {r.bsns_year:6} {label:6} {r.action:12} {r.n_findings:<8}"
            + (f" ({r.detail})" if r.detail else "")
        )

    counts: dict[str, int] = {}
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1

    print(f"\n합계: {counts}")

    return 1 if counts.get("failed", 0) else 0


if __name__ == "__main__":
    sys.exit(main())
