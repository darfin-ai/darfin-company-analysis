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
