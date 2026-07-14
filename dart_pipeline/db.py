"""MariaDB(darfin) 접근 계층. 스키마: darfin-main/ddl.sql §7.

원칙: stock 테이블은 darfin-main이 소유 — 없을 때만 최소 행을 넣고
절대 갱신하지 않는다 (INSERT IGNORE).
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pymysql

from .config import DB_CONFIG


@contextmanager
def connection():
    conn = pymysql.connect(**DB_CONFIG, autocommit=False)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_company(conn, corp_code: str, corp_name: str, stock_code: str | None) -> None:
    """stock → companies 순으로 FK 사슬을 만족시키며 없으면 생성."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT IGNORE INTO stock (company_name, dart_corp_code, stock_code) VALUES (%s, %s, %s)",
            (corp_name, corp_code, stock_code),
        )
        cur.execute("INSERT IGNORE INTO companies (corp_code) VALUES (%s)", (corp_code,))


def existing_rcept_nos(conn, corp_code: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT rcept_no FROM filings WHERE corp_code = %s", (corp_code,))
        return {row[0] for row in cur.fetchall()}


def insert_filing(
    conn,
    *,
    rcept_no: str,
    corp_code: str,
    corp_name: str,
    bsns_year: str,
    reprt_code: str,
    filed_date: str,
    zip_path: str,
    xml_path: str,
) -> bool:
    """filings 행 삽입. 이미 있으면(rcept_no PK) False."""
    with conn.cursor() as cur:
        inserted = cur.execute(
            """
            INSERT IGNORE INTO filings
              (rcept_no, corp_code, corp_name, bsns_year, reprt_code, filed_date,
               zip_path, xml_path, pipeline_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'RAW')
            """,
            (rcept_no, corp_code, corp_name, bsns_year, reprt_code, filed_date, zip_path, xml_path),
        )
    return inserted > 0


def mark_failed(conn, rcept_no: str, error_message: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE filings SET pipeline_status = 'FAILED', error_message = %s WHERE rcept_no = %s",
            (error_message[:300], rcept_no),
        )


# ── DART 재무제표 원본 캐시 (financial_facts) ────────────────────────────
# darfin-main의 FinancialFactDao와 같은 테이블을 같은 upsert 의미론으로 쓴다
# (report_facts와 동일한 dual-writer 관례). 서빙(재무 추이 API)은 전적으로
# darfin-main 몫이고, 파이프라인은 (a) 온보딩/일일 스캔 시 캐시를 미리 덥히고
# (b) diff의 수치형 입력으로 읽기만 한다.


def filings_missing_financial_facts(conn, corp_code: str, force: bool = False) -> list[dict]:
    """financial_facts가 아직 없거나 낡은(정정공시로 rcept_no가 바뀐) 해당 기업의
    filings (rcept_no/bsns_year/reprt_code). 같은 (연도, 보고서)에 정정공시가
    여러 건이면 filed_date 최신 filing만 대상으로 삼는다.

    force=True면 이미 캐시된 기간도 포함한다 — 전면 재적재용.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.rcept_no, f.bsns_year, f.reprt_code
            FROM filings f
            JOIN (
              SELECT bsns_year, reprt_code, MAX(filed_date) AS max_filed
              FROM filings
              WHERE corp_code = %s AND pipeline_status != 'FAILED'
              GROUP BY bsns_year, reprt_code
            ) latest ON latest.bsns_year = f.bsns_year
                    AND latest.reprt_code = f.reprt_code
                    AND latest.max_filed = f.filed_date
            WHERE f.corp_code = %s AND f.pipeline_status != 'FAILED'
            """
            + (
                ""
                if force
                else """
              AND 2 > (
                SELECT COUNT(*) FROM financial_facts ff
                WHERE ff.corp_code = f.corp_code
                  AND ff.bsns_year = f.bsns_year
                  AND ff.reprt_code = f.reprt_code
                  AND ff.rcept_no = f.rcept_no
              )
            """
            ),
            (corp_code, corp_code),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def upsert_financial_fact(
    conn,
    *,
    corp_code: str,
    bsns_year: str,
    reprt_code: str,
    fs_div: str,
    rcept_no: str | None,
    payload: list[dict] | None,
) -> None:
    """payload=None이면 013 무자료 negative cache — darfin-main
    FinancialFactDao.upsertFinancialFact와 컬럼·의미 동일."""
    payload_json = None if payload is None else json.dumps(payload, ensure_ascii=False)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO financial_facts (corp_code, bsns_year, reprt_code, fs_div, rcept_no, payload_json)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              rcept_no = VALUES(rcept_no),
              payload_json = VALUES(payload_json),
              fetched_at = CURRENT_TIMESTAMP
            """,
            (corp_code, bsns_year, reprt_code, fs_div, rcept_no, payload_json),
        )


def financial_fact_payloads(conn, corp_code: str, bsns_year: str, reprt_code: str) -> dict[str, list[dict] | None]:
    """fs_div(CFS/OFS) → 저장된 fnlttSinglAcntAll 원본 rows. 캐시 없으면 키 없음,
    negative cache면 None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fs_div, payload_json FROM financial_facts
            WHERE corp_code = %s AND bsns_year = %s AND reprt_code = %s
            """,
            (corp_code, bsns_year, reprt_code),
        )
        rows = cur.fetchall()
    return {fs_div: (None if payload_json is None else json.loads(payload_json)) for fs_div, payload_json in rows}


# ── DART 정기보고서 주요정보 API 캐시 (report_facts) ─────────────────────


def filing_periods(conn, corp_code: str) -> list[dict]:
    """기업 filings에서 distinct (bsns_year, reprt_code) 목록."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT bsns_year, reprt_code
            FROM filings
            WHERE corp_code = %s AND pipeline_status != 'FAILED'
            ORDER BY bsns_year, reprt_code
            """,
            (corp_code,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def report_fact_exists(
    conn, corp_code: str, bsns_year: str, reprt_code: str, api_id: str
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM report_facts
            WHERE corp_code = %s AND bsns_year = %s AND reprt_code = %s AND api_id = %s
            """,
            (corp_code, bsns_year, reprt_code, api_id),
        )
        return cur.fetchone() is not None


