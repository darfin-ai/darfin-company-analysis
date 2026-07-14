"""신규 관심기업 초기 backfill — job_type='onboard_ingest' (ddl.sql §7 llm_jobs).

관심기업(별표) 추가 시 Spring(`StarredCompanyService.addStarred`)이 이 회사에
filings가 하나도 없으면 이 job을 큐에 넣는다. `run_llm_worker.py`가 소비해
run_daily_scan.py 루프 본문과 같은 단계(수집→파싱→재무제표 워밍→
report_facts→diff→결정론적 개요, **LLM 없음**)를 이 한 회사에 대해서만
즉시 실행한다 — 그래야 "관심기업 추가 → 며칠 뒤 다음 일일 스캔"이 아니라
"관심기업 추가 → 1분 내 큐 처리 → 개요/재무추이/AI분석 전 데이터 완성"이 된다.
(daily_scan은 이미 온보딩된 회사의 *신규* filing만 얕게 훑는 90일 lookback이라
이 목적엔 안 맞는다 — 신규 온보딩은 상태머신·재무추이 차트가 쓸 수 있게
5년치를 한 번에 당겨온다.)

각 단계는 rcept_no/기간 단위로 멱등이라 job이 재시도(claim_next_job의
15분 방치 복구)돼도 이미 처리된 부분은 건너뛴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from . import DartClient, ingest_company
from .corp_codes import load_corp_codes
from .diff_ingest import diff_filings_for_stock
from .financial_facts_ingest import warm_financial_facts_for_stock
from .overview_ingest import build_deterministic_overview_for_stock
from .parse_ingest import parse_filings_for_stock
from .report_facts_ingest import QuotaExceededError, fetch_report_facts_for_stock

# RiskAnalysisService.LOOKBACK_DAYS(Java, darfin-main)와 동일 — 상태머신이 쓸
# 5년/20분기 이력을 첫 온보딩에서 한 번에 채운다.
ONBOARD_LOOKBACK_DAYS = 1825


@dataclass
class OnboardResult:
    stock_code: str
    corp_code: str
    filings_ingested: int


def resolve_stock_code(client: DartClient, corp_code: str) -> str:
    entry = load_corp_codes(client).by_corp_code(corp_code)
    if entry is None or not entry.stock_code:
        raise ValueError(f"corp_code {corp_code}: corpCode.xml에 없거나 비상장")
    return entry.stock_code


def ingest_company_full(client: DartClient, corp_code: str) -> OnboardResult:
    """단일 회사 stage 1~3 (LLM 없음). run_daily_scan.py 루프 본문과 동일 순서."""
    stock_code = resolve_stock_code(client, corp_code)
    bgn_de = (date.today() - timedelta(days=ONBOARD_LOOKBACK_DAYS)).strftime("%Y%m%d")
    end_de = date.today().strftime("%Y%m%d")

    results = ingest_company(client, stock_code, bgn_de, end_de)
    parse_filings_for_stock(client, stock_code)
    warm_financial_facts_for_stock(client, stock_code)
    try:
        fetch_report_facts_for_stock(client, stock_code)
    except QuotaExceededError:
        # 개요 일부 패널만 비어있는 저하 상태 — 전체 실패로 보지 않는다.
        # (report_facts는 다음 daily scan이 쿼터 회복 후 다시 채운다.)
        pass
    diff_filings_for_stock(client, stock_code)
    build_deterministic_overview_for_stock(client, stock_code)

    ingested = sum(1 for r in results if r.action == "ingested")
    return OnboardResult(stock_code=stock_code, corp_code=corp_code, filings_ingested=ingested)
