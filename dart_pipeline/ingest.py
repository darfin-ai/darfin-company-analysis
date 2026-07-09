"""수집 오케스트레이션: 발견 → 다운로드 → 압축 해제 → filings 기록 (status=RAW).

멱등성: rcept_no가 이미 DB에 있고 파일도 있으면 건너뛴다.
재실행은 언제나 안전하다.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

from . import db
from .client import DartClient
from .config import RAW_DIR
from .corp_codes import load_corp_codes
from .report_classify import classify_report


@dataclass
class IngestResult:
    rcept_no: str
    report_nm: str
    reprt_code: str
    bsns_year: str
    action: str  # ingested / skipped_exists / skipped_not_periodic / dry_run / failed
    detail: str = ""


def _extract_main_xml(zip_bytes: bytes, rcept_no: str) -> tuple[str, bytes]:
    """문서 zip에서 본문 XML을 고른다: {rcept_no}.xml 우선, 없으면 최대 크기 .xml.

    (정정공시 zip에는 이전 버전 등 XML이 여러 개 들어올 수 있다)
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        xml_names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        if not xml_names:
            raise ValueError(f"zip에 XML 없음: {zf.namelist()}")
        preferred = f"{rcept_no}.xml"
        name = preferred if preferred in xml_names else max(xml_names, key=lambda n: zf.getinfo(n).file_size)
        return name, zf.read(name)


def ingest_company(
    client: DartClient,
    stock_code: str,
    bgn_de: str,
    end_de: str,
    dry_run: bool = False,
    force: bool = False,
) -> list[IngestResult]:
    """한 기업의 정기공시를 기간 범위로 수집한다."""
    book = load_corp_codes(client)
    corp = book.by_stock_code(stock_code)
    if corp is None:
        raise ValueError(f"종목코드 {stock_code}에 해당하는 기업 없음 (corpCode.xml 기준)")

    filings = client.list_filings(corp.corp_code, bgn_de, end_de)
    results: list[IngestResult] = []

    with db.connection() as conn:
        if not dry_run:
            db.ensure_company(conn, corp.corp_code, corp.corp_name, corp.stock_code)
        known = db.existing_rcept_nos(conn, corp.corp_code)

        for item in filings:
            rcept_no = item["rcept_no"]
            report_nm = item["report_nm"].strip()

            classified = classify_report(report_nm)
            if classified is None:
                results.append(IngestResult(rcept_no, report_nm, "", "", "skipped_not_periodic"))
                continue
            reprt_code, bsns_year = classified

            corp_dir = RAW_DIR / corp.corp_code
            zip_path = corp_dir / f"{rcept_no}.zip"
            xml_path = corp_dir / f"{rcept_no}.xml"

            if not force and rcept_no in known and xml_path.exists():
                results.append(IngestResult(rcept_no, report_nm, reprt_code, bsns_year, "skipped_exists"))
                continue

            if dry_run:
                results.append(IngestResult(rcept_no, report_nm, reprt_code, bsns_year, "dry_run"))
                continue

            try:
                zip_bytes = client.document_zip(rcept_no)
                corp_dir.mkdir(parents=True, exist_ok=True)
                zip_path.write_bytes(zip_bytes)
                inner_name, xml_bytes = _extract_main_xml(zip_bytes, rcept_no)
                xml_path.write_bytes(xml_bytes)

                db.insert_filing(
                    conn,
                    rcept_no=rcept_no,
                    corp_code=corp.corp_code,
                    corp_name=item.get("corp_name", corp.corp_name),
                    bsns_year=bsns_year,
                    reprt_code=reprt_code,
                    filed_date=item["rcept_dt"],
                    zip_path=str(zip_path),
                    xml_path=str(xml_path),
                )
                conn.commit()  # 공시 1건 = 커밋 1건: 중단돼도 완료분은 보존
                results.append(
                    IngestResult(rcept_no, report_nm, reprt_code, bsns_year, "ingested", inner_name)
                )
            except Exception as e:  # 한 건의 실패가 나머지 수집을 막지 않게
                results.append(IngestResult(rcept_no, report_nm, reprt_code, bsns_year, "failed", str(e)))

    return results
