"""재무제표 캐시 워밍 오케스트레이션: fnlttSinglAcntAll → financial_facts.

metrics 테이블(파이프라인 소유, 2026-07-13 폐기)의 후신. 재무 추이 서빙은
darfin-main의 FinancialFactsService(온디맨드 read-through)가 전담하고, 이
스테이지는 같은 financial_facts 테이블을 온보딩/일일 스캔 시점에 미리 덥혀
(a) 사용자가 클릭하기 전에 diff의 수치형 입력이 준비돼 있게 하고
(b) darfin-main의 730일 lookback보다 깊은 과거 기간을 채워 차트 히스토리를
보존한다. 원본 rows를 가공 없이 JSON으로 저장한다 — 계정 행 변환은
metrics.transform()이 읽기 시점에 순수 함수로 수행한다.

각 (연도, 보고서) 기간은 멱등하게 처리: upsert라 재실행 시 덮어쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import db
from .client import DartClient
from .corp_codes import load_corp_codes

FS_DIVS = (("CFS", "연결"), ("OFS", "별도"))


@dataclass
class FinancialFactsResult:
    rcept_no: str
    bsns_year: str
    reprt_code: str
    action: str  # stored / no_data / failed
    n_rows: int = 0
    detail: str = ""


def warm_financial_facts_for_stock(
    client: DartClient, stock_code: str, force: bool = False
) -> list[FinancialFactsResult]:
    """한 기업의, financial_facts가 없거나 낡은(정정공시) 기간을 채운다.

    force=True면 이미 캐시된 기간도 다시 받아 교체한다.
    """
    book = load_corp_codes(client)
    corp = book.by_stock_code(stock_code)
    if corp is None:
        raise ValueError(f"종목코드 {stock_code}에 해당하는 기업 없음 (corpCode.xml 기준)")

    results: list[FinancialFactsResult] = []
    with db.connection() as conn:
        targets = db.filings_missing_financial_facts(conn, corp.corp_code, force=force)

        for f in targets:
            rcept_no, bsns_year, reprt_code = f["rcept_no"], f["bsns_year"], f["reprt_code"]
            try:
                n_total = 0
                for fs_div, _label in FS_DIVS:
                    raw = client.fnltt_singl_acnt_all(corp.corp_code, bsns_year, reprt_code, fs_div)
                    # 빈 응답은 negative cache(payload NULL) — darfin-main과 동일 규약
                    db.upsert_financial_fact(
                        conn,
                        corp_code=corp.corp_code,
                        bsns_year=bsns_year,
                        reprt_code=reprt_code,
                        fs_div=fs_div,
                        rcept_no=rcept_no,
                        payload=raw if raw else None,
                    )
                    n_total += len(raw or [])
                conn.commit()
                results.append(
                    FinancialFactsResult(
                        rcept_no, bsns_year, reprt_code, "stored" if n_total else "no_data", n_total
                    )
                )
            except Exception as e:  # 한 건의 실패가 나머지 기간 처리를 막지 않게
                conn.rollback()
                results.append(
                    FinancialFactsResult(rcept_no, bsns_year, reprt_code, "failed", detail=str(e))
                )

    return results
