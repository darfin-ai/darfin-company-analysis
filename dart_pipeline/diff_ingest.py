"""diff 오케스트레이션: PARSED filings → section_diffs (Stage 3 DIFFED).

공시마다 QoQ/YoY baseline을 결정하고(diff.resolve_baselines), 양쪽의
text_chunks/metrics를 읽어 diff.diff_pair()를 돌린 뒤 section_diffs에 기록한다.
완전히 오프라인으로 동작한다 (DART API 호출 없음 — DB와 이미 파싱된 데이터만).

각 공시는 멱등하게 처리: 재실행 시 기존 section_diffs를 지우고 다시 채운 뒤
pipeline_status를 DIFFED로 갱신한다 (IMPLEMENTATION_PLAN.md §2 Stage 3 원칙).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import db
from .client import DartClient
from .corp_codes import load_corp_codes
from .diff import diff_pair, headcount_metrics, order_filings, ownership_metrics, resolve_baselines
from .report_facts_resolve import facts_ready, resolve_headcount, resolve_ownership


@dataclass
class DiffResult:
    rcept_no: str
    bsns_year: str
    reprt_code: str
    action: str  # diffed / no_baseline / failed
    n_entries: int = 0
    detail: str = ""


def diff_filings_for_stock(client: DartClient, stock_code: str, force: bool = False) -> list[DiffResult]:
    """한 기업의 PARSED filings를 baseline과 비교해 section_diffs에 채운다.

    가장 오래된 공시는 baseline이 없어 no_baseline으로 남는다 (정상 —
    비교 기준이 생기면, 즉 다음 공시부터 diff가 시작된다).
    """
    book = load_corp_codes(client)
    corp = book.by_stock_code(stock_code)
    if corp is None:
        raise ValueError(f"종목코드 {stock_code}에 해당하는 기업 없음 (corpCode.xml 기준)")

    results: list[DiffResult] = []
    chunk_cache: dict[str, list[dict]] = {}
    metrics_cache: dict[str, list[dict]] = {}

    with db.connection() as conn:
        filings = db.filings_for_diffing(conn, corp.corp_code, force=force)
        ordered = order_filings(filings)
        parsed_ok = {f["rcept_no"] for f in ordered if f["pipeline_status"] != "RAW"}

        def load(rcept_no: str) -> tuple[list[dict], list[dict]]:
            if rcept_no not in chunk_cache:
                chunk_cache[rcept_no] = db.load_chunks(conn, rcept_no)
                metrics_cache[rcept_no] = db.load_metrics(conn, rcept_no)
            return chunk_cache[rcept_no], metrics_cache[rcept_no]

        for f in ordered:
            if not f["is_target"]:
                continue
            rcept_no, bsns_year, reprt_code = f["rcept_no"], f["bsns_year"], f["reprt_code"]
            try:
                baselines = resolve_baselines(ordered, rcept_no)
                usable = {
                    ct: b for ct, b in baselines.items()
                    if b is not None and b["rcept_no"] in parsed_ok
                }
                if not usable:
                    results.append(DiffResult(rcept_no, bsns_year, reprt_code, "no_baseline"))
                    continue

                cur_chunks, cur_metrics = load(rcept_no)
                rows = []
                for comparison_type, baseline in usable.items():
                    base_chunks, base_metrics = load(baseline["rcept_no"])
                    base_year, base_reprt = baseline["bsns_year"], baseline["reprt_code"]

                    use_api_hc = facts_ready(
                        conn, corp.corp_code, bsns_year, reprt_code, "empSttus", "exctvSttus"
                    ) and facts_ready(
                        conn, corp.corp_code, base_year, base_reprt, "empSttus", "exctvSttus"
                    )
                    use_api_own = facts_ready(
                        conn, corp.corp_code, bsns_year, reprt_code, "hyslrSttus", "mrhlSttus"
                    ) and facts_ready(
                        conn, corp.corp_code, base_year, base_reprt, "hyslrSttus", "mrhlSttus"
                    )

                    entries = diff_pair(
                        rcept_no=rcept_no,
                        reprt_code=reprt_code,
                        baseline_reprt_code=baseline["reprt_code"],
                        comparison_type=comparison_type,
                        cur_chunks=cur_chunks,
                        base_chunks=base_chunks,
                        cur_metrics=cur_metrics,
                        base_metrics=base_metrics,
                        cur_headcount=(
                            resolve_headcount(
                                conn,
                                corp.corp_code,
                                bsns_year,
                                reprt_code,
                                cur_chunks,
                                headcount_metrics,
                            )
                            if use_api_hc
                            else None
                        ),
                        base_headcount=(
                            resolve_headcount(
                                conn,
                                corp.corp_code,
                                base_year,
                                base_reprt,
                                base_chunks,
                                headcount_metrics,
                            )
                            if use_api_hc
                            else None
                        ),
                        cur_ownership=(
                            resolve_ownership(
                                conn,
                                corp.corp_code,
                                bsns_year,
                                reprt_code,
                                cur_chunks,
                                ownership_metrics,
                            )
                            if use_api_own
                            else None
                        ),
                        base_ownership=(
                            resolve_ownership(
                                conn,
                                corp.corp_code,
                                base_year,
                                base_reprt,
                                base_chunks,
                                ownership_metrics,
                            )
                            if use_api_own
                            else None
                        ),
                    )
                    for e in entries:
                        e["rcept_no"] = rcept_no
                        e["baseline_rcept_no"] = baseline["rcept_no"]
                        e["corp_code"] = corp.corp_code
                    rows.extend(entries)

                db.delete_section_diffs(conn, rcept_no)
                n = db.insert_section_diffs(conn, rows)
                db.mark_diffed(conn, rcept_no)
                conn.commit()  # 공시 1건 = 커밋 1건: 중단돼도 완료분 보존
                results.append(DiffResult(rcept_no, bsns_year, reprt_code, "diffed", n))
            except Exception as e:  # 한 건의 실패가 나머지 공시 처리를 막지 않게
                conn.rollback()
                results.append(DiffResult(rcept_no, bsns_year, reprt_code, "failed", detail=str(e)))

    return results
