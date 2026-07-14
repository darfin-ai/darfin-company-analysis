"""AI분석 Layer 2 오케스트레이션 — job_type='risk_analysis' llm_jobs 처리.

한 job = 한 회사:
1. text_extractions가 없는 filing을 시간순으로 추출(risk_extraction) —
   직전 filing의 item_key를 프롬프트에 넣어 cross-filing 매칭을 강제.
2. 인접 filing 간 item_key set-diff → dossier_events(item_appeared/disappeared).
3. 최신 분기 risk_states 중 내러티브가 없거나 낡은 행에 narrative_ko /
   watch_next_ko 생성(risk_narrative).

quant 상태(risk_states.state)는 Java(RiskAnalysisService)가 소유 — 여기서는
텍스트 컬럼(text_signals/narrative/watch_next/llm_updated_at)만 쓴다.
filing 단위 멱등(재실행 시 replace) — 다른 파이프라인 단계와 동일 규약.
"""

from __future__ import annotations

from google import genai

from . import db
from .risk_extraction import diff_events, extract_filing
from .risk_narrative import generate_narratives


def process_company(gemini: genai.Client, corp_code: str) -> tuple[bool, str]:
    """(성공 여부, 실패 사유). 부분 실패 시 이미 커밋된 filing 추출은 남는다."""
    with db.connection() as conn:
        targets = db.filings_for_risk_extraction(conn, corp_code)

    # 1~2) filing별 추출 + 직전 filing과의 diff 이벤트
    for f in targets:
        rcept_no = f["rcept_no"]
        try:
            with db.connection() as conn:
                chunks = db.chunks_for_risk_extraction(conn, rcept_no)
                prior = _prior_filing_items(conn, corp_code, f)
            prior_keys: dict[str, list[str]] = {}
            for item in prior:
                prior_keys.setdefault(item["category"], []).append(item["item_key"])

            result = extract_filing(gemini, corp_code, rcept_no, chunks, prior_keys)
            current_items = [
                {"category": r["category"], "item_key": r["item_key"],
                 "payload": r["payload_json"], "source_section": r["source_section"]}
                for r in result.rows
            ]
            events = diff_events(corp_code, rcept_no, prior, current_items) if prior else []

            with db.connection() as conn:
                db.delete_text_extractions(conn, rcept_no)
                inserted = db.insert_text_extractions(conn, result.rows)
                db.insert_dossier_events(conn, events)
            print(
                f"  {rcept_no}: 추출 {inserted}건, 이벤트 {len(events)}건 "
                f"(tokens {result.tokens_in}/{result.tokens_out})"
            )
        except Exception as e:  # noqa: BLE001 — job 단위로 실패를 보고
            return False, f"extraction {rcept_no}: {e}"

    # 3) 최신 분기 내러티브
    try:
        with db.connection() as conn:
            states = db.risk_states_needing_narrative(conn, corp_code)
            latest = db.filings_for_risk_extraction(conn, corp_code, force=True)
            latest_items = db.extraction_items(conn, latest[-1]["rcept_no"]) if latest else []

        if states:
            narratives = generate_narratives(gemini, states, latest_items)
            quarter = states[0]["quarter"]
            with db.connection() as conn:
                for category, (narrative, watch_next, text_signals) in narratives.by_category.items():
                    db.update_risk_state_text(
                        conn, corp_code, quarter, category, text_signals, narrative, watch_next
                    )
            print(
                f"  내러티브 {len(narratives.by_category)}건 "
                f"(tokens {narratives.tokens_in}/{narratives.tokens_out})"
            )
    except Exception as e:  # noqa: BLE001
        return False, f"narrative: {e}"

    return True, ""


def _prior_filing_items(conn, corp_code: str, filing: dict) -> list[dict]:
    """이 filing 직전(시간순) filing의 추출 항목 — item_key 매칭·diff 기준."""
    all_filings = db.filings_for_risk_extraction(conn, corp_code, force=True)
    ordered = [f["rcept_no"] for f in all_filings]
    try:
        idx = ordered.index(filing["rcept_no"])
    except ValueError:
        return []
    if idx == 0:
        return []
    return db.extraction_items(conn, ordered[idx - 1])
