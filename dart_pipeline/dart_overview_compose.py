"""report_facts raw DART rows → frontend DartOverview (types.js)."""

from __future__ import annotations

import json
import re
from typing import Any

from .report_facts import REPORT_FACT_API_IDS
from .report_facts import _META_KEYS

# DART snake_case keys that generic conversion would mis-name vs types.js
_FIELD_ALIASES: dict[str, str] = {
    "bsis_posesn_stock_co": "bsisPosesnStockCo",
    "bsis_posesn_stock_qota_rt": "bsisQotaRt",
    "trmend_posesn_stock_co": "trmendPosesnStockCo",
    "trmend_posesn_stock_qota_rt": "trmendQotaRt",
    "fo_bbm": "foBbm",
    "rgllbr_co": "rgllbrCo",
    "cnttk_co": "cnttkCo",
    "avrg_cnwk_sdytrn": "avrgCnwkSdytrn",
    "fyer_salary_totamt": "fyerSalaryTotamt",
    "jan_salary_am": "janSalaryAm",
    "acqs_mth1": "acqsMth1",
    "acqs_mth2": "acqsMth2",
    "acqs_mth3": "acqsMth3",
    "stock_knd": "stockKnd",
    "bsis_qy": "bsisQy",
    "change_qy_acqs": "changeQyAcqs",
    "change_qy_dsps": "changeQyDsps",
    "change_qy_incnr": "changeQyIncnr",
    "trmend_qy": "trmendQy",
    "isu_dcrs_de": "isuDcrsDe",
    "isu_dcrs_stle": "isuDcrsStle",
    "isu_dcrs_stock_knd": "isuDcrsStockKnd",
    "isu_dcrs_qy": "isuDcrsQy",
    "isu_dcrs_mstvdiv_fval_amount": "isuDcrsMstvdivFvalAmount",
    "isu_dcrs_mstvdiv_amount": "isuDcrsMstvdivAmount",
    "isu_stock_totqy": "isuStockTotqy",
    "istc_totqy": "istcTotqy",
    "tesstk_co": "tesstkCo",
    "distb_stock_co": "distbStockCo",
    "birth_ym": "birthYm",
    "rgist_exctv_at": "rgistExctvAt",
    "fte_at": "fteAt",
    "chrg_job": "chrgJob",
    "main_career": "mainCareer",
    "hffc_pd": "hffcPd",
    "tenure_end_on": "tenureEndOn",
    "change_on": "changeOn",
    "mxmm_shrholdr_nm": "mxmmShrholdrNm",
    "posesn_stock_co": "posesnStockCo",
    "change_cause": "changeCause",
    "shrholdr_co": "shrholdrCo",
    "shrholdr_tot_co": "shrholdrTotCo",
    "shrholdr_rate": "shrholdrRate",
    "hold_stock_co": "holdStockCo",
    "stock_tot_co": "stockTotCo",
    "hold_stock_rate": "holdStockRate",
    "adt_opinion": "adtOpinion",
    "emphs_matter": "emphsMatter",
    "core_adt_matter": "coreAdtMatter",
    "bsns_year": "bsnsYear",
}

_NUMERIC_FIELDS = frozenset(
    {
        "thstrm",
        "frmtrm",
        "lwfr",
        "bsisPosesnStockCo",
        "bsisQotaRt",
        "trmendPosesnStockCo",
        "trmendQotaRt",
        "posesnStockCo",
        "qotaRt",
        "shrholdrCo",
        "shrholdrTotCo",
        "shrholdrRate",
        "holdStockCo",
        "stockTotCo",
        "holdStockRate",
        "rgllbrCo",
        "cnttkCo",
        "sm",
        "fyerSalaryTotamt",
        "janSalaryAm",
        "bsisQy",
        "changeQyAcqs",
        "changeQyDsps",
        "changeQyIncnr",
        "trmendQy",
        "isuDcrsQy",
        "isuDcrsMstvdivFvalAmount",
        "isuDcrsMstvdivAmount",
        "isuStockTotqy",
        "istcTotqy",
        "redc",
        "tesstkCo",
        "distbStockCo",
    }
)

_API_TO_SECTION: dict[str, str] = {
    "alotMatter": "dividends",
    "hyslrSttus": "majorShareholders",
    "hyslrChgSttus": "majorShareholderChanges",
    "mrhlSttus": "minorityShareholders",
    "empSttus": "employees",
    "tesstkAcqsDspsSttus": "treasuryStock",
    "irdsSttus": "capitalChanges",
    "stockTotqySttus": "stockTotals",
    "exctvSttus": "executives",
    "accnutAdtorNmNdAdtOpinion": "auditOpinions",
}

