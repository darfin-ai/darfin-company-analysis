"""LLM 호출 — 순수 함수 모음 (Stage 4: diff 폴리싱 + findings 추출).

비용 통제 원칙(IMPLEMENTATION_PLAN.md §2 Stage 4): 모델은 공시 전문을 절대
보지 않는다. 이미 diff 엔진이 문단 단위로 격리해 둔 변경 구간(before/after)과
그 섹션 라벨만 입력한다.

findings/risks 추출도 같은 원칙: sectionLabel/excerpt/sourceRef는 전부 DB 원본
행(또는 문단 단위로 쪼갠 청크)에서 코드가 기계적으로 채우고(모델 신뢰 X), 모델은
(1) 어떤 증거를 하나로 묶을지, (2) severity/scoreComponent(or title/description),
(3) summary/insight 텍스트만 결정한다.

company_overview의 segments/products/regions/shareholders/dividend는 이 모듈이
아니라 overview.py가 tables_json에서 결정론적으로 뽑는다(LLM 불필요) — 이
모듈은 그 결과에 대한 `*Insight` 한 줄(generate_panel_insights)과, 프로즈만
있어 결정론적으로 뽑을 수 없는 risks 패널(extract_risks)만 담당한다.

strategyShifts는 범위 밖(별도 라운드).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel

MODEL_NAME = "gemini-2.5-flash"

# 대략적인 리스트 가격(2026년 기준, USD/1M 토큰) — 실제 청구액과 다를 수 있다.
# 비용 "가시성" 목적(ai_summary_result 패턴과 동일)이지 정산용 정확한 값이 아니다.
_PRICE_PER_1M_INPUT = 0.30
_PRICE_PER_1M_OUTPUT = 2.50

# 여기서 하는 4가지 작업(요약/분류/추출)은 다단계 추론이 필요 없어 thinking을
# 끈다 — 실제로 SK하이닉스 검증 중 20건 배치 하나가 thinking 토큰만 244초
# 태우다 MAX_TOKENS로 잘려 응답 파싱에 실패하는 걸 확인했다(thinking_budget=0
# 없이는 출력 토큰 예산을 내부 추론이 다 써버릴 수 있음). thinking을 꺼도
# 33건짜리 배치는 16384로 여전히 잘렸다 — gemini-2.5-flash의 실제
# output_token_limit(65536, client.models.get()으로 확인)까지 열어준다.
_NO_THINKING = types.ThinkingConfig(thinking_budget=0)
_MAX_OUTPUT_TOKENS = 65536

_SYSTEM_INSTRUCTION = (
    "당신은 기업 공시 비교 결과를 요약하는 보조 도구입니다. "
    "입력은 여러 개의 '변경 전'/'변경 후' 항목으로 구성된 배열이며, 각 항목은 "
    "이미 두 공시를 기계적으로 비교해 뽑아낸 변경 구간입니다. 각 항목을 "
    "한국어 2문장 이내로 간결하게 요약하고, 입력과 동일한 개수·순서로 "
    "결과 배열을 반환하세요.\n"
    "규칙:\n"
    "1. 입력된 텍스트에 없는 사실·수치·추측을 절대 추가하지 마세요.\n"
    "2. 입력에 없는 쪽(예: 신규 추가라 '변경 전'이 없음)은 해당 필드를 null로 두세요.\n"
    "3. 문어체 공시 표현을 자연스러운 설명체로 바꾸되 의미는 보존하세요.\n"
    "4. 각 항목의 index는 입력에 주어진 값을 그대로 돌려주세요."
)


class _DiffSummary(BaseModel):
    index: int
    before: str | None
    after: str | None


class _DiffSummaryBatch(BaseModel):
    results: list[_DiffSummary]


@dataclass
class DiffEntry:
    canonical_label: str
    source_label: str | None
    before: str | None
    after: str | None


@dataclass
class PolishResult:
    before: str | None
    after: str | None
    model_used: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int


def _entry_prompt(index: int, entry: DiffEntry) -> str:
    lines = [f"index: {index}", f"섹션: {entry.canonical_label}"]
    if entry.source_label:
        lines.append(f"세부 위치: {entry.source_label}")
    lines.append(f"변경 전: {entry.before or '(없음 — 이번 공시에서 신규 추가된 내용)'}")
    lines.append(f"변경 후: {entry.after or '(없음 — 이번 공시에서 삭제된 내용)'}")
    return "\n".join(lines)


def polish_diff_entries(client: genai.Client, entries: list[DiffEntry]) -> list[PolishResult]:
    """diff 엔진이 격리한 거친 before/after 여러 건을 한 번의 호출로 다듬는다.

    한 공시(filing)의 narrative diff 항목 전체를 배열로 묶어 보내고, 응답도
    같은 개수의 배열로 받는다. 토큰/비용은 호출 전체 사용량을 항목별로
    비례 배분해 기록한다(개별 호출 대비 정밀도는 낮지만 감사 목적 가시성은
    유지된다).
    """
    if not entries:
        return []

    contents = "\n\n---\n\n".join(_entry_prompt(i, e) for i, e in enumerate(entries))

    started = time.monotonic()
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_INSTRUCTION,
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=_DiffSummaryBatch,
            thinking_config=_NO_THINKING,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
        ),
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    parsed: _DiffSummaryBatch | None = response.parsed
    if parsed is None:
        raise RuntimeError(
            f"Gemini 응답이 스키마로 파싱되지 않음 (finish_reason="
            f"{response.candidates[0].finish_reason if response.candidates else '?'}) — "
            f"entries={len(entries)}건, latency={latency_ms}ms"
        )
    by_index = {item.index: item for item in parsed.results}

    usage = response.usage_metadata
    tokens_in = usage.prompt_token_count or 0
    tokens_out = usage.candidates_token_count or 0
    cost_usd = tokens_in / 1_000_000 * _PRICE_PER_1M_INPUT + tokens_out / 1_000_000 * _PRICE_PER_1M_OUTPUT

    n = len(entries)
    # 비례 배분: 정수 나눗셈 잔차는 마지막 항목에 몰아 합계가 실제 사용량과 일치하게 한다.
    share_in = tokens_in // n
    share_out = tokens_out // n
    share_cost = cost_usd / n

    results: list[PolishResult] = []
    for i, entry in enumerate(entries):
        item = by_index.get(i)
        # 원본에 없던 쪽을 모델이 지어내 채우지 않도록 입력 형태를 그대로 강제.
        # 반대로 원본에 있던 쪽을 모델이 비워 반환하면(드문 응답 이상) 내용
        # 유실을 막기 위해 원문으로 폴백한다.
        polished_before = ((item.before if item else None) if entry.before else None) or entry.before
        polished_after = ((item.after if item else None) if entry.after else None) or entry.after

        is_last = i == n - 1
        results.append(
            PolishResult(
                before=polished_before,
                after=polished_after,
                model_used=MODEL_NAME,
                tokens_in=share_in + (tokens_in - share_in * n) if is_last else share_in,
                tokens_out=share_out + (tokens_out - share_out * n) if is_last else share_out,
                cost_usd=round(share_cost, 6),
                latency_ms=latency_ms // n,
            )
        )

    return results


# ---------------------------------------------------------------------------
# findings 추출
# ---------------------------------------------------------------------------

_FINDINGS_SYSTEM_INSTRUCTION = (
    "당신은 기업 공시의 변경 사항을 분석해 투자자에게 의미 있는 관찰(finding)로 "
    "묶어내는 보조 도구입니다. 입력은 번호(evidence_id)가 매겨진 증거 항목 배열이며, "
    "각 항목은 이미 두 공시를 기계적으로 비교하거나 파싱해 뽑아낸 사실입니다.\n"
    "서로 연관된 증거들을 묶어 최대 5개 이내의 finding으로 정리하세요. 각 finding은:\n"
    "- evidence_ids: 근거로 삼은 evidence_id 목록 (반드시 입력에 실제로 존재하는 값, 1개 이상)\n"
    "- severity: high/medium/low 중 하나 (투자자에게 미치는 영향 크기)\n"
    "- score_component: financialChange(재무 변동)/riskEscalation(위험 확대)/"
    "managementEmphasis(경영진 강조)/governance(지배구조·공시) 중 가장 적합한 하나\n"
    "- summary: 이 finding을 설명하는 한국어 1~2문장 헤드라인\n"
    "규칙:\n"
    "1. 입력된 증거에 없는 사실·수치·추측을 절대 추가하지 마세요.\n"
    "2. evidence_ids는 입력에 주어진 evidence_id 값만 사용하세요 — 존재하지 않는 id를 "
    "만들어내지 마세요.\n"
    "3. 사소하거나 형식적인 변경(기준일 문자열 갱신 등)은 finding으로 만들지 마세요.\n"
    "4. 증거가 서로 무관하면 억지로 묶지 말고 각각 별도 finding으로 두세요."
)

_ScoreComponent = Literal["financialChange", "riskEscalation", "managementEmphasis", "governance"]
_Severity = Literal["high", "medium", "low"]


class _FindingCandidate(BaseModel):
    evidence_ids: list[int]
    severity: _Severity
    score_component: _ScoreComponent
    summary: str


class _FindingBatch(BaseModel):
    findings: list[_FindingCandidate]


@dataclass
class EvidenceItem:
    """findings 증거 카탈로그 항목. hop_type/section_label/excerpt/source_ref는 전부
    호출 전에 코드가 DB 원본 행에서 채운다 — LLM은 evidence_id로만 참조한다."""

    evidence_id: int
    hop_type: str  # financial_anomaly/note/mdna
    section_label: str
    excerpt: str
    source_ref: str


@dataclass
class FindingCandidate:
    evidence_ids: list[int]
    severity: str
    score_component: str
    summary: str


def _evidence_prompt(item: EvidenceItem) -> str:
    return (
        f"evidence_id: {item.evidence_id}\n"
        f"섹션: {item.section_label}\n"
        f"내용: {item.excerpt}"
    )


def extract_findings(client: genai.Client, evidence: list[EvidenceItem]) -> list[FindingCandidate]:
    """한 filing의 증거 카탈로그를 한 번에 보내 findings 후보를 뽑는다.

    응답의 evidence_ids가 실제 카탈로그에 없는 값을 참조하면 그 id만 걸러내고,
    걸러낸 뒤 evidence_ids가 비게 된 finding은 통째로 버린다(모델이 근거 없이
    지어낸 finding을 신뢰하지 않는다).
    """
    if not evidence:
        return []

    valid_ids = {item.evidence_id for item in evidence}
    contents = "\n\n---\n\n".join(_evidence_prompt(item) for item in evidence)

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=_FINDINGS_SYSTEM_INSTRUCTION,
            temperature=0.2,
            response_mime_type="application/json",
            response_schema=_FindingBatch,
            thinking_config=_NO_THINKING,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
        ),
    )

    parsed: _FindingBatch | None = response.parsed
    if parsed is None:
        raise RuntimeError(
            f"Gemini 응답이 스키마로 파싱되지 않음 (finish_reason="
            f"{response.candidates[0].finish_reason if response.candidates else '?'}) — "
            f"evidence={len(evidence)}건"
        )

    results: list[FindingCandidate] = []
    for candidate in parsed.findings:
        kept_ids = [eid for eid in candidate.evidence_ids if eid in valid_ids]
        if not kept_ids:
            continue
        results.append(
            FindingCandidate(
                evidence_ids=kept_ids,
                severity=candidate.severity,
                score_component=candidate.score_component,
                summary=candidate.summary,
            )
        )
    return results


# ---------------------------------------------------------------------------
# risks 추출 (company_overview 일부)
# ---------------------------------------------------------------------------

_RISKS_SYSTEM_INSTRUCTION = (
    "당신은 기업 공시의 위험요인 서술을 투자자가 읽기 좋은 핵심 리스크 항목으로 "
    "정리하는 보조 도구입니다. 입력은 번호(evidence_id)가 매겨진 문단 배열이며, "
    "각 문단은 공시의 위험관리 관련 서술을 그대로 잘라낸 것입니다.\n"
    "이 중 투자자에게 의미 있는 핵심 리스크를 최대 5개까지 뽑아 각각:\n"
    "- evidence_ids: 근거로 삼은 문단 evidence_id 목록 (입력에 실제로 존재하는 값, 1개 이상)\n"
    "- title: 리스크를 짧게 요약한 제목 (10자 내외)\n"
    "- description: 리스크 내용을 설명하는 한국어 1~2문장\n"
    "- severity: high/medium/low 중 하나\n"
    "규칙:\n"
    "1. 입력에 없는 사실·수치·추측을 절대 추가하지 마세요.\n"
    "2. evidence_ids는 입력에 주어진 값만 사용하세요.\n"
    "3. 형식적이거나 모든 기업에 공통되는 상투적 서술(예: 일반적인 환율 변동 언급)보다는 "
    "이 공시에 특정된 내용을 우선하세요."
)


class _RiskCandidate(BaseModel):
    evidence_ids: list[int]
    title: str
    description: str
    severity: _Severity


class _RiskBatch(BaseModel):
    risks: list[_RiskCandidate]


@dataclass
class RiskCandidate:
    evidence_ids: list[int]
    title: str
    description: str
    severity: str


def extract_risks(client: genai.Client, evidence: list[EvidenceItem]) -> list[RiskCandidate]:
    """위험요인 청크를 문단 단위로 쪼갠 evidence에서 핵심 리스크를 뽑는다.

    extract_findings와 동일한 검증 원칙: evidence_ids가 카탈로그에 없으면
    걸러내고, 다 걸러지면 해당 리스크는 버린다.
    """
    if not evidence:
        return []

    valid_ids = {item.evidence_id for item in evidence}
    contents = "\n\n---\n\n".join(_evidence_prompt(item) for item in evidence)

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=_RISKS_SYSTEM_INSTRUCTION,
            temperature=0.2,
            response_mime_type="application/json",
            response_schema=_RiskBatch,
            thinking_config=_NO_THINKING,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
        ),
    )

    parsed: _RiskBatch | None = response.parsed
    if parsed is None:
        raise RuntimeError(
            f"Gemini 응답이 스키마로 파싱되지 않음 (finish_reason="
            f"{response.candidates[0].finish_reason if response.candidates else '?'}) — "
            f"evidence={len(evidence)}건"
        )

    results: list[RiskCandidate] = []
    for candidate in parsed.risks:
        kept_ids = [eid for eid in candidate.evidence_ids if eid in valid_ids]
        if not kept_ids:
            continue
        results.append(
            RiskCandidate(
                evidence_ids=kept_ids,
                title=candidate.title,
                description=candidate.description,
                severity=candidate.severity,
            )
        )
    return results


# ---------------------------------------------------------------------------
# 패널 insight 생성 (company_overview 일부)
# ---------------------------------------------------------------------------

_INSIGHT_SYSTEM_INSTRUCTION = (
    "당신은 기업 공시의 수치 요약을 읽고 투자자에게 의미를 짚어주는 보조 도구입니다. "
    "입력은 여러 개의 패널 요약(panel_key + 수치 사실)으로 구성된 배열이며, 각 "
    "요약은 이미 공시에서 기계적으로 뽑아낸 사실입니다. 각 패널에 대해 'So "
    "what?'에 해당하는 한국어 1~2문장 해설을 쓰고, 입력과 동일한 개수·순서로 "
    "결과 배열을 반환하세요.\n"
    "규칙:\n"
    "1. 입력에 없는 사실·수치를 절대 추가하지 마세요.\n"
    "2. 각 항목의 index는 입력에 주어진 값을 그대로 돌려주세요."
)


class _PanelInsight(BaseModel):
    index: int
    insight: str


class _PanelInsightBatch(BaseModel):
    results: list[_PanelInsight]


@dataclass
class PanelFact:
    panel_key: str
    fact_summary: str


def _panel_prompt(index: int, fact: PanelFact) -> str:
    return f"index: {index}\n패널: {fact.panel_key}\n수치 사실: {fact.fact_summary}"


def generate_panel_insights(client: genai.Client, panels: list[PanelFact]) -> list[str]:
    """여러 패널의 결정론적 수치 요약을 한 번에 보내 'So what?' 한 줄씩 받는다."""
    if not panels:
        return []

    contents = "\n\n---\n\n".join(_panel_prompt(i, p) for i, p in enumerate(panels))

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=_INSIGHT_SYSTEM_INSTRUCTION,
            temperature=0.3,
            response_mime_type="application/json",
            response_schema=_PanelInsightBatch,
            thinking_config=_NO_THINKING,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
        ),
    )

    parsed: _PanelInsightBatch | None = response.parsed
    if parsed is None:
        raise RuntimeError(
            f"Gemini 응답이 스키마로 파싱되지 않음 (finish_reason="
            f"{response.candidates[0].finish_reason if response.candidates else '?'}) — "
            f"panels={len(panels)}건"
        )
    by_index = {item.index: item.insight for item in parsed.results}
    return [by_index.get(i, "") for i in range(len(panels))]
