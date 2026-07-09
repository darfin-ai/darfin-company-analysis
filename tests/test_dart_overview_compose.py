"""dart_overview_compose unit tests."""

from __future__ import annotations

import json
from pathlib import Path

from dart_pipeline.dart_overview_compose import _dedupe_mapped_rows, compose_dart_overview

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text())


def test_dedupe_mapped_rows_drops_duplicates_preserves_order():
    rows = [
        {"bsnsYear": "2026", "adtOpinion": ""},
        {"bsnsYear": "2026", "adtOpinion": ""},
        {"bsnsYear": "2025", "adtOpinion": "적정의견"},
        {"bsnsYear": "2024", "adtOpinion": "적정의견"},
        {"bsnsYear": "2024", "adtOpinion": "적정의견"},
    ]
    assert _dedupe_mapped_rows(rows) == [
        {"bsnsYear": "2026", "adtOpinion": ""},
        {"bsnsYear": "2025", "adtOpinion": "적정의견"},
        {"bsnsYear": "2024", "adtOpinion": "적정의견"},
    ]


def test_dedupe_mapped_rows_unchanged_when_no_duplicates():
    rows = [
        {"bsnsYear": "2026", "adtOpinion": ""},
        {"bsnsYear": "2025", "adtOpinion": "적정의견"},
    ]
    assert _dedupe_mapped_rows(rows) == rows


def test_compose_audit_opinions_dedupes_identical_rows():
    duplicate_rows = [
        {
            "rcept_no": "20250515001234",
            "bsns_year": "2026",
            "corp_cls": "Y",
            "corp_code": "00126380",
            "corp_name": "삼성전자",
            "stock_code": "005930",
            "stlm_dt": "2026.03.31",
            "adt_opinion": "",
            "adt_reprt_spcmnt_matter": "",
            "adt_reprt_spcmnt_matter_etc": "",
            "adt_reprt_spcmnt_matter_etc_yn": "",
            "adt_reprt_spcmnt_matter_yn": "",
            "adtor": "삼정회계법인",
            "adtor_nm": "김감사",
            "adtor_spcmnt_matter": "",
            "adtor_spcmnt_matter_etc": "",
            "adtor_spcmnt_matter_etc_yn": "",
            "adtor_spcmnt_matter_yn": "",
            "reprt_code": "11013",
        },
        {
            "rcept_no": "20250515001234",
            "bsns_year": "2026",
            "corp_cls": "Y",
            "corp_code": "00126380",
            "corp_name": "삼성전자",
            "stock_code": "005930",
            "stlm_dt": "2026.03.31",
            "adt_opinion": "",
            "adt_reprt_spcmnt_matter": "",
            "adt_reprt_spcmnt_matter_etc": "",
            "adt_reprt_spcmnt_matter_etc_yn": "",
            "adt_reprt_spcmnt_matter_yn": "",
            "adtor": "삼정회계법인",
            "adtor_nm": "김감사",
            "adtor_spcmnt_matter": "",
            "adtor_spcmnt_matter_etc": "",
            "adtor_spcmnt_matter_etc_yn": "",
            "adtor_spcmnt_matter_yn": "",
            "reprt_code": "11013",
        },
        {
            "rcept_no": "20250311001085",
            "bsns_year": "2025",
            "corp_cls": "Y",
            "corp_code": "00126380",
            "corp_name": "삼성전자",
            "stock_code": "005930",
            "stlm_dt": "2024.12.31",
            "adt_opinion": "적정의견",
            "adt_reprt_spcmnt_matter": "",
            "adt_reprt_spcmnt_matter_etc": "",
            "adt_reprt_spcmnt_matter_etc_yn": "",
            "adt_reprt_spcmnt_matter_yn": "",
            "adtor": "삼정회계법인",
            "adtor_nm": "김감사",
            "adtor_spcmnt_matter": "",
            "adtor_spcmnt_matter_etc": "",
            "adtor_spcmnt_matter_etc_yn": "",
            "adtor_spcmnt_matter_yn": "",
            "reprt_code": "11011",
        },
        {
            "rcept_no": "20250311001085",
            "bsns_year": "2025",
            "corp_cls": "Y",
            "corp_code": "00126380",
            "corp_name": "삼성전자",
            "stock_code": "005930",
            "stlm_dt": "2024.12.31",
            "adt_opinion": "적정의견",
            "adt_reprt_spcmnt_matter": "",
            "adt_reprt_spcmnt_matter_etc": "",
            "adt_reprt_spcmnt_matter_etc_yn": "",
            "adt_reprt_spcmnt_matter_yn": "",
            "adtor": "삼정회계법인",
            "adtor_nm": "김감사",
            "adtor_spcmnt_matter": "",
            "adtor_spcmnt_matter_etc": "",
            "adtor_spcmnt_matter_etc_yn": "",
            "adtor_spcmnt_matter_yn": "",
            "reprt_code": "11011",
        },
    ]
    payloads = {api: None for api in [
        "alotMatter", "hyslrSttus", "hyslrChgSttus", "mrhlSttus", "empSttus",
        "tesstkAcqsDspsSttus", "irdsSttus", "stockTotqySttus", "exctvSttus",
        "accnutAdtorNmNdAdtOpinion",
    ]}
    payloads["accnutAdtorNmNdAdtOpinion"] = duplicate_rows
    overview = compose_dart_overview(
        bsns_year="2026",
        reprt_code="11013",
        rcept_no="20250515001234",
        payloads=payloads,
    )
    opinions = overview["auditOpinions"]["rows"]
    assert len(opinions) == 2
    assert opinions[0]["bsnsYear"] == "2026"
    assert opinions[1]["bsnsYear"] == "2025"
    assert opinions[1]["adtOpinion"] == "적정의견"