_SECTION_LABELS: dict[str, str] = {
    "dividends": "배당에 관한 사항",
    "majorShareholders": "최대주주 및 특수관계인의 주식소유 현황",
    "majorShareholderChanges": "최대주주 변동현황",
    "minorityShareholders": "소액주주 현황",
    "employees": "직원 등의 현황",
    "treasuryStock": "자기주식 취득 및 처분 현황",
    "capitalChanges": "증자(감자) 현황",
    "stockTotals": "주식의 총수 등",
    "executives": "임원 현황",
    "auditOpinions": "회계감사인의 명칭 및 감사의견",
}


def _snake_to_camel(key: str) -> str:
    if key in _FIELD_ALIASES:
        return _FIELD_ALIASES[key]
    parts = key.split("_")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def _parse_num(text: str | None) -> float | int | None:
    if text is None:
        return None
    s = str(text).strip()
    if s in ("", "-", "－"):
        return None
    cleaned = s.replace(",", "").replace("%", "").strip()
    try:
        if "." in cleaned:
            return float(cleaned)
        return int(cleaned)
    except ValueError:
        return None


def _normalize_date(text: str | None) -> str | None:
    if not text or str(text).strip() in ("", "-"):
        return None
    s = str(text).strip()
    m = re.match(r"(\d{4})[.\-년/\s]+(\d{1,2})[.\-월/\s]+(\d{1,2})", s)
    if m:
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        return f"{y}-{mo:02d}-{d:02d}"
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    return s


def _normalize_stock_knd(value: str | None) -> str | None:
    if value is None:
        return None
    s = value.strip()
    if s in ("", "-", "－"):
        return None
    return s


def _map_row(row: dict, *, api_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key in _META_KEYS:
            continue
        camel = _snake_to_camel(key)
        if camel == "stockKnd":
            out[camel] = _normalize_stock_knd(value if isinstance(value, str) else str(value or ""))
        elif camel in _NUMERIC_FIELDS:
            out[camel] = _parse_num(value if value is None else str(value))
        elif camel in ("changeOn", "isuDcrsDe", "tenureEndOn"):
            out[camel] = _normalize_date(value if value is None else str(value))
        elif camel == "bsnsYear" and api_id == "accnutAdtorNmNdAdtOpinion":
            out[camel] = str(value).strip() if value is not None else ""
        else:
            out[camel] = "" if value is None else str(value).strip()

    if api_id == "alotMatter":
        se = out.get("se") or ""
        if "백만원" in se:
            for term in ("thstrm", "frmtrm", "lwfr"):
                v = out.get(term)
                if isinstance(v, (int, float)):
                    out[term] = int(v * 1_000_000)

    return out


def _dedupe_mapped_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop byte-identical mapped dicts, preserving first-occurrence order."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = json.dumps(row, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _section(
    rows: list[dict] | None,
    section_key: str,
    rcept_no: str | None,
    as_of: dict | None = None,
) -> dict | None:
    if not rows:
        return None
    mapped = [_map_row(r, api_id=_section_key_to_api(section_key)) for r in rows]
    if section_key == "auditOpinions":
        mapped = _dedupe_mapped_rows(mapped)
    label = _SECTION_LABELS[section_key]
    source_rcept_no = as_of["rceptNo"] if as_of else rcept_no
    source_ref = None
    if source_rcept_no:
        source_ref = {
            "sectionLabel": label,
            "excerpt": f"{label} (DART 정기공시 API)",
            "sourceRef": source_rcept_no,
        }
    section: dict[str, Any] = {"rows": mapped, "sourceRef": source_ref}
    if as_of is not None:
        section["asOf"] = as_of
    return section


def _section_key_to_api(section_key: str) -> str:
    for api_id, key in _API_TO_SECTION.items():
        if key == section_key:
            return api_id
    raise KeyError(section_key)


def compose_dart_overview(
    *,
    bsns_year: str,
    reprt_code: str,
    rcept_no: str | None,
    payloads: dict[str, list[dict] | None],
    fallback_info: dict[str, dict] | None = None,
) -> dict:
    """Build DartOverview dict from api_id → payload (None = 013).

    fallback_info: 현재 기간에 데이터가 없어 과거 정기공시에서 채운 api_id →
    {bsnsYear, reprtCode, rceptNo} — 해당 section에 asOf로 표시된다.
    """
    fallback_info = fallback_info or {}
    overview: dict[str, Any] = {
        "meta": {
            "bsnsYear": bsns_year,
            "reprtCode": reprt_code,
            "rceptNo": rcept_no or "",
        }
    }
    for api_id in REPORT_FACT_API_IDS:
        section_key = _API_TO_SECTION[api_id]
        raw = payloads.get(api_id)
        overview[section_key] = _section(
            raw, section_key, rcept_no, as_of=fallback_info.get(api_id)
        )
    return overview
