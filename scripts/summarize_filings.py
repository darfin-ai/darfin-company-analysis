"""LLM 요약 적재 CLI — Stage 4(일부): DIFFED filings의 서술형 diff를 다듬어
section_diffs.before_text/after_text를 갱신하고 llm_summaries에 비용을 기록.

DART API는 corp_code 조회에만 쓰이고(캐시 히트 시 호출 없음), 실제 비용이
드는 것은 Gemini 호출뿐이다.

예:
    python scripts/summarize_filings.py --stock 005930 --limit 1   # 소규모 검증
    python scripts/summarize_filings.py --stock 005930             # 전체
    python scripts/summarize_filings.py --stock 005930 --force     # 이미 SUMMARIZED도 재처리
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google import genai

from dart_pipeline import DartClient
from dart_pipeline.summarize_ingest import summarize_filings_for_stock

REPRT_LABELS = {"11011": "사업", "11012": "반기", "11013": "1분기", "11014": "3분기"}


def main() -> int:
    ap = argparse.ArgumentParser(description="DART 공시 diff LLM 요약 적재")
    ap.add_argument("--stock", required=True, help="종목코드, 예: 005930")
    ap.add_argument("--force", action="store_true", help="이미 SUMMARIZED인 filings도 재처리")
    ap.add_argument("--limit", type=int, default=None, help="처리할 filings 수 상한 (비용 통제용)")
    args = ap.parse_args()

    client = DartClient()
    gemini = genai.Client()
    results = summarize_filings_for_stock(client, gemini, args.stock, force=args.force, limit=args.limit)

    if not results:
        print("요약 대상 없음 (DIFFED 상태의 filings가 없음)")
        return 0

    print(f"\n{'rcept_no':16} {'연도':6} {'유형':6} {'액션':12} {'entries':8} {'비용(USD)':10}")
    for r in results:
        label = REPRT_LABELS.get(r.reprt_code, "-")
        print(
            f"{r.rcept_no:16} {r.bsns_year:6} {label:6} {r.action:12} {r.n_entries:<8} {r.total_cost_usd:<10.4f}"
            + (f" ({r.detail})" if r.detail else "")
        )

    counts: dict[str, int] = {}
    total_cost = 0.0
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1
        total_cost += r.total_cost_usd

    print(f"\n합계: {counts}  총비용: ${total_cost:.4f}")

    return 1 if counts.get("failed", 0) else 0


if __name__ == "__main__":
    sys.exit(main())
