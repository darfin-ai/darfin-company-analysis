"""재무 수치 적재 오케스트레이션: fnlttSinglAcntAll → metrics (Stage 2 연장).

filings에 이미 RAW로 기록된 공시마다 연결(CFS)·별도(OFS) 재무제표를 조회해
metrics에 채운다. 각 공시는 멱등하게 처리: 재실행 시 기존 metrics를 지우고
다시 채운다 (IMPLEMENTATION_PLAN.md §2 Stage 2 원칙).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import db
from .client import DartClient
from .corp_codes import load_corp_codes
from .metrics import transform


@dataclass
class MetricsResult:
    rcept_no: str
    bsns_year: str
    reprt_code: str
    action: str  # stored / no_data / failed
    n_metrics: int = 0
    detail: str = ""


def fetch_metrics_for_stock(client: DartClient, stock_code: str, force: bool = False) -> list[MetricsResult]:
    """한 기업의, metrics가 아직 없는 모든 filings에 연결+별도 재무제표를 적재.

    force=True면 이미 적재된 filings도 다시 받아 교체한다.
    """
    book = load_corp_codes(client)
    corp = book.by_stock_code(stock_code)
    if corp is None:
        raise ValueError(f"종목코드 {stock_code}에 해당하는 기업 없음 (corpCode.xml 기준)")

    results: list[MetricsResult] = []
    with db.connection() as conn:
        targets = db.filings_missing_metrics(conn, corp.corp_code, force=force)

        for f in targets:
            rcept_no, bsns_year, reprt_code = f["rcept_no"], f["bsns_year"], f["reprt_code"]
            try:
                rows: list[dict] = []
                for fs_div, is_consolidated in (("CFS", True), ("OFS", False)):
                    raw = client.fnltt_singl_acnt_all(corp.corp_code, bsns_year, reprt_code, fs_div)
                    rows.extend(
                        transform(
                            raw,
                            rcept_no=rcept_no,
                            corp_code=corp.corp_code,
                            bsns_year=bsns_year,
                            reprt_code=reprt_code,
                            is_consolidated=is_consolidated,
                        )
                    )

                db.delete_metrics(conn, rcept_no)
                n = db.insert_metrics(conn, rows)
                conn.commit()
                results.append(
                    MetricsResult(rcept_no, bsns_year, reprt_code, "stored" if n else "no_data", n)
                )
            except Exception as e:  # 한 건의 실패가 나머지 공시 처리를 막지 않게
                conn.rollback()
                results.append(MetricsResult(rcept_no, bsns_year, reprt_code, "failed", detail=str(e)))

    return results
