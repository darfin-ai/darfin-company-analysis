"""LLM 폴리싱 — 순수 함수 (Stage 4의 첫 조각: 서술형 diff before/after 요약).

비용 통제 원칙(IMPLEMENTATION_PLAN.md §2 Stage 4): 모델은 공시 전문을 절대
보지 않는다. 이미 diff 엔진이 문단 단위로 격리해 둔 변경 구간(before/after)과
그 섹션 라벨만 입력한다.

이 모듈은 findings/overview/scores 등 Stage 4의 나머지 산출물은 다루지 않는다
(각각 별도 추출 설계가 필요한 더 큰 작업) — 여기서는 diff 엔진이 이미 만든
before/after 원문(diff.py의 문단 단위 격리 결과, 다소 거친 텍스트)을 사람이
읽기 좋은 요약으로 다듬는 것만 한다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from google import genai
from google.genai import types
from pydantic import BaseModel

MODEL_NAME = "gemini-2.5-flash"

# 대략적인 리스트 가격(2026년 기준, USD/1M 토큰) — 실제 청구액과 다를 수 있다.
# 비용 "가시성" 목적(ai_summary_result 패턴과 동일)이지 정산용 정확한 값이 아니다.
_PRICE_PER_1M_INPUT = 0.30
_PRICE_PER_1M_OUTPUT = 2.50

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
        ),
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    parsed: _DiffSummaryBatch = response.parsed
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
