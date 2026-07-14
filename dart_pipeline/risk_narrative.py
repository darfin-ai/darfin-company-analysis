"""AI분석 Layer 2 — 리스크 상태의 한국어 서사(narrative_ko)와
"차기 분기 확인 사항"(watch_next_ko) 생성.

원칙(스펙 §2.1): LLM은 절대 계산하지 않는다 — 입력은 이미 Java 상태머신이
판정한 상태 + 판정에 쓰인 정량 신호 스냅샷 + 텍스트 추출 항목뿐이고, 모델은
그것을 자연스러운 한국어로 설명하고 다음 분기에 확인할 항목 한 줄을 쓴다.
출력은 모든 사용자에게 동일(개인화 없음 — 유사투자자문업 요건).

목표 출력 예:
  유동성: 악화 3분기 연속 — 유동비율 자체 3년 평균 대비 −2σ.
  차기 분기 확인 사항: 단기차입금 차환 여부.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from google import genai
from google.genai import types
from pydantic import BaseModel

from .llm import MODEL_NAME, _MAX_OUTPUT_TOKENS, _NO_THINKING
from .risk_extraction import STATE_CATEGORY_BY_EXTRACTION

_SYSTEM_INSTRUCTION = (
    "당신은 기업 리스크 분석 시스템의 판정 결과를 투자자용 한국어 문장으로 "
    "바꾸는 도구입니다. 입력은 카테고리별 항목 배열이며 각 항목에는 이미 "
    "계산된 상태(정상/악화/지속 등), 연속 분기 수, 판정에 쓰인 정량 신호, "
    "관련 공시 텍스트 발췌가 들어 있습니다.\n"
    "각 항목에 대해:\n"
    "- narrative: 상태와 근거를 요약한 한국어 1~2문장. 입력에 있는 수치·사실만 "
    "사용하고 새 수치를 계산하거나 추측하지 마세요.\n"
    "- watch_next: '차기 분기 확인 사항' 한 문장 — 이 상태가 이어질지 판별할 "
    "구체적 확인 포인트(예: '단기차입금 차환 여부'). 상태가 정상이면 null.\n"
    "규칙: 투자 권유·매수/매도 판단 표현 금지(정보 제공만). 카테고리와 index는 "
    "입력값 그대로 반환."
)


class _Narrative(BaseModel):
    index: int
    narrative: str
    watch_next: str | None


class _NarrativeBatch(BaseModel):
    results: list[_Narrative]


@dataclass
class NarrativeResult:
    by_category: dict[str, tuple[str, str | None, dict | None]]  # category → (narrative, watch_next, text_signals)
    tokens_in: int
    tokens_out: int


def generate_narratives(
    client: genai.Client,
    states: list[dict],
    latest_extractions: list[dict],
) -> NarrativeResult:
    """최신 분기 risk_states 행들에 대한 서사 생성.

    states: db.risk_states_needing_narrative() 결과.
    latest_extractions: 최신 공시의 db.extraction_items() 결과 — 상태 카테고리에
    매핑해 텍스트 근거로 첨부한다(text_signals_json에도 그대로 남겨 감사 추적).
    """
    if not states:
        return NarrativeResult({}, 0, 0)

    text_by_state_category: dict[str, list[dict]] = {}
    for item in latest_extractions:
        state_cat = STATE_CATEGORY_BY_EXTRACTION.get(item["category"])
        if state_cat:
            text_by_state_category.setdefault(state_cat, []).append({
                "category": item["category"],
                "itemKey": item["item_key"],
                "summary": (item.get("payload") or {}).get("summary"),
                "sourceSection": item.get("source_section"),
            })

    entries = []
    for i, s in enumerate(states):
        entries.append({
            "index": i,
            "category": s["category"],
            "state": s["state"],
            "consecutiveQtrs": s["consecutive_qtrs"],
            "quantSignals": s.get("quant_signals") or {},
            "textEvidence": text_by_state_category.get(s["category"], [])[:10],
        })

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=json.dumps(entries, ensure_ascii=False),
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_INSTRUCTION,
            temperature=0.2,
            response_mime_type="application/json",
            response_schema=_NarrativeBatch,
            thinking_config=_NO_THINKING,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
        ),
    )
    parsed: _NarrativeBatch | None = response.parsed
    if parsed is None:
        raise RuntimeError("Gemini 내러티브 응답이 스키마로 파싱되지 않음")

    by_category: dict[str, tuple[str, str | None, dict | None]] = {}
    for r in parsed.results:
        if not (0 <= r.index < len(states)):
            continue  # 환각 index 방지
        s = states[r.index]
        evidence = text_by_state_category.get(s["category"], [])[:10]
        by_category[s["category"]] = (
            r.narrative,
            r.watch_next,
            {"items": evidence} if evidence else None,
        )

    usage = response.usage_metadata
    return NarrativeResult(
        by_category,
        usage.prompt_token_count or 0 if usage else 0,
        usage.candidates_token_count or 0 if usage else 0,
    )
