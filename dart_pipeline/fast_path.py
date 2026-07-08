"""필링 1건의 LLM 호출 4개를 동시 실행하는 빠른 경로 (Stage 5: on-demand 지연시간 단축).

`polish_diff_entries`/`extract_findings`/`extract_risks`/`generate_panel_insights`는
서로 입력이 겹치지 않고 출력도 서로를 필요로 하지 않는다 — `extract_findings`는
폴리싱 전 원문(`before_text`/`after_text`)을 그대로 읽으므로 폴리싱 완료를
기다릴 필요가 없다. `ThreadPoolExecutor`로 4개를 동시에 던지고, DB 쓰기만
한 트랜잭션에서 순서대로 한다(pymysql 커넥션은 스레드 간 공유하지 않음 —
LLM 호출부만 병렬, 나머지는 기존 stage 모듈과 완전히 동일한 로직).

company_overview는 이제 2단계 쓰기다 — segments/products/regions/
shareholders/dividend(결정론적, LLM 없음)는 `overview_ingest.py:
build_deterministic_overview_for_stock()`가 이미 써놨을 것으로 기대하고
그 값을 그대로 읽어 재사용한다(재계산 안 함). 이 함수는 risks/insights/
findings만 LLM으로 만들어 그 행을 `db.update_overview_insights()`로
patch한다. 혹시 1단계가 아직 안 돈 filing이면(레이스 컨디션 등) 결정론적
부분을 여기서 직접 계산해 새로 만드는 폴백을 갖는다.

기존 CLI(`scripts/summarize_filings.py` 등)는 그대로 둔다 — 디버깅/수동
재처리용으로 순차 실행이 오히려 로그 보기 편하다. 이 모듈은
`scripts/run_llm_worker.py`만 사용한다.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from google import genai

from . import db
from .findings_ingest import _build_evidence_catalogue
from .llm import (
    MODEL_NAME,
    DiffEntry,
    PanelFact,
    extract_findings,
    extract_risks,
    generate_panel_insights,
    polish_diff_entries,
)
from .overview import (
    compute_segment_status,
    extract_dividend,
    extract_products,
    extract_regions,
    extract_segments,
    extract_shareholders,
)
from .overview_ingest import _norm_title, _risk_evidence
from .scoring import Finding, compute_scores, quarter_label


@dataclass
class FastPathResult:
    rcept_no: str
    action: str  # processed / failed
    detail: str = ""


def process_filing_concurrent(
    gemini: genai.Client,
    corp_code: str,
    rcept_no: str,
    bsns_year: str,
    reprt_code: str,
    baseline_overview: dict | None,
) -> FastPathResult:
    """한 filing에 서술형 diff 폴리싱 + findings/score_history + company_overview를
    전부 만든다. 이미 결과가 있으면 지우고 다시 채운다(기존 stage들과 동일한
    멱등 원칙)."""
    quarter = quarter_label(bsns_year, reprt_code)

    with db.connection() as conn:
        try:
            # ── 입력 준비 (순수 조회, 빠름) ──────────────────────────────
            narrative_diffs = db.narrative_diffs_for_filing(conn, rcept_no)
            qoq_diffs = db.all_diffs_for_filing(conn, rcept_no, comparison_type="QoQ")
            mdna_chunk = db.mdna_chunk_for_filing(conn, rcept_no)
            risk_chunks = db.risk_chunks_for_filing(conn, rcept_no)

            findings_evidence = _build_evidence_catalogue(qoq_diffs, mdna_chunk)
            for item in findings_evidence:
                if item.evidence_id == -1:
                    item.source_ref = f"{rcept_no}#mdna"

            risk_evidence = _risk_evidence(risk_chunks)
            for item in risk_evidence:
                item.source_ref = f"{rcept_no}#risk-{item.evidence_id}"

            # 결정론적 패널(segments/products/regions/shareholders/dividend)은
            # 1단계(build_deterministic_overview_for_stock)가 이미 써놨을 것으로
            # 기대하고 그대로 재사용 — 재계산하지 않는다. 없으면(레이스 컨디션
            # 등 예외 상황) 여기서 직접 계산해 새로 만드는 폴백.
            existing_overview = db.overview_for_filing(conn, rcept_no)
            if existing_overview is None:
                chunks = db.load_chunks(conn, rcept_no)
                dividend_chunk = db.dividend_chunk_for_filing(conn, rcept_no)
                baseline_segments = baseline_overview.get("segments") if baseline_overview else None
                baseline_regions = baseline_overview.get("regions") if baseline_overview else None
                segments = compute_segment_status(extract_segments(chunks), baseline_segments)
                products = extract_products(chunks)
                regions = extract_regions(chunks, baseline_regions)
                shareholders = extract_shareholders(chunks)
                dividend = extract_dividend(
                    dividend_chunk, bsns_year=bsns_year, reprt_code=reprt_code
                )
            else:
                segments = existing_overview["segments"]
                products = existing_overview["products"]
                regions = existing_overview["regions"]
                shareholders = existing_overview["shareholders"]
                dividend = existing_overview["dividend"]

            panel_keys: list[str] = []
            panel_facts: list[str] = []
            if segments:
                panel_keys.append("segment")
                panel_facts.append(", ".join(f"{s['name']} {s['revenueShare']}%" for s in segments))
            if products:
                panel_keys.append("product")
                panel_facts.append(", ".join(f"{p['name']} {p['share']}%" for p in products))
            if regions:
                panel_keys.append("region")
                panel_facts.append(", ".join(f"{r['region']} {r['share']}%" for r in regions))
            if shareholders:
                panel_keys.append("shareholder")
                panel_facts.append(", ".join(f"{s['name']} {s['share']}%" for s in shareholders))
            if dividend:
                panel_keys.append("dividend")
                panel_facts.append(
                    f"주당배당금 {dividend['perShareKrw']}원, 배당수익률 "
                    f"{dividend['yieldPct']}%, 배당성향 {dividend['payoutRatioPct']}%"
                )

            diff_entries = [
                DiffEntry(
                    canonical_label=d["canonical_label"],
                    source_label=d["source_label"],
                    before=d["before_text"],
                    after=d["after_text"],
                )
                for d in narrative_diffs
            ]

            # ── LLM 호출 4개 동시 실행 (전부 빈 입력을 안전하게 처리하므로
            #    조건 분기 없이 항상 제출) ─────────────────────────────────
            with ThreadPoolExecutor(max_workers=4) as pool:
                fut_polish = pool.submit(polish_diff_entries, gemini, diff_entries)
                fut_findings = pool.submit(extract_findings, gemini, findings_evidence)
                fut_risks = pool.submit(extract_risks, gemini, risk_evidence)
                fut_insights = pool.submit(
                    generate_panel_insights,
                    gemini,
                    [PanelFact(panel_key=k, fact_summary=f) for k, f in zip(panel_keys, panel_facts)],
                )

                polish_results = fut_polish.result()
                finding_candidates = fut_findings.result()
                risk_candidates = fut_risks.result()
                insights = fut_insights.result()

            # ── 결과 조립 + DB 쓰기 (순차, 한 트랜잭션 — summarize_ingest.py/
            #    findings_ingest.py/overview_ingest.py와 동일한 로직) ──────

            # 1) 서술형 diff 폴리싱
            summary_rows = []
            for d, result in zip(narrative_diffs, polish_results):
                db.update_section_diff_text(conn, d["id"], result.before, result.after)
                summary_rows.append(
                    {
                        "rcept_no": rcept_no,
                        "corp_code": corp_code,
                        "summary_type": f"DIFF_{d['comparison_type']}_{d['canonical_label']}"[:50],
                        "content": (result.after or result.before or "")[:2000],
                        "source_refs": json.dumps(
                            [
                                {
                                    "sectionLabel": d["canonical_label"],
                                    "sourceRef": d["source_ref"],
                                    "sectionDiffId": d["id"],
                                }
                            ],
                            ensure_ascii=False,
                        ),
                        "model_used": result.model_used,
                        "tokens_in": result.tokens_in,
                        "tokens_out": result.tokens_out,
                        "cost_usd": result.cost_usd,
                        "latency_ms": result.latency_ms,
                    }
                )
            db.delete_llm_summaries(conn, rcept_no)
            db.insert_llm_summaries(conn, summary_rows)
            db.mark_summarized(conn, rcept_no)

            # 2) findings + score_history
            by_id = {item.evidence_id: item for item in findings_evidence}
            finding_rows = []
            scoring_findings: list[Finding] = []
            for c in finding_candidates:
                hops = [
                    {
                        "type": by_id[eid].hop_type,
                        "sectionLabel": by_id[eid].section_label,
                        "excerpt": by_id[eid].excerpt,
                        "sourceRef": by_id[eid].source_ref,
                    }
                    for eid in c.evidence_ids
                ]
                finding_rows.append(
                    {
                        "rcept_no": rcept_no,
                        "corp_code": corp_code,
                        "severity": c.severity,
                        "score_component": c.score_component,
                        "summary": c.summary,
                        "hops_json": json.dumps(hops, ensure_ascii=False),
                    }
                )
                scoring_findings.append(Finding(severity=c.severity, score_component=c.score_component))
            db.delete_findings(conn, rcept_no)
            db.insert_findings(conn, finding_rows)
            score_rows = compute_scores(scoring_findings, corp_code=corp_code, rcept_no=rcept_no, quarter=quarter)
            db.delete_score_history(conn, rcept_no)
            db.insert_score_history(conn, score_rows)

            # 3) company_overview
            risk_by_id = {item.evidence_id: item for item in risk_evidence}
            baseline_titles = set()
            if baseline_overview:
                baseline_titles = {_norm_title(r["title"]) for r in baseline_overview.get("risks", [])}
            risks = []
            for i, c in enumerate(risk_candidates):
                status = "existing" if _norm_title(c.title) in baseline_titles else "new"
                excerpt = " ".join(risk_by_id[eid].excerpt for eid in c.evidence_ids)
                risks.append(
                    {
                        "id": f"{rcept_no}-risk-{i}",
                        "title": c.title,
                        "description": c.description,
                        "insight": None,
                        "status": status,
                        "severity": c.severity,
                        "sourceRef": {
                            "sectionLabel": risk_by_id[c.evidence_ids[0]].section_label,
                            "excerpt": excerpt[:500],
                            "sourceRef": risk_by_id[c.evidence_ids[0]].source_ref,
                        },
                    }
                )

            insight_by_key = dict(zip(panel_keys, insights))

            if existing_overview is not None:
                # 정상 경로: 1단계가 써둔 행에 insight/risks만 patch
                db.update_overview_insights(conn, rcept_no, insight_by_key, risks)
            else:
                # 폴백: 1단계가 아직 안 돈 경우 — 결정론적 부분까지 포함해 새로 만듦
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
                    "aiInsightsReady": True,
                }
                db.delete_company_overview(conn, rcept_no)
                db.insert_company_overview(
                    conn,
                    {
                        "rcept_no": rcept_no,
                        "corp_code": corp_code,
                        "overview_json": json.dumps(overview, ensure_ascii=False),
                        "model_used": MODEL_NAME,
                    },
                )

            conn.commit()  # filing 1건 = 커밋 1건: 중단돼도 완료분 보존
            return FastPathResult(rcept_no, "processed")
        except Exception as e:
            conn.rollback()
            return FastPathResult(rcept_no, "failed", detail=str(e))