def report_fact_payload(
    conn, corp_code: str, bsns_year: str, reprt_code: str, api_id: str
) -> list[dict] | None:
    """저장된 API rows. 행 없으면 KeyError, 013 캐시면 None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT payload_json FROM report_facts
            WHERE corp_code = %s AND bsns_year = %s AND reprt_code = %s AND api_id = %s
            """,
            (corp_code, bsns_year, reprt_code, api_id),
        )
        row = cur.fetchone()
    if row is None:
        raise KeyError(f"report_facts missing: {corp_code}/{bsns_year}/{reprt_code}/{api_id}")
    if row[0] is None:
        return None
    return json.loads(row[0])


def report_facts_missing(
    conn,
    corp_code: str,
    api_ids: list[str],
    force: bool = False,
) -> list[dict]:
    """아직 fetch하지 않은 (bsns_year, reprt_code, api_id) 조합."""
    periods = filing_periods(conn, corp_code)
    missing: list[dict] = []
    for period in periods:
        bsns_year, reprt_code = period["bsns_year"], period["reprt_code"]
        for api_id in api_ids:
            if force or not report_fact_exists(conn, corp_code, bsns_year, reprt_code, api_id):
                missing.append(
                    {"bsns_year": bsns_year, "reprt_code": reprt_code, "api_id": api_id}
                )
    return missing


def latest_filing_period(conn, corp_code: str) -> dict | None:
    """최신 비실패 filing의 (bsns_year, reprt_code, rcept_no). 없으면 None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT rcept_no, bsns_year, reprt_code
            FROM filings
            WHERE corp_code = %s AND pipeline_status != 'FAILED'
            ORDER BY filed_date DESC
            LIMIT 1
            """,
            (corp_code,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"rcept_no": row[0], "bsns_year": row[1], "reprt_code": row[2]}


