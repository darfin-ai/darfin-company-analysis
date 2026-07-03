"""MariaDB(darfin) 접근 계층. 스키마: darfin-main/ddl.sql §7.

원칙: stock 테이블은 darfin-main이 소유 — 없을 때만 최소 행을 넣고
절대 갱신하지 않는다 (INSERT IGNORE).
"""

from __future__ import annotations

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


def filings_missing_metrics(conn, corp_code: str) -> list[dict]:
    """metrics가 아직 없는 해당 기업의 filings (rcept_no/bsns_year/reprt_code)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.rcept_no, f.bsns_year, f.reprt_code
            FROM filings f
            LEFT JOIN metrics m ON m.rcept_no = f.rcept_no
            WHERE f.corp_code = %s AND m.id IS NULL AND f.pipeline_status != 'FAILED'
            """,
            (corp_code,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def delete_metrics(conn, rcept_no: str) -> None:
    """재실행 멱등성: 새로 채우기 전에 해당 공시의 기존 metrics를 지운다."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM metrics WHERE rcept_no = %s", (rcept_no,))


def insert_metrics(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO metrics
              (rcept_no, corp_code, bsns_year, reprt_code, concept, account_nm,
               statement_type, is_consolidated, period_qualifier, amount)
            VALUES (%(rcept_no)s, %(corp_code)s, %(bsns_year)s, %(reprt_code)s,
                    %(concept)s, %(account_nm)s, %(statement_type)s,
                    %(is_consolidated)s, %(period_qualifier)s, %(amount)s)
            """,
            rows,
        )
    return len(rows)


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


def load_metrics(conn, rcept_no: str) -> list[dict]:
    """한 공시의 metrics 전체 (수치형 diff 입력)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT concept, account_nm, statement_type, is_consolidated,
                   period_qualifier, amount
            FROM metrics WHERE rcept_no = %s
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
