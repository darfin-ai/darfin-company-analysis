"""AI분석 Layer 2 — 공시 텍스트 섹션에서의 구조화 리스크 추출.

llm.py와 같은 원칙:
- Gemini 구조화 출력(response_schema=Pydantic), thinking_budget=0, 저온도.
- 모델은 사실 추출만 한다 — 수치 계산·판정은 전부 Java(MetricsCalculator/
  RiskStateMachine) 몫.
- 모든 항목은 source_section(text_chunks breadcrumb)을 기계적으로 가진다 —
  출처 표시(유사투자자문업 방어선).

핵심 설계 — item_key: 같은 항목(소송 사건번호, 보증 상대방 등)이 분기마다
표현이 조금씩 달라도 동일 키로 이어져야 분기 간 set-diff로
item_appeared/item_disappeared 이벤트를 만들 수 있다. 그래서 직전 공시의
키 목록을 프롬프트에 넣어 "기존 키에 매칭 가능하면 새 키를 만들지 말고
그대로 쓰라"고 지시한다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel

from .llm import MODEL_NAME, _NO_THINKING
from .llm_runtime import generate_content

# ddl.sql §8 text_extractions.category와 1:1.
CATEGORIES = (
    "audit_opinion", "going_concern", "contingent_liability", "guarantee",
    "related_party", "financing", "capacity_util", "customer_concentration",
    "segment_mix", "corporate_event",
)

# 카테고리 → 리스크 상태머신 카테고리 매핑 (risk_narrative가 텍스트 신호를
# 어느 카드에 붙일지). Java RiskStateMachine.CATEGORIES와 1:1.
STATE_CATEGORY_BY_EXTRACTION = {
    "audit_opinion": "going_concern",
    "going_concern": "going_concern",
    "contingent_liability": "leverage",
    "guarantee": "leverage",
    "related_party": "governance",
    "financing": "liquidity",
    "capacity_util": "operational",
    "customer_concentration": "operational",
    "segment_mix": "operational",
    "corporate_event": "governance",
}

# 주석 섹션은 수십만 자까지 가므로 청크당 상한 — flash 컨텍스트 낭비와
# 비용 폭주 방지. 잘린 꼬리는 이번 라운드 범위 밖(알려진 한계).
_MAX_CHARS_PER_CHUNK = 20_000
_MAX_TOTAL_CHARS = 200_000
_MAX_SECTIONS_PER_REQUEST = 10
_RISK_EXTRACTION_MAX_OUTPUT_TOKENS = 8_000

# 기간이 박힌 item_key 검출 — 이런 키는 분기마다 바뀌어 diff가 전부 잡음이 된다.
_PERIODIC_KEY = re.compile(r"\d{4}\s*년|\d\s*분기|\d{4}[.\-/]\d{1,2}|반기|사업연도")


class _ExtractedItem(BaseModel):
    category: Literal[
        "audit_opinion", "going_concern", "contingent_liability", "guarantee",
        "related_party", "financing", "capacity_util", "customer_concentration",
        "segment_mix", "corporate_event",
    ]
    # 안정 키: 소송=사건번호, 보증=상대방, 그 외=간결한 한국어 명사구.
    item_key: str
    summary: str
    amount_krw: float | None
    section_index: int  # 입력 섹션 배열의 index — source_section을 코드가 채운다


class _ExtractionBatch(BaseModel):
    items: list[_ExtractedItem]


_SYSTEM_INSTRUCTION = (
    "당신은 한국 기업 정기공시의 텍스트 섹션에서 리스크 관련 사실을 구조화해 "
    "추출하는 도구입니다. 입력은 섹션 배열(index + 제목 + 본문)이며, 다음 "
    "카테고리에 해당하는 항목만 추출하세요:\n"
    "- audit_opinion: 감사의견(적정/한정/부적정/의견거절)과 강조사항·핵심감사사항\n"
    "- going_concern: 계속기업 불확실성 언급\n"
    "- contingent_liability: 우발부채·소송 (금액, 전기 대비 변화 포함)\n"
    "- guarantee: 지급보증, 특히 계열사 대상\n"
    "- related_party: 특수관계자 거래 규모\n"
    "- financing: CB/BW/유상증자 등 자금조달\n"
    "- capacity_util: 가동률\n"
    "- customer_concentration: 주요 고객 집중도\n"
    "- segment_mix: 사업부문 매출 구성 변화\n"
    "- corporate_event: 최대주주 변경, 대표이사 변경, 배당 축소\n"
    "규칙:\n"
    "1. 본문에 없는 사실·수치를 절대 추가하지 마세요. 해당 항목이 없으면 빈 배열.\n"
    "2. item_key는 분기가 지나도 같은 항목이면 같아야 합니다 — 소송은 사건번호, "
    "보증은 상대방명처럼 안정적인 식별자를 쓰고, '직전 공시의 기존 키' 목록에 "
    "같은 항목이 있으면 반드시 그 키를 그대로 재사용하세요(새 키 발명 금지). "
    "item_key에 연도·분기·날짜(예: '2025년', '1분기')를 절대 포함하지 마세요 — "
    "기간이 들어가면 다음 분기에 같은 항목을 이어붙일 수 없습니다.\n"
    "3. amount_krw는 본문에 명시된 금액만(원 단위 환산), 없으면 null.\n"
    "4. section_index는 해당 사실이 나온 입력 섹션의 index를 그대로 반환하세요.\n"
    "5. summary는 한국어 1~2문장."
)


@dataclass
class ExtractionResult:
    rows: list[dict]        # db.insert_text_extractions 입력 shape
    tokens_in: int
    tokens_out: int


def extract_filing(
    client: genai.Client,
    corp_code: str,
    rcept_no: str,
    chunks: list[dict],
    prior_item_keys: dict[str, list[str]],
) -> ExtractionResult:
    """한 공시의 대상 섹션들에서 카테고리별 항목을 추출한다. chunks가 비면 빈 결과."""
    if not chunks:
        return ExtractionResult([], 0, 0)

    sections, total = [], 0
    for i, c in enumerate(chunks):
        content = (c["content"] or "")[:_MAX_CHARS_PER_CHUNK]
        if total + len(content) > _MAX_TOTAL_CHARS:
            break
        total += len(content)
        sections.append(f"[섹션 {i}] {c['breadcrumb']}\n{content}")

    prior_block = json.dumps(
        {k: v for k, v in prior_item_keys.items() if v}, ensure_ascii=False,
    )
    rows = []
    seen: set[tuple[str, str]] = set()
    tokens_in = tokens_out = 0
    for start in range(0, len(sections), _MAX_SECTIONS_PER_REQUEST):
        section_batch = sections[start : start + _MAX_SECTIONS_PER_REQUEST]
        contents = (
            f"직전 공시의 기존 키(카테고리별): {prior_block or '없음'}\n\n"
            + "\n\n===\n\n".join(section_batch)
        )
        response = generate_content(
            client,
            operation="risk_extraction",
            item_count=len(section_batch),
            model=MODEL_NAME,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_INSTRUCTION,
                temperature=0.1,
                response_mime_type="application/json",
                response_schema=_ExtractionBatch,
                thinking_config=_NO_THINKING,
                max_output_tokens=_RISK_EXTRACTION_MAX_OUTPUT_TOKENS,
            ),
        )
        parsed: _ExtractionBatch | None = response.parsed
        if parsed is None:
            finish = response.candidates[0].finish_reason if response.candidates else "?"
            raise RuntimeError(
                f"Gemini 추출 응답이 스키마로 파싱되지 않음 "
                f"(rcept_no={rcept_no}, sections={len(section_batch)}, finish_reason={finish})"
            )
        usage = response.usage_metadata
        if usage:
            tokens_in += usage.prompt_token_count or 0
            tokens_out += usage.candidates_token_count or 0
        for item in parsed.items:
            # 프롬프트의 section_index는 filing 전체 chunks의 원래 index다.
            if not (start <= item.section_index < start + len(section_batch)):
                continue
            key = (item.category, item.item_key.strip()[:120])
            if not key[1] or key in seen:
                continue
            seen.add(key)
            rows.append({
                "rcept_no": rcept_no,
                "corp_code": corp_code,
                "category": item.category,
                "item_key": key[1],
                "payload_json": json.dumps(
                    {"summary": item.summary, "amountKrw": item.amount_krw},
                    ensure_ascii=False,
                ),
                "source_section": chunks[item.section_index]["breadcrumb"][:500],
                "model_used": MODEL_NAME,
            })

    return ExtractionResult(rows, tokens_in, tokens_out)


def diff_events(
    corp_code: str,
    rcept_no: str,
    prior_items: list[dict],
    current_items: list[dict],
) -> list[dict]:
    """직전 공시와의 item_key set-diff → item_appeared/item_disappeared 이벤트.

    소멸 이벤트는 단일 공시 분석이 구조적으로 못 잡는 신호다 — 우발부채가
    해소 언급 없이 주석에서 사라지는 패턴. audit_opinion/segment_mix처럼
    매 공시 새로 쓰이는 서술형 카테고리와, 매 분기 집계가 갱신되는
    related_party(특수관계자 거래 규모)는 잡음이 많아 제외한다. 방어선으로
    기간이 박힌 키(예: '2025년 1분기 …')도 건너뛴다 — 프롬프트 지시만으로는
    위반이 실측됐다(삼성전자 related_party 키 70건 churn).
    """
    diffable = {"contingent_liability", "guarantee", "financing"}

    def usable(r: dict) -> bool:
        return r["category"] in diffable and not _PERIODIC_KEY.search(r["item_key"])

    prior = {(r["category"], r["item_key"]): r for r in prior_items if usable(r)}
    current = {(r["category"], r["item_key"]): r for r in current_items if usable(r)}

    events = []
    for key, row in current.items():
        if key not in prior:
            events.append(_event(corp_code, rcept_no, "item_appeared", key, row))
    for key, row in prior.items():
        if key not in current:
            events.append(_event(corp_code, rcept_no, "item_disappeared", key, row))
    return events


def _event(corp_code: str, rcept_no: str, event_type: str, key: tuple[str, str], row: dict) -> dict:
    payload = row.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    return {
        "corp_code": corp_code,
        "rcept_no": rcept_no,
        "event_type": event_type,
        "category": key[0],
        "item_key": key[1],
        "detail_json": json.dumps(
            {"summary": payload.get("summary"), "sourceSection": row.get("source_section")},
            ensure_ascii=False,
        ),
    }
