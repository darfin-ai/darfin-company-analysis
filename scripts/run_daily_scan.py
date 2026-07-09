"""일일 스캔 CLI — 커버 대상 회사 전체(companies 테이블) 순회하며 1~3단계
(수집→파싱→재무제표→diff)를 실행한다. cron이 하루 1회 호출한다(예: 새벽).

1~3단계는 DART API 호출과 순수 계산뿐이라 커버 대상 전체에 대해 매일
돌려도 비용/속도 문제가 없다. **LLM 단계(4단계)는 여기서 절대 트리거하지
않는다** — 아무도 보지 않을 회사까지 매일 Gemini 비용을 쓰게 되므로,
LLM 처리는 순수 on-demand로만 돈다(darfin-main의 GET /api/v1/companies/
{corpCode}가 overview 없는 회사를 조회할 때 llm_jobs에 등록 →
scripts/run_llm_worker.py가 소비). 이 스크립트는 diff까지만 최신으로
유지해서, 다음에 사용자가 클릭했을 때 diff는 이미 끝나 있고 LLM만
기다리면 되게 한다.

예:
    python scripts/run_daily_scan.py
    python scripts/run_daily_scan.py --from 20230101   # 최초 실행 시 과거分까지
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dart_pipeline import DartClient, db, ingest_company
from dart_pipeline.corp_codes import load_corp_codes
from dart_pipeline.diff_ingest import diff_filings_for_stock
from dart_pipeline.metrics_ingest import fetch_metrics_for_stock
from dart_pipeline.overview_ingest import build_deterministic_overview_for_stock
from dart_pipeline.parse_ingest import parse_filings_for_stock
from dart_pipeline.report_facts_ingest import QuotaExceededError, fetch_report_facts_for_stock


def _covered_corp_codes(conn) -> list[str]:
    """커버 대상 = companies 테이블에 이미 등록된 회사 전체 (회사 추가는 이
    스크립트의 범위 밖 — ingest_filings.py --stock으로 최초 1회 온보딩)."""
    with conn.cursor() as cur:
        cur.execute("SELECT corp_code FROM companies")
        return [r[0] for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description="일일 스캔: 커버 대상 회사 전체 1~3단계(수집/파싱/재무제표/diff)")
    ap.add_argument("--from", dest="bgn_de", default=None, help="수집 시작일 YYYYMMDD (기본: 90일 전)")
    args = ap.parse_args()

    bgn_de = args.bgn_de or (date.today() - timedelta(days=90)).strftime("%Y%m%d")
    end_de = date.today().strftime("%Y%m%d")

    client = DartClient()
    book = load_corp_codes(client)

    with db.connection() as conn:
        corp_codes = _covered_corp_codes(conn)

    print(f"커버 대상: {len(corp_codes)}개 회사 (기간: {bgn_de}~{end_de})\n")

    for corp_code in corp_codes:
        entry = book.by_corp_code(corp_code)
        if entry is None or not entry.stock_code:
            print(f"{corp_code}: corpCode.xml에 없거나 비상장 — 스킵")
            continue
        stock_code = entry.stock_code
        try:
            ingest_company(client, stock_code, bgn_de, end_de)
            parse_filings_for_stock(client, stock_code)
            fetch_metrics_for_stock(client, stock_code)
            try:
                fetch_report_facts_for_stock(client, stock_code)
            except QuotaExceededError as e:
                print(f"{stock_code}({corp_code}): report_facts 쿼터 초과 — {e}")
                continue
            diff_filings_for_stock(client, stock_code)
            # 결정론적 부분(segments/products/regions/shareholders/dividend)은
            # LLM이 전혀 없어 여기서 바로 채운다 — 클릭을 기다릴 필요 없음.
            # insight/risks/findings(진짜 LLM 단계)만 on-demand로 남긴다.
            overview_results = build_deterministic_overview_for_stock(client, stock_code)
            built = [r for r in overview_results if r.action == "built"]

            if built:
                print(f"{stock_code}({corp_code}): 새 filing {len(built)}건 — 개요 패널 채움(LLM은 클릭 시)")
            else:
                print(f"{stock_code}({corp_code}): 변경 없음")
        except Exception as e:  # 한 회사 실패가 나머지를 막지 않게
            print(f"{stock_code}({corp_code}): 실패 — {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
