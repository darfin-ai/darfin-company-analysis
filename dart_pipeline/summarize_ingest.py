"""LLM 요약 오케스트레이션: DIFFED filings → section_diffs before/after 폴리싱 (Stage 4 일부).

Stage 4의 나머지 산출물(findings, overview, strategyShifts, scores)은 각각
별도 추출 설계가 필요해 이 모듈의 범위가 아니다 — 여기서는 diff 엔진(Stage 3)이
이미 만들어 둔 서술형 section_diffs 행의 before/after를 사람이 읽기 좋게
다듬고, 호출마다 llm_summaries에 비용/토큰을 기록한다.

각 공시는 멱등하게 처리: 재실행 시 기존 llm_summaries를 지우고 다시 채우며,
section_diffs.before_text/after_text도 다시 폴리싱한다(같은 diff.py 원문에서
재생성되므로 안전). pipeline_status를 SUMMARIZED로 갱신한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from google import genai

from . import db
from .client import DartClient
from .corp_codes import load_corp_codes
from .llm import DiffEntry, polish_diff_entries


@dataclass
class SummarizeResult:
    rcept_no: str
    bsns_year: str
    reprt_code: str
    action: str  # summarized / no_entries / failed
    n_entries: int = 0
    total_cost_usd: float = 0.0
    detail: str = ""


def summarize_filings_for_stock(
    client: DartClient,
    gemini: genai.Client,
    stock_code: str,
    force: bool = False,
    limit: int | None = None,
) -> list[SummarizeResult]:
    """한 기업의 DIFFED filings를 대상으로 서술형 diff before/after를 LLM으로 다듬는다.

    limit이 있으면 대상 filings 중 앞에서부터 그만큼만 처리한다 (비용 통제된
    소규모 검증용).
    """
    book = load_corp_codes(client)
    corp = book.by_stock_code(stock_code)
    if corp is None:
        raise ValueError(f"종목코드 {stock_code}에 해당하는 기업 없음 (corpCode.xml 기준)")

    results: list[SummarizeResult] = []
    with db.connection() as conn:
        targets = db.filings_for_summarizing(conn, corp.corp_code, force=force)
        if limit is not None:
            targets = targets[:limit]

        for f in targets:
            rcept_no, bsns_year, reprt_code = f["rcept_no"], f["bsns_year"], f["reprt_code"]
            try:
                diffs = db.narrative_diffs_for_filing(conn, rcept_no)
                if not diffs:
                    db.mark_summarized(conn, rcept_no)
                    conn.commit()
                    results.append(SummarizeResult(rcept_no, bsns_year, reprt_code, "no_entries"))
                    continue

                summary_rows = []
                total_cost = 0.0
                polish_results = polish_diff_entries(
                    gemini,
                    [
                        DiffEntry(
                            canonical_label=d["canonical_label"],
                            source_label=d["source_label"],
                            before=d["before_text"],
                            after=d["after_text"],
                        )
                        for d in diffs
                    ],
                )
                for d, result in zip(diffs, polish_results):
                    db.update_section_diff_text(conn, d["id"], result.before, result.after)
                    summary_rows.append(
                        {
                            "rcept_no": rcept_no,
                            "corp_code": corp.corp_code,
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
                    total_cost += result.cost_usd

                db.delete_llm_summaries(conn, rcept_no)
                db.insert_llm_summaries(conn, summary_rows)
                db.mark_summarized(conn, rcept_no)
                conn.commit()  # 공시 1건 = 커밋 1건: 중단돼도 완료분 보존
                results.append(
                    SummarizeResult(
                        rcept_no, bsns_year, reprt_code, "summarized", len(diffs), round(total_cost, 6)
                    )
                )
            except Exception as e:  # 한 건의 실패가 나머지 공시 처리를 막지 않게
                conn.rollback()
                results.append(SummarizeResult(rcept_no, bsns_year, reprt_code, "failed", detail=str(e)))

    return results
