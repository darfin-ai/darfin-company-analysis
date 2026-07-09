"""report_facts → overview/diff 소비 경로 해석 (legacy 폴백 포함)."""

from __future__ import annotations

import logging
from collections.abc import Callable

from . import db
from .report_facts import (
    dividend_panel,
    headcount_metrics,
    ownership_metrics,
    shareholders_panel,
)

log = logging.getLogger(__name__)


def facts_ready(
    conn, corp_code: str, bsns_year: str, reprt_code: str, *api_ids: str
) -> bool:
    return all(
        db.report_fact_exists(conn, corp_code, bsns_year, reprt_code, api_id)
        for api_id in api_ids
    )


def resolve_dividend(
    conn,
    corp_code: str,
    bsns_year: str,
    reprt_code: str,
    dividend_chunk,
    extract_legacy: Callable,
):
    if db.report_fact_exists(conn, corp_code, bsns_year, reprt_code, "alotMatter"):
        rows = db.report_fact_payload(conn, corp_code, bsns_year, reprt_code, "alotMatter")
        return dividend_panel(rows, bsns_year=bsns_year, reprt_code=reprt_code)
    log.info(
        "dividend: legacy extract (no report_facts) %s/%s/%s",
        corp_code,
        bsns_year,
        reprt_code,
    )
    return extract_legacy(dividend_chunk)


def resolve_shareholders(
    conn,
    corp_code: str,
    bsns_year: str,
    reprt_code: str,
    chunks: list[dict],
    extract_legacy: Callable,
) -> list[dict]:
    if db.report_fact_exists(conn, corp_code, bsns_year, reprt_code, "hyslrSttus"):
        rows = db.report_fact_payload(conn, corp_code, bsns_year, reprt_code, "hyslrSttus")
        return shareholders_panel(rows)
    log.info(
        "shareholders: legacy extract (no report_facts) %s/%s/%s",
        corp_code,
        bsns_year,
        reprt_code,
    )
    return extract_legacy(chunks)


def resolve_headcount(
    conn,
    corp_code: str,
    bsns_year: str,
    reprt_code: str,
    chunks: list[dict],
    extract_legacy: Callable,
) -> dict[str, float]:
    if db.report_fact_exists(conn, corp_code, bsns_year, reprt_code, "empSttus") and db.report_fact_exists(
        conn, corp_code, bsns_year, reprt_code, "exctvSttus"
    ):
        emp = db.report_fact_payload(conn, corp_code, bsns_year, reprt_code, "empSttus") or []
        exctv = db.report_fact_payload(conn, corp_code, bsns_year, reprt_code, "exctvSttus") or []
        return headcount_metrics(emp, exctv)
    return extract_legacy(chunks)


def resolve_ownership(
    conn,
    corp_code: str,
    bsns_year: str,
    reprt_code: str,
    chunks: list[dict],
    extract_legacy: Callable,
) -> dict[str, float]:
    if db.report_fact_exists(conn, corp_code, bsns_year, reprt_code, "hyslrSttus") and db.report_fact_exists(
        conn, corp_code, bsns_year, reprt_code, "mrhlSttus"
    ):
        major = db.report_fact_payload(conn, corp_code, bsns_year, reprt_code, "hyslrSttus") or []
        minority = db.report_fact_payload(conn, corp_code, bsns_year, reprt_code, "mrhlSttus") or []
        return ownership_metrics(major, minority)
    return extract_legacy(chunks)
