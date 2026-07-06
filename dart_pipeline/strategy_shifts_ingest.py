"""strategyShifts 오케스트레이션 — 여러 filing에 걸친 전략 전환 감지·저장.

findings/risks/insight는 filing 1건 대 직전 baseline(QoQ) 비교지만,
strategyShifts는 한 회사의 filing 역사 전체를 한 번에 봐야 하는 질문이라
다른 축(per-filing이 아니라 per-company)으로 처리한다. 그래서 on-demand
워커(run_llm_worker.py)가 그 회사의 밀린 filing을 다 처리한 뒤 마지막에
한 번만 이 함수를 호출해, 최신 filing의 company_overview에 patch한다
(재작성이 아니라 그 필드만 갱신 — update_overview_insights와 동일한 원칙).
"""

from __future__ import annotations

import json

from google import genai

from . import db
from .diff import _STATEMENT_LABELS
from .llm import FilingDigest, detect_strategy_shifts
from .scoring import quarter_label

_METRICS_KEEP = 6  # filing 1건당 재무 변동 요약이 너무 길어지지 않도록 상한
_MDNA_EXCERPT_MAX = 600
_MDNA_MIN_LEN = 200  # 이보다 짧으면 실질 내용 없는 정형 문구로 간주(아래 참고)
# 분기·반기보고서는 "이사의 경영진단 및 분석의견"을 기재 의무가 없어 이
# 정형 문구만 들어있다(사업보고서에만 실제 서술이 있음) — 실측(삼성전자)
# 기준 이 문구뿐인 청크는 66자, 실제 서술이 있는 사업보고서는 1만자 이상이라
# _MDNA_MIN_LEN과 문구 매칭 둘 다로 걸러낸다.
_MDNA_BOILERPLATE_MARKER = "기재하지 않습니다"


def _format_metrics_line(metrics_json: str | None) -> str | None:
    if not metrics_json:
        return None
    try:
        metrics = json.loads(metrics_json)
    except (TypeError, ValueError):
        return None
    if not metrics:
        return None
    lines = []
    for m in metrics[:_METRICS_KEEP]:
        label, current, baseline = m.get("label"), m.get("current"), m.get("baseline")
        if current is None or baseline is None:
            continue
        unit_label = m.get("unitLabel") or ("원" if m.get("unit") == "KRW" else "")
        lines.append(f"{label} {baseline:,}{unit_label}→{current:,}{unit_label}")
    return "; ".join(lines) if lines else None


def _build_digest(conn, f: dict) -> FilingDigest | None:
    rcept_no, bsns_year, reprt_code = f["rcept_no"], f["bsns_year"], f["reprt_code"]

    diffs = db.all_diffs_for_filing(conn, rcept_no, comparison_type="QoQ")
    fin_lines = [
        line
        for d in diffs
        if d["canonical_label"] in _STATEMENT_LABELS
        and (line := _format_metrics_line(d["metrics_json"])) is not None
    ]

    overview = db.overview_for_filing(conn, rcept_no)
    segment_line = None
    if overview and overview.get("segments"):
        segment_line = "; ".join(f"{s['name']} {s['revenueShare']}%" for s in overview["segments"])

    mdna = db.mdna_chunk_for_filing(conn, rcept_no)
    mdna_content = mdna["content"].strip() if mdna and mdna.get("content") else ""
    has_real_mdna = len(mdna_content) >= _MDNA_MIN_LEN and _MDNA_BOILERPLATE_MARKER not in mdna_content[:200]
    mdna_line = mdna_content[:_MDNA_EXCERPT_MAX] if has_real_mdna else None

    parts = []
    if fin_lines:
        parts.append("재무 변동: " + "; ".join(fin_lines))
    if segment_line:
        parts.append("사업부문 매출비중: " + segment_line)
    if mdna_line:
        parts.append("경영진 설명: " + mdna_line)

    if not parts:
        return None

    return FilingDigest(
        filing_id=rcept_no,
        quarter=quarter_label(bsns_year, reprt_code),
        facts="\n".join(parts),
        has_management_rationale=mdna_line is not None,
    )


def refresh_strategy_shifts_for_company(conn, gemini: genai.Client, corp_code: str) -> int:
    """한 회사의 filing 역사 전체를 한 번에 보고 strategyShifts를 갱신한다.

    가장 최근(=filings_for_ai_insights가 시간순으로 반환하는 마지막) filing의
    company_overview에만 patch — 감지된 shift 개수를 반환(0이면 빈 배열로 patch).
    """
    filings = db.filings_for_ai_insights(conn, corp_code)  # 시간순, FAILED 제외
    if not filings:
        return 0

    digests = [d for f in filings if (d := _build_digest(conn, f)) is not None]
    shifts = detect_strategy_shifts(gemini, digests)

    by_id = {d.filing_id: d for d in digests}
    shift_rows = []
    for s in shifts:
        evidence = by_id.get(s.evidence_filing_id)
        if evidence is None:  # 모델이 catalog에 없는 filing_id를 반환한 경우 — 방어
            continue
        shift_rows.append(
            {
                "quarter": evidence.quarter,
                "from": s.prior_focus,
                "to": s.new_focus,
                "metrics": s.metrics,
                "rationale": s.rationale,
                "sourceRef": f"{s.evidence_filing_id}#mdna",
            }
        )

    latest_rcept_no = filings[-1]["rcept_no"]
    db.update_overview_strategy_shifts(conn, latest_rcept_no, shift_rows)

    return len(shift_rows)
