"""company_overview 오케스트레이션 (Stage 4 나머지).

filing 이력을 시간순으로(diff.py와 동일한 QoQ 의미론) 순회하며 각 filing의
segments/products/regions/shareholders/dividend를 tables_json에서 결정론적으로
뽑고, risks는 위험요인 프로즈에서 LLM으로 추출한다. added/existing 상태와
regions delta는 직전 filing(QoQ baseline)의 company_overview와 비교해서
계산하므로, 이번 실행에서 새로 만든 overview는 캐시에 담아 다음 filing이
바로 참조할 수 있게 한다.

각 filing은 멱등하게 처리: 재실행 시 기존 company_overview를 지우고 다시
채운다. filing 1건 = 커밋 1건(기존 스테이지들과 동일한 장애 격리 원칙).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from google import genai

from . import db
from .client import DartClient
from .corp_codes import load_corp_codes
from .diff import order_filings, resolve_baselines
from .llm import MODEL_NAME, EvidenceItem, PanelFact, extract_risks, generate_panel_insights
from .overview import (
    build_mdna_entry,
    compute_segment_status,
    extract_dividend,
    extract_products,
    extract_regions,
    extract_segments,
    extract_shareholders,
)
from .report_facts_resolve import resolve_dividend, resolve_shareholders
from .scoring import quarter_label

_RISK_EXCERPT_MAX = 500


@dataclass
class OverviewResult:
    rcept_no: str
    bsns_year: str
    reprt_code: str
    action: str  # built / failed / skipped(비대상, 통계에는 안 실림)
    detail: str = ""


def _risk_evidence(risk_chunks: list[dict]) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    eid = 0
    for chunk in risk_chunks:
        section_label = chunk["breadcrumb"] or chunk["section_title"]
        for para in (chunk["content"] or "").split("\n"):
            para = para.strip()
            if len(para) < 20:  # 너무 짧은 조각(빈 줄, 목록 기호 등)은 제외
                continue
            items.append(
                EvidenceItem(
                    evidence_id=eid,
                    hop_type="note",
                    section_label=section_label,
                    excerpt=para[:_RISK_EXCERPT_MAX],
                    source_ref="",  # 호출부에서 rcept_no 앵커로 채움
                )
            )
            eid += 1
    return items


def _build_risks(
    gemini: genai.Client, rcept_no: str, risk_chunks: list[dict], baseline_overview: dict | None
) -> list[dict]:
    evidence = _risk_evidence(risk_chunks)
    for item in evidence:
        item.source_ref = f"{rcept_no}#risk-{item.evidence_id}"
    if not evidence:
        return []

    by_id = {item.evidence_id: item for item in evidence}
    candidates = extract_risks(gemini, evidence)

    baseline_titles = set()
    if baseline_overview:
        baseline_titles = {_norm_title(r["title"]) for r in baseline_overview.get("risks", [])}

    out = []
    for i, c in enumerate(candidates):
        title_norm = _norm_title(c.title)
        status = "existing" if title_norm in baseline_titles else "new"
        excerpt = " ".join(by_id[eid].excerpt for eid in c.evidence_ids)
        source_ref = by_id[c.evidence_ids[0]].source_ref
        out.append(
            {
                "id": f"{rcept_no}-risk-{i}",
                "title": c.title,
                "description": c.description,
                "insight": None,
                "status": status,
                "severity": c.severity,
                "sourceRef": {
                    "sectionLabel": by_id[c.evidence_ids[0]].section_label,
                    "excerpt": excerpt[:_RISK_EXCERPT_MAX],
                    "sourceRef": source_ref,
                },
            }
        )
    return out


def _norm_title(title: str) -> str:
    return "".join(title.split())


def build_deterministic_overview_for_stock(
    client: DartClient,
    stock_code: str,
    force: bool = False,
    limit: int | None = None,
) -> list[OverviewResult]:
    """company_overview의 1단계(결정론적 부분만) — segments/products/regions/
    shareholders/dividend를 tables_json에서 뽑아 저장한다. Gemini 호출이 전혀
    없어 커버 대상 전체에 매일 돌려도 무해하다(scripts/run_daily_scan.py가
    diff 직후 호출). `*Insight` 필드는 전부 null, `risks`는 빈 배열,
    `aiInsightsReady: false`로 저장 — 2단계(dart_pipeline.fast_path)가 나중에
    이 행을 UPDATE해서 insight/risks를 채우고 true로 바꾼다.

    대상 filing은 build_overview_for_stock과 동일(company_overview 행 자체가
    없는 것) — 이미 1단계든 2단계든 처리된 filing은 건드리지 않는다.
    """
    book = load_corp_codes(client)
    corp = book.by_stock_code(stock_code)
    if corp is None:
        raise ValueError(f"종목코드 {stock_code}에 해당하는 기업 없음 (corpCode.xml 기준)")

    results: list[OverviewResult] = []
    overview_cache: dict[str, dict] = {}

    with db.connection() as conn:
        raw = db.filings_for_overview(conn, corp.corp_code, force=force)
        is_target = {r["rcept_no"] for r in raw if r["is_target"]}
        ordered = order_filings(raw)

        target_count = 0
        for f in ordered:
            rcept_no, bsns_year, reprt_code = f["rcept_no"], f["bsns_year"], f["reprt_code"]

            baseline = resolve_baselines(ordered, rcept_no)["QoQ"]
            baseline_overview = None
            if baseline is not None:
                baseline_overview = overview_cache.get(baseline["rcept_no"]) or db.overview_for_filing(
                    conn, baseline["rcept_no"]
                )

            if rcept_no not in is_target:
                cached = db.overview_for_filing(conn, rcept_no)
                if cached is not None:
                    overview_cache[rcept_no] = cached
                continue

            if limit is not None and target_count >= limit:
                continue
            target_count += 1

            try:
                chunks = db.load_chunks(conn, rcept_no)
                dividend_chunk = db.dividend_chunk_for_filing(conn, rcept_no)
                mdna_chunk = db.mdna_chunk_for_filing(conn, rcept_no)

                baseline_segments = baseline_overview.get("segments") if baseline_overview else None
                baseline_regions = baseline_overview.get("regions") if baseline_overview else None
                baseline_mdna_history = baseline_overview.get("mdnaHistory") if baseline_overview else None

                segments = compute_segment_status(extract_segments(chunks), baseline_segments)
                products = extract_products(chunks)
                regions = extract_regions(chunks, baseline_regions)
                shareholders = resolve_shareholders(
                    conn,
                    corp.corp_code,
                    bsns_year,
                    reprt_code,
                    chunks,
                    extract_shareholders,
                )
                dividend = resolve_dividend(
                    conn,
                    corp.corp_code,
                    bsns_year,
                    reprt_code,
                    dividend_chunk,
                    lambda chunk: extract_dividend(
                        chunk, bsns_year=bsns_year, reprt_code=reprt_code
                    ),
                )
                mdna_entry = build_mdna_entry(
                    rcept_no, bsns_year, reprt_code, quarter_label(bsns_year, reprt_code),
                    mdna_chunk["content"] if mdna_chunk else None,
                )
                mdna_history = list(baseline_mdna_history or [])
                if mdna_entry is not None:
                    mdna_history.append(mdna_entry)

                overview = {
                    "segments": segments,
                    "segmentInsight": None,
                    "products": products,
                    "productInsight": None,
                    "customers": [],
                    "regions": regions,
                    "regionInsight": None,
                    "risks": [],
                    "shareholders": shareholders,
                    "shareholderInsight": None,
                    "dividend": ({**dividend, "insight": None} if dividend else None),
                    "mdnaHistory": mdna_history,
                    "aiInsightsReady": False,
                }

                db.delete_company_overview(conn, rcept_no)
                db.insert_company_overview(
                    conn,
                    {
                        "rcept_no": rcept_no,
                        "corp_code": corp.corp_code,
                        "overview_json": json.dumps(overview, ensure_ascii=False),
                        "model_used": "none",  # 이 단계는 LLM을 안 씀
                    },
                )
                conn.commit()  # 공시 1건 = 커밋 1건: 중단돼도 완료분 보존

                overview_cache[rcept_no] = overview
                results.append(OverviewResult(rcept_no, bsns_year, reprt_code, "built"))
            except Exception as e:  # 한 건의 실패가 나머지 공시 처리를 막지 않게
                conn.rollback()
                results.append(OverviewResult(rcept_no, bsns_year, reprt_code, "failed", detail=str(e)))

    return results


def build_overview_for_stock(
    client: DartClient,
    gemini: genai.Client,
    stock_code: str,
    force: bool = False,
    limit: int | None = None,
) -> list[OverviewResult]:
    book = load_corp_codes(client)
    corp = book.by_stock_code(stock_code)
    if corp is None:
        raise ValueError(f"종목코드 {stock_code}에 해당하는 기업 없음 (corpCode.xml 기준)")

    results: list[OverviewResult] = []
    overview_cache: dict[str, dict] = {}

    with db.connection() as conn:
        raw = db.filings_for_overview(conn, corp.corp_code, force=force)
        is_target = {r["rcept_no"] for r in raw if r["is_target"]}
        ordered = order_filings(raw)

        target_count = 0
        for f in ordered:
            rcept_no, bsns_year, reprt_code = f["rcept_no"], f["bsns_year"], f["reprt_code"]

            baseline = resolve_baselines(ordered, rcept_no)["QoQ"]
            baseline_overview = None
            if baseline is not None:
                baseline_overview = overview_cache.get(baseline["rcept_no"]) or db.overview_for_filing(
                    conn, baseline["rcept_no"]
                )

            if rcept_no not in is_target:
                # 처리 대상은 아니지만, 이미 저장된 overview를 캐시에 올려 다음
                # filing의 baseline 조회가 DB 재조회 없이 되게 한다.
                cached = db.overview_for_filing(conn, rcept_no)
                if cached is not None:
                    overview_cache[rcept_no] = cached
                continue

            if limit is not None and target_count >= limit:
                continue
            target_count += 1

            try:
                chunks = db.load_chunks(conn, rcept_no)
                dividend_chunk = db.dividend_chunk_for_filing(conn, rcept_no)
                risk_chunks = db.risk_chunks_for_filing(conn, rcept_no)
                mdna_chunk = db.mdna_chunk_for_filing(conn, rcept_no)

                baseline_segments = baseline_overview.get("segments") if baseline_overview else None
                baseline_regions = baseline_overview.get("regions") if baseline_overview else None
                baseline_mdna_history = baseline_overview.get("mdnaHistory") if baseline_overview else None

                segments = compute_segment_status(extract_segments(chunks), baseline_segments)
                products = extract_products(chunks)
                regions = extract_regions(chunks, baseline_regions)
                shareholders = resolve_shareholders(
                    conn,
                    corp.corp_code,
                    bsns_year,
                    reprt_code,
                    chunks,
                    extract_shareholders,
                )
                dividend = resolve_dividend(
                    conn,
                    corp.corp_code,
                    bsns_year,
                    reprt_code,
                    dividend_chunk,
                    lambda chunk: extract_dividend(
                        chunk, bsns_year=bsns_year, reprt_code=reprt_code
                    ),
                )
                risks = _build_risks(gemini, rcept_no, risk_chunks, baseline_overview)
                mdna_entry = build_mdna_entry(
                    rcept_no, bsns_year, reprt_code, quarter_label(bsns_year, reprt_code),
                    mdna_chunk["content"] if mdna_chunk else None,
                )
                mdna_history = list(baseline_mdna_history or [])
                if mdna_entry is not None:
                    mdna_history.append(mdna_entry)

                panel_facts = []
                panel_keys = []
                if segments:
                    panel_keys.append("segment")
                    panel_facts.append(
                        ", ".join(f"{s['name']} {s['revenueShare']}%" for s in segments)
                    )
                if products:
                    panel_keys.append("product")
                    panel_facts.append(", ".join(f"{p['name']} {p['share']}%" for p in products))
                if regions:
                    panel_keys.append("region")
                    panel_facts.append(", ".join(f"{r['region']} {r['share']}%" for r in regions))
                if shareholders:
                    panel_keys.append("shareholder")
                    panel_facts.append(
                        ", ".join(f"{s['name']} {s['share']}%" for s in shareholders)
                    )
                if dividend:
                    panel_keys.append("dividend")
                    panel_facts.append(
                        f"주당배당금 {dividend['perShareKrw']}원, 배당수익률 "
                        f"{dividend['yieldPct']}%, 배당성향 {dividend['payoutRatioPct']}%"
                    )

                insights = generate_panel_insights(
                    gemini, [PanelFact(panel_key=k, fact_summary=f) for k, f in zip(panel_keys, panel_facts)]
                )
                insight_by_key = dict(zip(panel_keys, insights))

                overview = {
                    "segments": segments,
                    "segmentInsight": insight_by_key.get("segment"),
                    "products": products,
                    "productInsight": insight_by_key.get("product"),
                    "customers": [],
                    "regions": regions,
                    "regionInsight": insight_by_key.get("region"),
                    "risks": risks,
                    "shareholders": shareholders,
                    "shareholderInsight": insight_by_key.get("shareholder"),
                    "dividend": (
                        {**dividend, "insight": insight_by_key.get("dividend")} if dividend else None
                    ),
                    "mdnaHistory": mdna_history,
                }

                db.delete_company_overview(conn, rcept_no)
                db.insert_company_overview(
                    conn,
                    {
                        "rcept_no": rcept_no,
                        "corp_code": corp.corp_code,
                        "overview_json": json.dumps(overview, ensure_ascii=False),
                        "model_used": MODEL_NAME,
                    },
                )
                conn.commit()  # 공시 1건 = 커밋 1건: 중단돼도 완료분 보존

                overview_cache[rcept_no] = overview
                results.append(OverviewResult(rcept_no, bsns_year, reprt_code, "built"))
            except Exception as e:  # 한 건의 실패가 나머지 공시 처리를 막지 않게
                conn.rollback()
                results.append(OverviewResult(rcept_no, bsns_year, reprt_code, "failed", detail=str(e)))

    return results
