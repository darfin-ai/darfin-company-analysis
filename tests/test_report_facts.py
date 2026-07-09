"""report_facts transform unit tests (real DART API fixtures)."""

from __future__ import annotations

import json
from pathlib import Path

from dart_pipeline.report_facts import (
    dividend_panel,
    headcount_metrics,
    is_placeholder_only,
    ownership_metrics,
    shareholders_panel,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text())


def test_samsung_dividend_panel_matches_api_shape():
    rows = _load("samsung_alotMatter_2024.json")
    panel = dividend_panel(rows, bsns_year="2024", reprt_code="11011")
    assert panel is not None
    assert panel["perShareKrw"] == 1446.0
    assert panel["yieldPct"] == 2.7
    assert panel["payoutRatioPct"] == 29.2
    assert panel["isInterimReport"] is False
    assert len(panel["history"]) == 3
    assert panel["history"][-1]["fiscalYear"] == "2024"


def test_samsung_shareholders_common_only():
    rows = _load("samsung_hyslrSttus_2024.json")
    holders = shareholders_panel(rows)
    assert len(holders) > 0
    assert holders[0]["name"] == "삼성생명보험㈜"
    assert holders[0]["share"] == 8.51
    # 우선주-only 행(홍라희 0.03%)은 보통주 행(1.64%)보다 작아 상위에 안 들어옴
    assert all(h["share"] >= 1.0 for h in holders[:3])


def test_samsung_headcount_from_emp_and_exctv():
    emp = _load("samsung_empSttus_2024.json")
    exctv = _load("samsung_exctvSttus_2024.json")
    metrics = headcount_metrics(emp, exctv)
    assert any(k.startswith("직원 수 (") for k in metrics)
    assert metrics["직원 수 (DX남)"] == 38291.0


def test_samsung_ownership_major_and_minority():
    major = _load("samsung_hyslrSttus_2024.json")
    minority = _load("samsung_mrhlSttus_2024.json")
    metrics = ownership_metrics(major, minority)
    assert metrics["삼성생명보험㈜ 지분율"] == 8.51
    assert metrics["소액주주 소유주식 비율"] == 68.23


def test_dividend_panel_empty_on_013():
    assert dividend_panel(None, bsns_year="2024", reprt_code="11011") is None
    assert shareholders_panel(None) == []


def test_sk_hynix_shareholders_not_empty():
    rows = _load("skhynix_hyslrSttus_2024.json")
    holders = shareholders_panel(rows)
    assert len(holders) >= 1


def test_is_placeholder_only_none_and_empty():
    assert is_placeholder_only(None) is True
    assert is_placeholder_only([]) is True


def test_is_placeholder_only_all_dash_row():
    rows = [
        {
            "rcept_no": "20260515002181",
            "corp_code": "00126380",
            "corp_name": "삼성전자",
            "fo_bbm": "-",
            "rgllbr_co": "-",
            "sm": "-",
            "stlm_dt": "2026-03-31",
            "rm": "-",
        }
    ]
    assert is_placeholder_only(rows) is True


def test_is_placeholder_only_false_for_real_data():
    rows = [
        {
            "rcept_no": "20260515002181",
            "corp_code": "00126380",
            "corp_name": "삼성전자",
            "fo_bbm": "본사",
            "rgllbr_co": "1000",
            "sm": "1000",
        }
    ]
    assert is_placeholder_only(rows) is False


def test_is_placeholder_only_mixed_rows():
    rows = [
        {"se": "합계", "isu_stock_totqy": "-"},
        {"se": "보통주", "isu_stock_totqy": "1000000"},
    ]
    assert is_placeholder_only(rows) is False


def test_is_placeholder_only_ignores_se_category_label():
    """stockTotqySttus/mrhlSttus의 "se" 구분 라벨은 데이터가 없어도 항상 채워진다."""
    rows = [
        {"se": "합계", "isu_stock_totqy": "-", "istc_totqy": "-"},
        {"se": "비고", "isu_stock_totqy": "-", "istc_totqy": "-"},
    ]
    assert is_placeholder_only(rows) is True