def test_compose_samsung_dividends_scaled():
    payloads = {
        "alotMatter": _load("samsung_alotMatter_2024.json"),
        "hyslrSttus": None,
        "hyslrChgSttus": None,
        "mrhlSttus": None,
        "empSttus": None,
        "tesstkAcqsDspsSttus": None,
        "irdsSttus": None,
        "stockTotqySttus": None,
        "exctvSttus": None,
        "accnutAdtorNmNdAdtOpinion": None,
    }
    overview = compose_dart_overview(
        bsns_year="2024",
        reprt_code="11011",
        rcept_no="20250311001085",
        payloads=payloads,
    )
    assert overview["meta"]["bsnsYear"] == "2024"
    assert overview["meta"]["reprtCode"] == "11011"
    div = overview["dividends"]
    assert div is not None
    row = next(r for r in div["rows"] if "(연결)당기순이익" in r["se"])
    assert row["thstrm"] == 33_621_363_000_000
    per_share = next(r for r in div["rows"] if r["se"] == "주당 현금배당금(원)" and r["stockKnd"] == "보통주")
    assert per_share["thstrm"] == 1446


def test_compose_major_shareholders_camel_case():
    payloads = {
        "alotMatter": None,
        "hyslrSttus": _load("samsung_hyslrSttus_2024.json"),
        "hyslrChgSttus": None,
        "mrhlSttus": None,
        "empSttus": None,
        "tesstkAcqsDspsSttus": None,
        "irdsSttus": None,
        "stockTotqySttus": None,
        "exctvSttus": None,
        "accnutAdtorNmNdAdtOpinion": None,
    }
    overview = compose_dart_overview(
        bsns_year="2024",
        reprt_code="11011",
        rcept_no="20250311001085",
        payloads=payloads,
    )
    holders = overview["majorShareholders"]["rows"]
    life = next(r for r in holders if "삼성생명" in r["nm"])
    assert life["trmendQotaRt"] == 8.51
    assert "trmend_posesn_stock_qota_rt" not in life


def test_compose_employees_and_executives():
    payloads = {
        "alotMatter": None,
        "hyslrSttus": None,
        "hyslrChgSttus": None,
        "mrhlSttus": None,
        "empSttus": _load("samsung_empSttus_2024.json"),
        "tesstkAcqsDspsSttus": None,
        "irdsSttus": None,
        "stockTotqySttus": None,
        "exctvSttus": _load("samsung_exctvSttus_2024.json"),
        "accnutAdtorNmNdAdtOpinion": None,
    }
    overview = compose_dart_overview(
        bsns_year="2024",
        reprt_code="11011",
        rcept_no="20250311001085",
        payloads=payloads,
    )
    emp = next(r for r in overview["employees"]["rows"] if r["foBbm"] == "DX" and r["sexdstn"] == "남")
    assert emp["sm"] == 38291
    assert emp["foBbm"] == "DX"
    exctv = overview["executives"]["rows"][0]
    assert exctv["nm"] == "한종희"
    assert exctv["tenureEndOn"] == "2026-03-17"


def test_compose_null_section_on_013():
    overview = compose_dart_overview(
        bsns_year="2024",
        reprt_code="11011",
        rcept_no=None,
        payloads={api: None for api in [
            "alotMatter", "hyslrSttus", "hyslrChgSttus", "mrhlSttus", "empSttus",
            "tesstkAcqsDspsSttus", "irdsSttus", "stockTotqySttus", "exctvSttus",
            "accnutAdtorNmNdAdtOpinion",
        ]},
    )
    assert overview["dividends"] is None
    assert overview["employees"] is None
