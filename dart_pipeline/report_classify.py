"""정기공시 report_nm → (reprt_code, bsns_year). ingest·dartOverview 공용."""

from __future__ import annotations

import re

# "분기보고서 (2026.03)" / "[기재정정]사업보고서 (2025.12)" → (종류, 연도, 월)
_REPORT_NM = re.compile(r"(사업|반기|분기)보고서\s*\((\d{4})\.(\d{2})\)")

# (종류, 월) → reprt_code. 분기보고서만 월로 1/3분기를 구분한다.
_REPRT_CODES = {
    ("사업", None): "11011",
    ("반기", None): "11012",
    ("분기", "03"): "11013",
    ("분기", "09"): "11014",
}


def classify_report(report_nm: str) -> tuple[str, str] | None:
    """report_nm → (reprt_code, bsns_year). 정기보고서 4종이 아니면 None."""
    m = _REPORT_NM.search(report_nm)
    if not m:
        return None
    kind, year, month = m.groups()
    code = _REPRT_CODES.get((kind, month if kind == "분기" else None))
    return (code, year) if code else None