def upsert_report_fact(
    conn,
    *,
    corp_code: str,
    bsns_year: str,
    reprt_code: str,
    api_id: str,
    payload: list[dict] | None,
    rcept_no: str | None = None,
) -> None:
    """payload=None이면 013 negative cache. rcept_no는 수집 시점의 최신 접수번호."""
    payload_json = None if payload is None else json.dumps(payload, ensure_ascii=False)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO report_facts (corp_code, bsns_year, reprt_code, api_id, rcept_no, payload_json)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              rcept_no = VALUES(rcept_no),
              payload_json = VALUES(payload_json),
              fetched_at = CURRENT_TIMESTAMP
            """,
            (corp_code, bsns_year, reprt_code, api_id, rcept_no, payload_json),
        )


def delete_report_facts_other_periods(
    conn, corp_code: str, bsns_year: str, reprt_code: str
) -> int:
    """회사당 최신 기간만 유지 — dartOverview용 report_facts에서 이전 기간 행 삭제."""
    with conn.cursor() as cur:
        return cur.execute(
            """
            DELETE FROM report_facts
            WHERE corp_code = %s AND (bsns_year != %s OR reprt_code != %s)
            """,
            (corp_code, bsns_year, reprt_code),
        )


def filings_for_parsing(conn, corp_code: str, force: bool = False) -> list[dict]:
    """파싱(text_chunks 적재) 대상 filings. force 없으면 아직 RAW인 것만 (재실행 시 이미
    PARSED된 것도 다시 돌리려면 force=True — 파서 개선 후 재처리에 사용)."""
    status_filter = "" if force else "AND f.pipeline_status = 'RAW'"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT rcept_no, corp_code, bsns_year, reprt_code, xml_path
            FROM filings f
            WHERE f.corp_code = %s AND f.pipeline_status != 'FAILED' {status_filter}
            """,
            (corp_code,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def delete_text_chunks(conn, rcept_no: str) -> None:
    """재실행 멱등성: 새로 채우기 전에 해당 공시의 기존 text_chunks를 지운다."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM text_chunks WHERE rcept_no = %s", (rcept_no,))


def insert_text_chunks(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO text_chunks
              (rcept_no, corp_code, section_title, canonical_label, assoc_note, atocid,
               breadcrumb, section_level, section_order, content, tables_json, content_hash, chunk_index)
            VALUES (%(rcept_no)s, %(corp_code)s, %(section_title)s, %(canonical_label)s, %(assoc_note)s, %(atocid)s,
                    %(breadcrumb)s, %(section_level)s, %(section_order)s, %(content)s, %(tables_json)s,
                    %(content_hash)s, %(chunk_index)s)
            """,
            rows,
        )
    return len(rows)


def mark_parsed(conn, rcept_no: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE filings SET pipeline_status = 'PARSED', error_message = NULL WHERE rcept_no = %s",
            (rcept_no,),
        )


def filings_for_diffing(conn, corp_code: str, force: bool = False) -> list[dict]:
    """diff 대상 filings. 기본은 PARSED 상태만, force=True면 DIFFED 이후도 재처리.

    baseline 결정에는 회사의 전체 filing 목록이 필요하므로(직전/전년 동분기)
    대상 여부와 무관하게 전체를 반환하고 is_target 플래그로 구분한다.
    정정공시로 같은 (연도, 보고서 유형)에 행이 여러 개면 filed_date 최신이 이긴다.
    """
    target_statuses = ("PARSED", "DIFFED", "SUMMARIZED") if force else ("PARSED",)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT rcept_no, corp_code, bsns_year, reprt_code, filed_date, pipeline_status
            FROM filings
            WHERE corp_code = %s AND pipeline_status != 'FAILED'
            ORDER BY bsns_year, filed_date
            """,
            (corp_code,),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    for r in rows:
        r["is_target"] = r["pipeline_status"] in target_statuses
    return rows


def load_chunks(conn, rcept_no: str) -> list[dict]:
    """한 공시의 text_chunks 전체 (diff 입력). section_order 순."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT section_title, canonical_label, assoc_note, atocid, breadcrumb,
                   section_level, section_order, content, tables_json, content_hash
            FROM text_chunks WHERE rcept_no = %s ORDER BY section_order
            """,
            (rcept_no,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def delete_section_diffs(conn, rcept_no: str) -> None:
    """재실행 멱등성: 새로 채우기 전에 해당 공시의 기존 section_diffs를 지운다."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM section_diffs WHERE rcept_no = %s", (rcept_no,))


