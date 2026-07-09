"""Read-through cache: report_facts 접수번호(rcept_no) 비교 → parallel DART fetch → DartOverview."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import httpx

from . import db
from .async_dart_client import AsyncDartClient
from .client import DartApiError
from .dart_overview_compose import compose_dart_overview
from .dart_period import list_filings_date_range, periodic_candidates_from_list
from .report_facts import REPORT_FACT_API_IDS, is_placeholder_only

log = logging.getLogger(__name__)

API_IDS = list(REPORT_FACT_API_IDS)

# alotMatter(배당)는 자체적으로 thstrm/frmtrm/lwfr 3개년 history를 갖고 있어
# 과거 기간 fallback 대상에서 제외한다.
_FALLBACK_ELIGIBLE_API_IDS = [a for a in API_IDS if a != "alotMatter"]
_MAX_FALLBACK_CANDIDATES = 3


class QuotaExceededError(Exception):
    """DART 일일 쿼터(020)."""


async def _resolve_period(
    dart: AsyncDartClient,
    http: httpx.AsyncClient,
    corp_code: str,
) -> tuple[str, str, str | None, list[dict]]:
    """최신 정기공시 기간 + lookback 창 내 전체 후보(최신순) — DART list.json (filings 테이블과 무관)."""
    bgn_de, end_de = list_filings_date_range()
    try:
        items = await dart.list_filings(http, corp_code, bgn_de, end_de)
    except DartApiError as exc:
        log.warning("list.json failed for %s: %s", corp_code, exc)
        items = []

    candidates = periodic_candidates_from_list(items)
    if candidates:
        latest = candidates[0]
        return latest["bsns_year"], latest["reprt_code"], latest["rcept_no"], candidates

    year = datetime.now().year - 1
    log.warning(
        "no periodic filing in list.json for %s (%s~%s), falling back to %s 11011",
        corp_code,
        bgn_de,
        end_de,
        year,
    )
    return str(year), "11011", None, []


async def _fetch_one(
    dart: AsyncDartClient,
    http: httpx.AsyncClient,
    *,
    api_id: str,
    corp_code: str,
    bsns_year: str,
    reprt_code: str,
) -> tuple[str, list[dict] | None | Exception]:
    try:
        payload = await dart.report_api(http, api_id, corp_code, bsns_year, reprt_code)
        return api_id, payload
    except Exception as exc:
        return api_id, exc


async def _refresh_stale_with_client(
    dart: AsyncDartClient,
    http: httpx.AsyncClient,
    corp_code: str,
    bsns_year: str,
    reprt_code: str,
    stale_ids: list[str],
    rcept_no: str | None,
) -> None:
    if not stale_ids:
        return

    quota_hit = False

    results = await asyncio.gather(
        *[
            _fetch_one(
                dart,
                http,
                api_id=api_id,
                corp_code=corp_code,
                bsns_year=bsns_year,
                reprt_code=reprt_code,
            )
            for api_id in stale_ids
        ]
    )

    with db.connection() as conn:
        _ensure_company_for_cache(conn, corp_code)
        for api_id, outcome in results:
            if isinstance(outcome, DartApiError):
                if outcome.status == "020":
                    quota_hit = True
                    log.warning("DART quota exceeded for %s/%s", corp_code, api_id)
                    continue
                log.warning("DART error %s for %s: %s", outcome.status, api_id, outcome)
                continue
            if isinstance(outcome, Exception):
                log.warning("fetch failed for %s: %s", api_id, outcome)
                continue
            db.upsert_report_fact(
                conn,
                corp_code=corp_code,
                bsns_year=bsns_year,
                reprt_code=reprt_code,
                api_id=api_id,
                payload=None if is_placeholder_only(outcome) else outcome,
                rcept_no=rcept_no,
            )
        conn.commit()

    if quota_hit:
        raise QuotaExceededError("DART daily quota exceeded (020)")


async def _resolve_sections_with_fallback(
    dart: AsyncDartClient,
    http: httpx.AsyncClient,
    corp_code: str,
    missing_api_ids: list[str],
    older_candidates: list[dict],
) -> dict[str, dict[str, list[dict] | dict]]:
    """현재 기간에 데이터가 없는 api_id를 과거 정기공시(최신순)에서 채운다.

    반환: api_id → {"payload": rows, "asOf": {bsnsYear, reprtCode, rceptNo}}.
    분기보고서엔 기재의무 없는 항목(직원현황 등)을 반기/사업보고서로 보정하기 위함 —
    alotMatter(배당)는 호출부에서 애초에 missing_api_ids에서 제외한다.
    """
    resolved: dict[str, dict] = {}
    remaining = set(missing_api_ids)
    quota_hit = False

    for candidate in older_candidates[:_MAX_FALLBACK_CANDIDATES]:
        if not remaining or quota_hit:
            break
        bsns_year, reprt_code = candidate["bsns_year"], candidate["reprt_code"]
        cand_rcept_no = candidate["rcept_no"]

        with db.connection() as conn:
            cached = db.report_facts_for_period(conn, corp_code, bsns_year, reprt_code)

        still_missing = [a for a in remaining if a not in cached]
        results: list[tuple[str, list[dict] | None | Exception]] = [
            (a, cached[a]["payload"]) for a in remaining if a in cached
        ]

        if still_missing:
            fetched = await asyncio.gather(
                *[
                    _fetch_one(
                        dart,
                        http,
                        api_id=api_id,
                        corp_code=corp_code,
                        bsns_year=bsns_year,
                        reprt_code=reprt_code,
                    )
                    for api_id in still_missing
                ]
            )
            with db.connection() as conn:
                _ensure_company_for_cache(conn, corp_code)
                for api_id, outcome in fetched:
                    if isinstance(outcome, DartApiError):
                        if outcome.status == "020":
                            quota_hit = True
                            log.warning(
                                "DART quota exceeded during fallback for %s/%s", corp_code, api_id
                            )
                        else:
                            log.warning(
                                "DART error %s during fallback for %s: %s",
                                outcome.status,
                                api_id,
                                outcome,
                            )
                        continue
                    if isinstance(outcome, Exception):
                        log.warning("fallback fetch failed for %s: %s", api_id, outcome)
                        continue
                    db.upsert_report_fact(
                        conn,
                        corp_code=corp_code,
                        bsns_year=bsns_year,
                        reprt_code=reprt_code,
                        api_id=api_id,
                        payload=None if is_placeholder_only(outcome) else outcome,
                        rcept_no=cand_rcept_no,
                    )
                conn.commit()
            results.extend(fetched)

        for api_id, outcome in results:
            if isinstance(outcome, Exception) or outcome is None:
                continue
            if is_placeholder_only(outcome):
                continue
            resolved[api_id] = {
                "payload": outcome,
                "asOf": {
                    "bsnsYear": bsns_year,
                    "reprtCode": reprt_code,
                    "rceptNo": cand_rcept_no,
                },
            }
            remaining.discard(api_id)

    return resolved


def _payloads_from_cache(
    cached: dict[str, dict | None],
) -> dict[str, list[dict] | None]:
    """캐시된 payload → api_id별 rows. is_placeholder_only 적용 전(rcept_no가 바뀌지
    않아 재조회되지 않은) 과거 캐시 행도 여기서 다시 걸러 placeholder를 None으로
    취급한다 — 그래야 배포 전 캐시된 all-dash 응답도 별도 백필 없이 자연 치유된다.
    """
    out: dict[str, list[dict] | None] = {}
    for api_id in API_IDS:
        entry = cached.get(api_id)
        if entry is None or is_placeholder_only(entry["payload"]):
            out[api_id] = None
        else:
            out[api_id] = entry["payload"]
    return out


def _ensure_company_for_cache(conn, corp_code: str) -> None:
    """report_facts FK(companies) — browse-only stock도 캐시 가능하게 최소 행 확보."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM companies WHERE corp_code = %s", (corp_code,))
        if cur.fetchone() is not None:
            return
        cur.execute(
            "SELECT company_name, stock_code FROM stock WHERE dart_corp_code = %s",
            (corp_code,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"corp_code {corp_code} not found in stock table")
    db.ensure_company(conn, corp_code, row[0], row[1])


async def get_dart_overview(
    corp_code: str,
    *,
    force: bool = False,
) -> dict:
    """Read-through: DB cache first, parallel DART fetch when 접수번호가 바뀐 경우만."""
    dart = AsyncDartClient()
    async with httpx.AsyncClient() as http:
        with db.connection() as conn:
            _ensure_company_for_cache(conn, corp_code)
            conn.commit()

        bsns_year, reprt_code, rcept_no, candidates = await _resolve_period(
            dart, http, corp_code
        )

        with db.connection() as conn:
            # rcept_no=None이면 기간이 검증되지 않은 fallback 추정치 — prune하면
            # 실제 최신 기간의 캐시를 지울 수 있으므로 건너뛴다.
            if rcept_no is not None:
                keep_periods = [(c["bsns_year"], c["reprt_code"]) for c in candidates]
                deleted = db.delete_report_facts_outside_periods(
                    conn, corp_code, keep_periods
                )
                if deleted:
                    log.info(
                        "pruned %d stale report_facts row(s) for %s (keeping %s/%s)",
                        deleted,
                        corp_code,
                        bsns_year,
                        reprt_code,
                    )
                conn.commit()
            stale_ids = db.report_facts_needing_refresh(
                conn,
                corp_code,
                bsns_year,
                reprt_code,
                API_IDS,
                current_rcept_no=rcept_no,
                force=force,
            )

        try:
            await _refresh_stale_with_client(
                dart, http, corp_code, bsns_year, reprt_code, stale_ids, rcept_no
            )
        except QuotaExceededError:
            pass  # serve partial cache

        with db.connection() as conn:
            cached = db.report_facts_for_period(conn, corp_code, bsns_year, reprt_code)

        payloads = _payloads_from_cache(cached)
        fallback_info: dict[str, dict] = {}
        missing_api_ids = [
            a for a in _FALLBACK_ELIGIBLE_API_IDS if payloads.get(a) is None
        ]
        if missing_api_ids and len(candidates) > 1:
            resolved = await _resolve_sections_with_fallback(
                dart, http, corp_code, missing_api_ids, candidates[1:]
            )
            for api_id, info in resolved.items():
                payloads[api_id] = info["payload"]
                fallback_info[api_id] = info["asOf"]

    return compose_dart_overview(
        bsns_year=bsns_year,
        reprt_code=reprt_code,
        rcept_no=rcept_no,
        payloads=payloads,
        fallback_info=fallback_info,
    )
