"""findings + score_history 오케스트레이션 (Stage 4 나머지).

DIFFED/SUMMARIZED filings마다: 이번 filing의 QoQ section_diffs + MD&A 텍스트
청크로 증거 카탈로그를 만들고(코드), LLM로 findings 후보를 뽑은 뒤(evidence_id
참조만 허용 — sectionLabel/excerpt/sourceRef는 카탈로그에서 그대로 가져와
기계적으로 채운다), findings를 저장하고 그 findings를 집계해 score_history를
계산한다(순수 계산, LLM 없음).

각 filing은 멱등하게 처리: 재실행 시 기존 findings/score_history를 지우고
다시 채운다. filing 1건 = 커밋 1건(기존 스테이지들과 동일한 장애 격리 원칙).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from google import genai

from . import db
from .client import DartClient
from .corp_codes import load_corp_codes
from .diff import _STATEMENT_LABELS
from .llm import EvidenceItem, extract_findings
from .scoring import Finding, compute_scores, quarter_label

_MDNA_EXCERPT_MAX = 1500
_TEXT_EXCERPT_MAX = 800


@dataclass
class FindingsResult:
    rcept_no: str
    bsns_year: str
    reprt_code: str
    action: str  # extracted / no_evidence / failed
    n_findings: int = 0
    detail: str = ""


def _format_metrics(metrics_json: str | None) -> str | None:
    """metrics_json([{label,current,baseline,unit}, ...])을 한 줄 요약으로 렌더링."""
    if not metrics_json:
        return None
    try:
        metrics = json.loads(metrics_json)
    except (TypeError, ValueError):
        return None
    if not metrics:
        return None

    lines = []
    for m in metrics:
        label, current, baseline, unit = m.get("label"), m.get("current"), m.get("baseline"), m.get("unit")
        if current is None or baseline is None:
            continue
        if baseline == 0:
            pct = "신규" if current != 0 else "0%"
        else:
            pct = f"{(current - baseline) / abs(baseline) * 100:+.1f}%"
        unit_label = m.get("unitLabel") or ("원" if unit == "KRW" else "")
        lines.append(f"{label}: {baseline:,}{unit_label} → {current:,}{unit_label} ({pct})")
    return "; ".join(lines) if lines else None


def _build_evidence_catalogue(diffs: list[dict], mdna_chunk: dict | None) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    for d in diffs:
        excerpt = _format_metrics(d["metrics_json"])
        if excerpt is None:
            excerpt = (d["after_text"] or d["before_text"] or "").strip()
            if not excerpt:
                continue
            excerpt = excerpt[:_TEXT_EXCERPT_MAX]

        hop_type = "financial_anomaly" if d["canonical_label"] in _STATEMENT_LABELS else "note"
        items.append(
            EvidenceItem(
                evidence_id=d["id"],
                hop_type=hop_type,
                section_label=d["source_label"] or d["canonical_label"],
                excerpt=excerpt,
                source_ref=d["source_ref"] or "",
            )
        )

    if mdna_chunk is not None:
        content = (mdna_chunk["content"] or "").strip()
        if content:
            items.append(
                EvidenceItem(
                    evidence_id=-1,
                    hop_type="mdna",
                    section_label=mdna_chunk["breadcrumb"] or mdna_chunk["section_title"],
                    excerpt=content[:_MDNA_EXCERPT_MAX],
                    source_ref="",  # rcept_no 앵커는 호출부에서 채움
                )
            )

    return items


def extract_findings_for_stock(
    client: DartClient,
    gemini: genai.Client,
    stock_code: str,
    force: bool = False,
    limit: int | None = None,
) -> list[FindingsResult]:
    book = load_corp_codes(client)
    corp = book.by_stock_code(stock_code)
    if corp is None:
        raise ValueError(f"종목코드 {stock_code}에 해당하는 기업 없음 (corpCode.xml 기준)")

    results: list[FindingsResult] = []
    with db.connection() as conn:
        targets = db.filings_for_findings(conn, corp.corp_code, force=force)
        if limit is not None:
            targets = targets[:limit]

        for f in targets:
            rcept_no, bsns_year, reprt_code = f["rcept_no"], f["bsns_year"], f["reprt_code"]
            try:
                diffs = db.all_diffs_for_filing(conn, rcept_no, comparison_type="QoQ")
                mdna_chunk = db.mdna_chunk_for_filing(conn, rcept_no)
                evidence = _build_evidence_catalogue(diffs, mdna_chunk)
                for item in evidence:
                    if item.evidence_id == -1:
                        item.source_ref = f"{rcept_no}#mdna"

                quarter = quarter_label(bsns_year, reprt_code)

                if not evidence:
                    db.delete_findings(conn, rcept_no)
                    db.delete_score_history(conn, rcept_no)
                    score_rows = compute_scores([], corp_code=corp.corp_code, rcept_no=rcept_no, quarter=quarter)
                    db.insert_score_history(conn, score_rows)
                    conn.commit()
                    results.append(FindingsResult(rcept_no, bsns_year, reprt_code, "no_evidence"))
                    continue

                by_id = {item.evidence_id: item for item in evidence}
                candidates = extract_findings(gemini, evidence)

                finding_rows = []
                scoring_findings: list[Finding] = []
                for c in candidates:
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
                            "corp_code": corp.corp_code,
                            "severity": c.severity,
                            "score_component": c.score_component,
                            "summary": c.summary,
                            "hops_json": json.dumps(hops, ensure_ascii=False),
                        }
                    )
                    scoring_findings.append(Finding(severity=c.severity, score_component=c.score_component))

                db.delete_findings(conn, rcept_no)
                db.insert_findings(conn, finding_rows)

                score_rows = compute_scores(
                    scoring_findings, corp_code=corp.corp_code, rcept_no=rcept_no, quarter=quarter
                )
                db.delete_score_history(conn, rcept_no)
                db.insert_score_history(conn, score_rows)

                conn.commit()  # 공시 1건 = 커밋 1건: 중단돼도 완료분 보존
                results.append(
                    FindingsResult(rcept_no, bsns_year, reprt_code, "extracted", len(finding_rows))
                )
            except Exception as e:  # 한 건의 실패가 나머지 공시 처리를 막지 않게
                conn.rollback()
                results.append(FindingsResult(rcept_no, bsns_year, reprt_code, "failed", detail=str(e)))

    return results