def insert_section_diffs(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO section_diffs
              (rcept_no, baseline_rcept_no, corp_code, canonical_label, comparison_type,
               analysis_type, change_type, before_text, after_text, metrics_json,
               source_label, source_ref)
            VALUES (%(rcept_no)s, %(baseline_rcept_no)s, %(corp_code)s, %(canonical_label)s,
                    %(comparison_type)s, %(analysis_type)s, %(change_type)s, %(before_text)s,
                    %(after_text)s, %(metrics_json)s, %(source_label)s, %(source_ref)s)
            """,
            rows,
        )
    return len(rows)


def mark_diffed(conn, rcept_no: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE filings SET pipeline_status = 'DIFFED', error_message = NULL WHERE rcept_no = %s",
            (rcept_no,),
        )


# 요약 대상: LLM이 실제로 손댈 필요가 있는 서술형 분석 유형만
# (numeric/headcount/ownership은 이미 구조화된 metrics만 있고 before/after 텍스트가 없음)
_NARRATIVE_ANALYSIS_TYPES = ("text", "text_numeric", "structural", "event")


def filings_for_summarizing(conn, corp_code: str, force: bool = False) -> list[dict]:
    """LLM 요약 대상 filings. 기본은 DIFFED 상태만, force=True면 SUMMARIZED도 재처리."""
    target_statuses = ("DIFFED", "SUMMARIZED") if force else ("DIFFED",)
    placeholders = ",".join(["%s"] * len(target_statuses))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT rcept_no, corp_code, bsns_year, reprt_code
            FROM filings
            WHERE corp_code = %s AND pipeline_status IN ({placeholders})
            ORDER BY bsns_year, reprt_code
            """,
            (corp_code, *target_statuses),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def narrative_diffs_for_filing(conn, rcept_no: str) -> list[dict]:
    """LLM 폴리싱 대상 section_diffs 행 (서술형 + before/after 중 하나 이상 존재)."""
    placeholders = ",".join(["%s"] * len(_NARRATIVE_ANALYSIS_TYPES))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, canonical_label, comparison_type, analysis_type, change_type,
                   before_text, after_text, source_label, source_ref
            FROM section_diffs
            WHERE rcept_no = %s AND analysis_type IN ({placeholders})
              AND (before_text IS NOT NULL OR after_text IS NOT NULL)
            """,
            (rcept_no, *_NARRATIVE_ANALYSIS_TYPES),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def update_section_diff_text(conn, diff_id: int, before_text: str | None, after_text: str | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE section_diffs SET before_text = %s, after_text = %s WHERE id = %s",
            (before_text, after_text, diff_id),
        )


def delete_llm_summaries(conn, rcept_no: str) -> None:
    """재실행 멱등성: 새로 채우기 전에 해당 공시의 기존 llm_summaries를 지운다."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM llm_summaries WHERE rcept_no = %s", (rcept_no,))


