"""파싱 결과 적재 오케스트레이션: RAW filings → dart_parser → text_chunks (Stage 2 PARSED).

fetch_metrics_for_stock와 달리 DART API를 호출하지 않는다 — 이미 디스크에 있는
xml_path를 dart_parser로 파싱해 DB에 반영할 뿐이므로 완전히 오프라인으로 동작한다
(corp_code 조회용 corpCode.xml 캐시만 필요, load_corp_codes가 24시간 캐시 사용).

각 공시는 멱등하게 처리: 재실행 시 기존 text_chunks를 지우고 다시 채운 뒤
pipeline_status를 PARSED로 갱신한다 (IMPLEMENTATION_PLAN.md §2 Stage 2 원칙).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from dart_parser import Section, parse_filing

from . import db
from .client import DartClient
from .corp_codes import load_corp_codes

_TITLE_MAX = 200
_BREADCRUMB_MAX = 500


@dataclass
class ParseResult:
    rcept_no: str
    bsns_year: str
    reprt_code: str
    action: str  # parsed / no_sections / failed
    n_sections: int = 0
    detail: str = ""


def _section_row(section: Section, rcept_no: str, corp_code: str) -> dict:
    tables = [asdict(t) for t in section.tables]
    return {
        "rcept_no": rcept_no,
        "corp_code": corp_code,
        "section_title": section.title[:_TITLE_MAX],
        "canonical_label": section.canonical,
        "assoc_note": section.assoc_note,
        "atocid": section.atocid,
        "breadcrumb": " > ".join(section.breadcrumb)[:_BREADCRUMB_MAX],
        "section_level": section.level,
        "section_order": section.order,
        "content": section.narrative,
        "tables_json": json.dumps(tables, ensure_ascii=False) if tables else None,
        "content_hash": section.content_hash,
        "chunk_index": 0,
    }


def parse_filings_for_stock(client: DartClient, stock_code: str, force: bool = False) -> list[ParseResult]:
    """한 기업의 filings를 파싱해 text_chunks에 채운다.

    기본은 아직 PARSED 이전(RAW)인 것만 대상으로 한다. force=True면 이미 PARSED된
    것도 다시 파싱한다 (파서 개선 후 재처리용) — 원본 XML은 디스크에 남아 있으므로
    재다운로드 없이 가능하다.
    """
    book = load_corp_codes(client)
    corp = book.by_stock_code(stock_code)
    if corp is None:
        raise ValueError(f"종목코드 {stock_code}에 해당하는 기업 없음 (corpCode.xml 기준)")

    results: list[ParseResult] = []
    with db.connection() as conn:
        targets = db.filings_for_parsing(conn, corp.corp_code, force=force)

        for f in targets:
            rcept_no, bsns_year, reprt_code = f["rcept_no"], f["bsns_year"], f["reprt_code"]
            try:
                filing = parse_filing(Path(f["xml_path"]))
                rows = [_section_row(s, rcept_no, corp.corp_code) for s in filing.sections]

                db.delete_text_chunks(conn, rcept_no)
                n = db.insert_text_chunks(conn, rows)
                db.mark_parsed(conn, rcept_no)
                conn.commit()  # 공시 1건 = 커밋 1건: 중단돼도 완료분 보존
                results.append(
                    ParseResult(rcept_no, bsns_year, reprt_code, "parsed" if n else "no_sections", n)
                )
            except Exception as e:  # 한 건의 실패가 나머지 공시 처리를 막지 않게
                conn.rollback()
                db.mark_failed(conn, rcept_no, str(e))
                conn.commit()
                results.append(ParseResult(rcept_no, bsns_year, reprt_code, "failed", detail=str(e)))

    return results
