"""findings → score_history 순수 계산 (Stage 4 일부). DB/LLM 비의존.

점수는 LLM이 아니라 이 필링에서 이미 만들어진 findings를 severity 가중치로
집계해서 결정한다 — findings와 점수가 항상 정합되고, 별도 LLM 호출이 없어
비용도 들지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

# 프론트 mock(darfin-front/src/mocks/companyAnalysis/samsungElectronics.js)의
# scores[].maxPoints와 동일 — IMPLEMENTATION_PLAN.md의 "governance 0-10" 서술은
# financialChange 외 세 컴포넌트를 뭉뚱그린 표현이고, 실제 프론트 코드가 정답이다.
SCORE_MAX_POINTS: dict[str, int] = {
    "financialChange": 40,
    "riskEscalation": 30,
    "managementEmphasis": 20,
    "governance": 10,
}

_SEVERITY_WEIGHT: dict[str, float] = {"high": 1.0, "medium": 0.6, "low": 0.3}

# severity-high finding 3개가 모이면 해당 컴포넌트가 만점에 도달하도록 하는 스케일.
_FINDINGS_TO_SATURATE = 3

_REPRT_CODE_TO_QUARTER = {
    "11013": "Q1",  # 1분기보고서
    "11012": "Q2",  # 반기보고서
    "11014": "Q3",  # 3분기보고서
    "11011": "Q4",  # 사업보고서(연간) — 다음 해 3월에 전년도 실적으로 제출됨
}


@dataclass
class Finding:
    severity: str  # high/medium/low
    score_component: str  # financialChange/riskEscalation/managementEmphasis/governance


def quarter_label(bsns_year: str, reprt_code: str) -> str:
    """예: (2026, 11013) -> '2026Q1', (2025, 11011) -> '2025Q4'."""
    suffix = _REPRT_CODE_TO_QUARTER.get(reprt_code)
    if suffix is None:
        raise ValueError(f"알 수 없는 reprt_code: {reprt_code}")
    return f"{bsns_year}{suffix}"


def compute_scores(
    findings: list[Finding],
    *,
    corp_code: str,
    rcept_no: str,
    quarter: str,
) -> list[dict]:
    """4개 score_component 전부에 대해 행을 만든다(관련 finding이 없으면 0점).

    프론트가 분기별 연속 시계열을 기대하므로(ScoreComponent.history) 컴포넌트를
    누락하지 않는다.
    """
    weighted_sum: dict[str, float] = {key: 0.0 for key in SCORE_MAX_POINTS}
    for f in findings:
        if f.score_component not in weighted_sum:
            continue  # 알 수 없는 component는 무시(방어적) — LLM 응답 검증에서 이미 걸러짐
        weighted_sum[f.score_component] += _SEVERITY_WEIGHT.get(f.severity, 0.0)

    rows = []
    for component, max_points in SCORE_MAX_POINTS.items():
        raw = weighted_sum[component] * max_points / _FINDINGS_TO_SATURATE
        value = round(min(max_points, raw), 2)
        rows.append(
            {
                "corp_code": corp_code,
                "rcept_no": rcept_no,
                "quarter": quarter,
                "component": component,
                "value": value,
                "max_points": max_points,
            }
        )
    return rows