def insert_llm_summaries(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO llm_summaries
              (rcept_no, corp_code, summary_type, content, source_refs,
               model_used, tokens_in, tokens_out, cost_usd, latency_ms)
            VALUES (%(rcept_no)s, %(corp_code)s, %(summary_type)s, %(content)s, %(source_refs)s,
                    %(model_used)s, %(tokens_in)s, %(tokens_out)s, %(cost_usd)s, %(latency_ms)s)
            """,
            rows,
        )
    return len(rows)


def mark_summarized(conn, rcept_no: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE filings SET pipeline_status = 'SUMMARIZED', error_message = NULL WHERE rcept_no = %s",
            (rcept_no,),
        )


def filings_for_findings(conn, corp_code: str, force: bool = False) -> list[dict]:
    """findings 추출 대상 filings. 기본은 아직 findings가 없는 DIFFED/SUMMARIZED만,
    force=True면 상태 무관하게 전체 재처리(기존 findings/score_history를 지우고 재생성)."""
    if force:
        query = """
            SELECT rcept_no, corp_code, bsns_year, reprt_code
            FROM filings
            WHERE corp_code = %s AND pipeline_status IN ('DIFFED', 'SUMMARIZED')
            ORDER BY bsns_year, reprt_code
        """
    else:
        query = """
            SELECT f.rcept_no, f.corp_code, f.bsns_year, f.reprt_code
            FROM filings f
            LEFT JOIN findings fd ON fd.rcept_no = f.rcept_no
            WHERE f.corp_code = %s AND f.pipeline_status IN ('DIFFED', 'SUMMARIZED')
            GROUP BY f.rcept_no, f.corp_code, f.bsns_year, f.reprt_code
            HAVING COUNT(fd.id) = 0
            ORDER BY f.bsns_year, f.reprt_code
        """
    with conn.cursor() as cur:
        cur.execute(query, (corp_code,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def all_diffs_for_filing(conn, rcept_no: str, comparison_type: str = "QoQ") -> list[dict]:
    """findings 증거 카탈로그 입력: analysis_type 무관 전체 section_diffs 행.

    numeric(재무상태표/손익계산서/현금흐름표)까지 포함해야 financial_anomaly
    hop을 만들 수 있다 — narrative_diffs_for_filing과 달리 필터링하지 않는다.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, canonical_label, analysis_type, change_type,
                   before_text, after_text, metrics_json, source_label, source_ref
            FROM section_diffs
            WHERE rcept_no = %s AND comparison_type = %s
            """,
            (rcept_no, comparison_type),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def mdna_chunk_for_filing(conn, rcept_no: str) -> dict | None:
    """이사의 경영진단 및 분석의견(MD&A) 텍스트 청크 1건.

    12개 표준 섹션에 없어(canonical_label NULL) section_diffs로 잡히지 않으므로
    text_chunks에서 제목으로 직접 조회한다. 실제 서술은 최상위 헤더 청크(예:
    "IV. 이사의 경영진단 및 분석의견", content 대개 빈 문자열)가 아니라 그
    하위 청크(예: "3. 재무상태 및 영업실적")에 들어있다 — section_title이 아니라
    breadcrumb으로 매칭해야 하위 청크까지 잡힌다. 여러 하위 청크로 쪼개졌을 수
    있어 가장 긴 것을 대표로 쓴다.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT section_title, breadcrumb, content
            FROM text_chunks
            WHERE rcept_no = %s AND breadcrumb LIKE '%%경영진단%%'
            ORDER BY CHAR_LENGTH(content) DESC
            LIMIT 1
            """,
            (rcept_no,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def delete_findings(conn, rcept_no: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM findings WHERE rcept_no = %s", (rcept_no,))


def insert_findings(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO findings
              (rcept_no, corp_code, severity, score_component, summary, hops_json)
            VALUES (%(rcept_no)s, %(corp_code)s, %(severity)s, %(score_component)s,
                    %(summary)s, %(hops_json)s)
            """,
            rows,
        )
    return len(rows)


