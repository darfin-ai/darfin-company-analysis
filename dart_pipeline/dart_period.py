"""dartOverview용 최신 정기공시 기간 결정 — DART list.json만 사용 (filings 비의존)."""

from __future__ import annotations

from datetime import date, timedelta

from .report_classify import classify_report

# 연간보고서가 아직 없고 분기만 있는 시점을 커버 (약 18개월)
_LIST_LOOKBACK_DAYS = 548


def periodic_candidates_from_list(items: list[dict]) -> list[dict]:
    """list.json 항목 → 정기공시 후보 전체, rcept_dt 내림차순.

    같은 (bsns_year, reprt_code)가 여러 건이면(정정공시) 가장 최근 rcept_dt만
    남긴다 — fallback 탐색 시 과거 기간의 최종본을 쓰기 위함.
    """
    by_period: dict[tuple[str, str], dict] = {}
    for item in items:
        report_nm = (item.get("report_nm") or "").strip()
        classified = classify_report(report_nm)
        if classified is None:
            continue
        reprt_code, bsns_year = classified
        rcept_dt = item.get("rcept_dt") or ""
        key = (bsns_year, reprt_code)
        existing = by_period.get(key)
        if existing is not None and existing["rcept_dt"] >= rcept_dt:
            continue
        by_period[key] = {
            "rcept_no": item["rcept_no"],
            "bsns_year": bsns_year,
            "reprt_code": reprt_code,
            "rcept_dt": rcept_dt,
        }
    return sorted(by_period.values(), key=lambda c: c["rcept_dt"], reverse=True)


def latest_periodic_from_list(items: list[dict]) -> dict | None:
    """list.json 항목 → 가장 최근 정기공시 (reprt_code, bsns_year, rcept_no)."""
    candidates = periodic_candidates_from_list(items)
    return candidates[0] if candidates else None


def list_filings_date_range(*, today: date | None = None) -> tuple[str, str]:
    """list.json 조회용 (bgn_de, end_de) YYYYMMDD."""
    end = today or date.today()
    bgn = end - timedelta(days=_LIST_LOOKBACK_DAYS)
    return bgn.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def resolve_latest_period_sync(client, corp_code: str) -> dict | None:
    """DART list.json → 최신 정기공시 (동기 ingest 경로용)."""
    from .client import DartApiError

    bgn_de, end_de = list_filings_date_range()
    try:
        items = client.list_filings(corp_code, bgn_de, end_de)
    except DartApiError:
        return None
    return latest_periodic_from_list(items)
