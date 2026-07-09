"""dart_period unit tests."""

from datetime import date

from dart_pipeline.dart_period import (
    latest_periodic_from_list,
    list_filings_date_range,
    periodic_candidates_from_list,
)


def test_latest_periodic_picks_most_recent_by_rcept_dt():
    items = [
        {
            "rcept_no": "20250315000001",
            "report_nm": "사업보고서 (2024.12)",
            "rcept_dt": "20250315",
        },
        {
            "rcept_no": "20250514000001",
            "report_nm": "분기보고서 (2025.03)",
            "rcept_dt": "20250514",
        },
    ]
    period = latest_periodic_from_list(items)
    assert period is not None
    assert period["rcept_no"] == "20250514000001"
    assert period["bsns_year"] == "2025"
    assert period["reprt_code"] == "11013"


def test_latest_periodic_skips_non_periodic():
    items = [
        {
            "rcept_no": "20250601000001",
            "report_nm": "[기재정정]주요사항보고서(유상증자결정)",
            "rcept_dt": "20250601",
        },
        {
            "rcept_no": "20250514000001",
            "report_nm": "반기보고서 (2025.06)",
            "rcept_dt": "20250814",
        },
    ]
    period = latest_periodic_from_list(items)
    assert period is not None
    assert period["reprt_code"] == "11012"
    assert period["bsns_year"] == "2025"


def test_latest_periodic_empty_when_no_periodic_reports():
    assert latest_periodic_from_list([]) is None
    assert latest_periodic_from_list(
        [{"rcept_no": "1", "report_nm": "주요사항보고서", "rcept_dt": "20250101"}]
    ) is None


def test_list_filings_date_range_lookback():
    bgn, end = list_filings_date_range(today=date(2026, 7, 9))
    assert end == "20260709"
    assert bgn == "20250107"


def test_periodic_candidates_sorted_descending_and_deduped():
    items = [
        {
            "rcept_no": "20250315000001",
            "report_nm": "사업보고서 (2024.12)",
            "rcept_dt": "20250315",
        },
        {
            "rcept_no": "20250514000001",
            "report_nm": "분기보고서 (2025.03)",
            "rcept_dt": "20250514",
        },
        {
            "rcept_no": "20250814000001",
            "report_nm": "반기보고서 (2025.06)",
            "rcept_dt": "20250814",
        },
    ]
    candidates = periodic_candidates_from_list(items)
    assert [c["rcept_no"] for c in candidates] == [
        "20250814000001",
        "20250514000001",
        "20250315000001",
    ]


def test_periodic_candidates_keeps_latest_correction_per_period():
    items = [
        {
            "rcept_no": "20250514000001",
            "report_nm": "분기보고서 (2025.03)",
            "rcept_dt": "20250514",
        },
        {
            "rcept_no": "20250520000001",
            "report_nm": "[기재정정]분기보고서 (2025.03)",
            "rcept_dt": "20250520",
        },
    ]
    candidates = periodic_candidates_from_list(items)
    assert len(candidates) == 1
    assert candidates[0]["rcept_no"] == "20250520000001"


def test_periodic_candidates_empty_when_no_periodic_reports():
    assert periodic_candidates_from_list([]) == []
