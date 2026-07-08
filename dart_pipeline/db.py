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


def filings_missing_metrics(conn, corp_code: str, force: bool = False) -> list[dict]:
    """metrics가 아직 없는 해당 기업의 filings (rcept_no/bsns_year/reprt_code).

    force=True면 이미 metrics가 있는 filings도 포함한다 — 스키마에 컬럼이
    추가되는 등 전면 재적재가 필요할 때 사용(적재는 filing 단위
    delete→insert라 멱등).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT f.rcept_no, f.bsns_year, f.reprt_code
            FROM filings f
            LEFT JOIN metrics m ON m.rcept_no = f.rcept_no
            WHERE f.corp_code = %s AND f.pipeline_status != 'FAILED'
            """
            + ("" if force else " AND m.id IS NULL"),
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
               statement_type, ord, is_consolidated, period_qualifier, amount)
            VALUES (%(rcept_no)s, %(corp_code)s, %(bsns_year)s, %(reprt_code)s,
                    %(concept)s, %(account_nm)s, %(statement_type)s, %(ord)s,
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
            "SELECT id, corp_code FROM llm_jobs "
            "WHERE status = 'pending' "
            "   OR (status = 'running' AND started_at < NOW() - INTERVAL 15 MINUTE) "
            "ORDER BY requested_at ASC LIMIT 1 FOR UPDATE"
        )
        row = cur.fetchone()
        if row is None:
            return None
        job_id, corp_code = row
        cur.execute(
            "UPDATE llm_jobs SET status = 'running', started_at = CURRENT_TIMESTAMP WHERE id = %s",
            (job_id,),
        )
    return {"id": job_id, "corp_code": corp_code}


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