def delete_score_history(conn, rcept_no: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM score_history WHERE rcept_no = %s", (rcept_no,))


def insert_score_history(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO score_history
              (corp_code, rcept_no, quarter, component, value, max_points)
            VALUES (%(corp_code)s, %(rcept_no)s, %(quarter)s, %(component)s,
                    %(value)s, %(max_points)s)
            """,
            rows,
        )
    return len(rows)


def filings_for_overview(conn, corp_code: str, force: bool = False) -> list[dict]:
    """company_overview 대상 filings. baseline(직전 filing) 체이닝에 전체
    이력이 필요하므로(diff.py의 filings_for_diffing과 같은 이유) 전체를 반환하고
    is_target 플래그로 실제 처리 대상만 구분한다."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.rcept_no, f.corp_code, f.bsns_year, f.reprt_code, f.filed_date,
                   (co.rcept_no IS NOT NULL) AS has_overview
            FROM filings f
            LEFT JOIN company_overview co ON co.rcept_no = f.rcept_no
            WHERE f.corp_code = %s AND f.pipeline_status != 'FAILED'
            ORDER BY f.bsns_year, f.filed_date
            """,
            (corp_code,),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    for r in rows:
        r["is_target"] = force or not r["has_overview"]
    return rows


def dividend_chunk_for_filing(conn, rcept_no: str) -> dict | None:
    """배당에 관한 사항 텍스트 청크. 12개 표준 섹션에 없어(canonical_label NULL)
    MD&A와 동일한 패턴으로 제목 조회한다."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT section_title, breadcrumb, content, tables_json
            FROM text_chunks
            WHERE rcept_no = %s AND section_title LIKE '%%배당%%'
            ORDER BY CHAR_LENGTH(content) DESC
            LIMIT 1
            """,
            (rcept_no,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def risk_chunks_for_filing(conn, rcept_no: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT section_title, breadcrumb, content
            FROM text_chunks
            WHERE rcept_no = %s AND canonical_label = '위험요인'
            ORDER BY section_order
            """,
            (rcept_no,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def overview_for_filing(conn, rcept_no: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT overview_json FROM company_overview WHERE rcept_no = %s", (rcept_no,))
        row = cur.fetchone()
        if row is None:
            return None
        return json.loads(row[0])


def delete_company_overview(conn, rcept_no: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM company_overview WHERE rcept_no = %s", (rcept_no,))


def insert_company_overview(conn, row: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO company_overview (rcept_no, corp_code, overview_json, model_used)
            VALUES (%(rcept_no)s, %(corp_code)s, %(overview_json)s, %(model_used)s)
            """,
            row,
        )


def filings_for_ai_insights(conn, corp_code: str, force: bool = False) -> list[dict]:
    """company_overview 2단계(insight/risks) 대상 filings. 1단계
    (build_deterministic_overview_for_stock)가 이미 결정론적 부분만 채워 둔
    행 중 `aiInsightsReady`가 false인 것, 그리고 아직 company_overview 행
    자체가 없는 것(1단계가 아직 안 돈 경우 — fast_path.py가 폴백으로 처리)
    까지 실제 처리 대상. baseline 체이닝에 전체 이력이 필요하므로
    filings_for_overview와 같은 모양(전체 반환 + is_target)으로 맞춘다.
    `aiInsightsReady` 필드 자체가 없는 구버전 행은 이미 완료된 것으로
    간주(하위 호환)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.rcept_no, f.corp_code, f.bsns_year, f.reprt_code, f.filed_date,
                   co.overview_json
            FROM filings f
            LEFT JOIN company_overview co ON co.rcept_no = f.rcept_no
            WHERE f.corp_code = %s AND f.pipeline_status != 'FAILED'
            ORDER BY f.bsns_year, f.filed_date
            """,
            (corp_code,),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    for r in rows:
        overview_json = r.pop("overview_json")
        if overview_json is None:
            r["is_target"] = True  # overview 자체가 없음 — fast_path 폴백 경로
            continue
        overview = json.loads(overview_json)
        ready = overview.get("aiInsightsReady") is not False
        r["is_target"] = force or not ready
    return rows


def update_overview_insights(conn, rcept_no: str, insight_by_key: dict, risks: list[dict]) -> None:
    """1단계가 이미 써둔 company_overview 행에 insight/risks만 patch한다 —
    결정론적 부분(segments/products/regions/shareholders/dividend 수치)은
    그대로 보존."""
    with conn.cursor() as cur:
        cur.execute("SELECT overview_json FROM company_overview WHERE rcept_no = %s", (rcept_no,))
        row = cur.fetchone()
        overview = json.loads(row[0])
        overview["segmentInsight"] = insight_by_key.get("segment")
        overview["productInsight"] = insight_by_key.get("product")
        overview["regionInsight"] = insight_by_key.get("region")
        overview["shareholderInsight"] = insight_by_key.get("shareholder")
        if overview.get("dividend"):
            overview["dividend"]["insight"] = insight_by_key.get("dividend")
        overview["risks"] = risks
        overview["aiInsightsReady"] = True
        cur.execute(
            "UPDATE company_overview SET overview_json = %s WHERE rcept_no = %s",
            (json.dumps(overview, ensure_ascii=False), rcept_no),
        )


# ── LLM 처리 대기열 (darfin-main이 클릭 시 등록하고, 워커가 소비) ──────────
# 작업 단위는 회사(corp_code) — summarize/findings/overview 오케스트레이션
# 함수들이 이미 "이 회사의 밀린 filing을 전부 찾아서 처리" 방식이라 필링
# 단위 추적이 필요 없다. 등록 경로가 on-demand 하나뿐이라 우선순위 개념은
# 없음(단순 FIFO) — 등록은 darfin-main(CompanyAnalysisService.enqueueOnDemandJob)
# 이 담당하고, 이 모듈은 claim/완료 처리만 한다.


def claim_next_job(conn) -> dict | None:
    """대기 중인 job 중 가장 오래된 1건을 잠그고(FOR UPDATE) 'running'으로
    표시한다. 같은 트랜잭션 커밋 전까지 다른 워커가 같은 행을 못 집는다(row lock).

    'running'으로 바뀐 뒤 워커가 죽으면(LLM 처리 중 크래시 등) 그 job은
    영영 'running'에 머무를 수 있다 — claim 자체는 짧게 커밋되고 그 뒤의
    느린 처리 도중 죽는 시나리오이므로 트랜잭션 롤백으로는 복구가 안 된다.
    그래서 15분 넘게 'running'인 job은 방치된 것으로 보고 다시 집는다."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, corp_code, job_type FROM llm_jobs "
            "WHERE status = 'pending' "
            "   OR (status = 'running' AND started_at < NOW() - INTERVAL 15 MINUTE) "
            # onboard_ingest 우선 — 이게 끝나야 그 회사의 개요/AI분석 나머지가
            # 전부 풀리므로(preview 탈출), 큐에 다른 job이 쌓여 있어도 새로
            # 관심등록된 회사가 뒤로 밀리지 않게 한다.
            "ORDER BY (job_type = 'onboard_ingest') DESC, requested_at ASC LIMIT 1 FOR UPDATE"
        )
        row = cur.fetchone()
        if row is None:
            return None
        job_id, corp_code, job_type = row
        cur.execute(
            "UPDATE llm_jobs SET status = 'running', started_at = CURRENT_TIMESTAMP WHERE id = %s",
            (job_id,),
        )
    return {"id": job_id, "corp_code": corp_code, "job_type": job_type}


def mark_job_done(conn, job_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE llm_jobs SET status = 'done', completed_at = CURRENT_TIMESTAMP WHERE id = %s",
            (job_id,),
        )


def mark_job_failed(conn, job_id: int, error_message: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE llm_jobs SET status = 'failed', completed_at = CURRENT_TIMESTAMP, error_message = %s WHERE id = %s",
            (error_message[:300], job_id),
        )


# ---------------------------------------------------------------------------
# AI분석 리스크 텍스트 레이어 (job_type='risk_analysis' — ddl.sql §8)
# ---------------------------------------------------------------------------

def filings_for_risk_extraction(conn, corp_code: str, force: bool = False) -> list[dict]:
    """text_extractions가 아직 없는 PARSED 이상 filings, 시간순. 정정공시로
    같은 (연도, 보고서)가 중복이면 filed_date 최신 행만 쓴다(§7 filings 규약)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.rcept_no, f.bsns_year, f.reprt_code, f.filed_date,
                   EXISTS(SELECT 1 FROM text_extractions te WHERE te.rcept_no = f.rcept_no) AS extracted
            FROM filings f
            JOIN (
                SELECT corp_code, bsns_year, reprt_code, MAX(filed_date) AS max_filed
                FROM filings WHERE corp_code = %s AND pipeline_status != 'FAILED'
                GROUP BY corp_code, bsns_year, reprt_code
            ) latest ON latest.bsns_year = f.bsns_year AND latest.reprt_code = f.reprt_code
                    AND latest.max_filed = f.filed_date
            WHERE f.corp_code = %s
              AND f.pipeline_status IN ('PARSED', 'DIFFED', 'SUMMARIZED')
            ORDER BY f.bsns_year, f.filed_date
            """,
            (corp_code, corp_code),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    return rows if force else [r for r in rows if not r["extracted"]]


def chunks_for_risk_extraction(conn, rcept_no: str) -> list[dict]:
    """리스크 추출 입력 섹션 — 감사의견/주석/사업의 내용/위험요인/지배구조/주주현황.
    breadcrumb가 text_extractions.source_section(출처 표시)이 된다."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT section_title, canonical_label, breadcrumb, content
            FROM text_chunks
            WHERE rcept_no = %s
              AND canonical_label IN ('주석', '사업의 내용', '위험요인', '지배구조', '주주현황')
            ORDER BY section_order
            """,
            (rcept_no,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def extraction_items(conn, rcept_no: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT category, item_key, payload_json, source_section FROM text_extractions WHERE rcept_no = %s",
            (rcept_no,),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    for r in rows:
        r["payload"] = json.loads(r.pop("payload_json"))
    return rows


def delete_text_extractions(conn, rcept_no: str) -> None:
    """재실행 멱등성 — parse/diff 단계와 동일한 replace 규약."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM text_extractions WHERE rcept_no = %s", (rcept_no,))


def insert_text_extractions(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO text_extractions
                (rcept_no, corp_code, category, item_key, payload_json, source_section, model_used)
            VALUES (%(rcept_no)s, %(corp_code)s, %(category)s, %(item_key)s,
                    %(payload_json)s, %(source_section)s, %(model_used)s)
            ON DUPLICATE KEY UPDATE payload_json = VALUES(payload_json),
                source_section = VALUES(source_section), model_used = VALUES(model_used)
            """,
            rows,
        )
        return cur.rowcount


def insert_dossier_events(conn, rows: list[dict]) -> int:
    """UNIQUE(uq_dossier_event) 충돌은 무시 — 재실행 멱등성."""
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT IGNORE INTO dossier_events
                (corp_code, rcept_no, event_type, category, item_key, detail_json)
            VALUES (%(corp_code)s, %(rcept_no)s, %(event_type)s, %(category)s,
                    %(item_key)s, %(detail_json)s)
            """,
            rows,
        )
        return cur.rowcount


def risk_states_needing_narrative(conn, corp_code: str) -> list[dict]:
    """내러티브가 없거나 quant 재계산(computed_at) 이후 갱신 안 된 최신 분기 행."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT quarter, category, state, consecutive_qtrs, quant_signals_json
            FROM risk_states
            WHERE corp_code = %s
              AND quarter = (SELECT MAX(quarter) FROM risk_states WHERE corp_code = %s)
              AND (llm_updated_at IS NULL OR llm_updated_at < computed_at)
            ORDER BY category
            """,
            (corp_code, corp_code),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    for r in rows:
        raw = r.pop("quant_signals_json")
        r["quant_signals"] = json.loads(raw) if raw else {}
    return rows


def update_risk_state_text(
    conn, corp_code: str, quarter: str, category: str,
    text_signals: dict | None, narrative_ko: str | None, watch_next_ko: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE risk_states
            SET text_signals_json = %s, narrative_ko = %s, watch_next_ko = %s,
                llm_updated_at = CURRENT_TIMESTAMP
            WHERE corp_code = %s AND quarter = %s AND category = %s
            """,
            (
                json.dumps(text_signals, ensure_ascii=False) if text_signals else None,
                narrative_ko, watch_next_ko, corp_code, quarter, category,
            ),
        )
