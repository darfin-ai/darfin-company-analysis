"""report_facts 적재 오케스트레이션: DART 주요정보 10종 → report_facts.

회사당 최신 정기공시 기간 1건만 유지한다(list.json으로 기간 결정).
이전 기간 행은 적재 전에 삭제한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import db
from .client import DartApiError, DartClient
from .corp_codes import load_corp_codes
from .dart_period import resolve_latest_period_sync
from .report_facts import REPORT_FACT_API_IDS, is_placeholder_only


class QuotaExceededError(Exception):
    """DART 일일 쿼터(020) — 호출부가 run 전체를 멈추게 한다."""


@dataclass
class ReportFactsResult:
    bsns_year: str
    reprt_code: str
    api_id: str
    action: str  # stored / no_data / failed / skipped
    n_rows: int = 0
    detail: str = ""


def fetch_report_facts_for_stock(
    client: DartClient, stock_code: str, force: bool = False
) -> list[ReportFactsResult]:
    book = load_corp_codes(client)
    corp = book.by_stock_code(stock_code)
    if corp is None:
        raise ValueError(f"종목코드 {stock_code}에 해당하는 기업 없음 (corpCode.xml 기준)")

    period = resolve_latest_period_sync(client, corp.corp_code)
    if period is None:
        return []

    bsns_year = period["bsns_year"]
    reprt_code = period["reprt_code"]
    results: list[ReportFactsResult] = []

    with db.connection() as conn:
        db.delete_report_facts_other_periods(conn, corp.corp_code, bsns_year, reprt_code)
        conn.commit()

        for api_id in REPORT_FACT_API_IDS:
            if not force and db.report_fact_exists(
                conn, corp.corp_code, bsns_year, reprt_code, api_id
            ):
                continue
            try:
                raw = client.report_api(api_id, corp.corp_code, bsns_year, reprt_code)
                placeholder = is_placeholder_only(raw)
                db.upsert_report_fact(
                    conn,
                    corp_code=corp.corp_code,
                    bsns_year=bsns_year,
                    reprt_code=reprt_code,
                    api_id=api_id,
                    payload=None if placeholder else raw,
                    rcept_no=period.get("rcept_no"),
                )
                conn.commit()
                if placeholder:
                    results.append(ReportFactsResult(bsns_year, reprt_code, api_id, "no_data"))
                else:
                    results.append(
                        ReportFactsResult(bsns_year, reprt_code, api_id, "stored", len(raw))
                    )
            except DartApiError as e:
                conn.rollback()
                if e.status == "020":
                    raise QuotaExceededError(str(e)) from e
                results.append(
                    ReportFactsResult(bsns_year, reprt_code, api_id, "failed", detail=str(e))
                )
            except Exception as e:
                conn.rollback()
                results.append(
                    ReportFactsResult(bsns_year, reprt_code, api_id, "failed", detail=str(e))
                )

    return results
