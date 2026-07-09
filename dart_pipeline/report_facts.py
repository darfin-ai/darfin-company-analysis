"""DART 정기보고서 주요정보 API 원본 → overview/diff 소비 형태 변환.

순수 함수 (DB/네트워크 없음). metrics.py와 동일한 역할.
"""

from __future__ import annotations

from .diff import _norm
from .overview import _TOP_SHAREHOLDERS, _dividend_fiscal_year

_SKIP_LABELS = {"총계", "합계", "계", "기타"}
_COMMON_STOCK = {"보통주", "의결권 있는 주식"}

# DART 응답의 메타 필드(공시 식별용) — placeholder 판정 시 값 비교 대상에서 제외
_META_KEYS = frozenset({"rcept_no", "corp_cls", "corp_code", "corp_name", "stlm_dt", "rm"})
# "se"(구분): stockTotqySttus의 "합계"/"비고", mrhlSttus의 "소액주주"처럼 나머지
# 필드가 전부 대시여도 행 구분용 라벨은 항상 채워져 있다 — 실질 데이터가 아니므로
# placeholder 판정 시 값 비교 대상에서 제외한다.
_LABEL_KEYS = frozenset({"se"})
_PLACEHOLDER_VALUES = frozenset({None, "", "-", "－"})

_DIVIDEND_LABELS = {
    "주당현금배당금(원)": "perShareKrw",
    "현금배당수익률(%)": "yieldPct",
    "(연결)현금배당성향(%)": "payoutRatioPct",
}


def _parse_num(text: str | None) -> float | None:
    if not text or text.strip() in ("-", ""):
        return None
    cleaned = text.replace(",", "").replace("%", "").strip()
    try:
        if "." in cleaned:
            return float(cleaned)
        return float(int(cleaned))
    except ValueError:
        return None


def _parse_int(text: str | None) -> int | None:
    v = _parse_num(text)
    return int(v) if v is not None else None


def _norm_se(se: str) -> str:
    return _norm(se or "")


def dividend_panel(
    rows: list[dict] | None,
    *,
    bsns_year: str,
    reprt_code: str,
) -> dict | None:
    """alotMatter → overview.extract_dividend과 동일한 dict."""
    if not rows:
        return None

    values: dict[str, list[float]] = {}
    for row in rows:
        label_norm = _norm_se(row.get("se", ""))
        mapped = _DIVIDEND_LABELS.get(label_norm)
        if mapped is None:
            continue
        stock_knd = (row.get("stock_knd") or "").strip()
        if mapped == "perShareKrw" and stock_knd not in _COMMON_STOCK:
            continue
        if mapped in ("yieldPct", "payoutRatioPct"):
            if mapped == "yieldPct" and stock_knd not in _COMMON_STOCK:
                continue
            if mapped == "payoutRatioPct" and stock_knd not in ("-", ""):
                continue
        nums = [_parse_num(row.get(k)) for k in ("thstrm", "frmtrm", "lwfr")]
        nums = [n for n in nums if n is not None]
        if nums:
            values[mapped] = nums

    per_share = values.get("perShareKrw")
    if not per_share:
        return None

    yield_pct = values.get("yieldPct")
    payout = values.get("payoutRatioPct")
    is_interim = reprt_code != "11011"
    history = []
    for i, label in enumerate(("당기", "전기", "전전기")):
        amount = per_share[i] if i < len(per_share) else None
        fiscal_year, is_partial = _dividend_fiscal_year(bsns_year, reprt_code, i)
        history.append(
            {
                "fiscalYear": fiscal_year,
                "perShareKrw": amount,
                "isPartial": is_partial,
                "year": label,
            }
        )
    history.sort(key=lambda row: int(row["fiscalYear"]))
    return {
        "perShareKrw": per_share[0],
        "yieldPct": yield_pct[0] if yield_pct else 0.0,
        "payoutRatioPct": payout[0] if payout else 0.0,
        "isInterimReport": is_interim,
        "history": history,
    }


def shareholders_panel(rows: list[dict] | None) -> list[dict]:
    """hyslrSttus → overview.extract_shareholders와 동일한 list."""
    if not rows:
        return []

    out = []
    for row in rows:
        stock_knd = (row.get("stock_knd") or "").strip()
        if stock_knd not in _COMMON_STOCK:
            continue
        name = (row.get("nm") or "").strip()
        if not name or name.startswith("※") or _norm(name) in _SKIP_LABELS:
            continue
        share = _parse_num(row.get("trmend_posesn_stock_qota_rt"))
        if share is None:
            continue
        out.append(
            {
                "name": name,
                "detail": (row.get("relate") or "").strip(),
                "share": share,
            }
        )

    out.sort(key=lambda s: s["share"], reverse=True)
    return out[:_TOP_SHAREHOLDERS]


def headcount_metrics(
    emp_rows: list[dict] | None,
    exctv_rows: list[dict] | None,
) -> dict[str, float]:
    """empSttus + exctvSttus → diff.headcount_metrics와 동일한 dict."""
    out: dict[str, float] = {}

    for row in emp_rows or []:
        division = (row.get("fo_bbm") or "").strip()
        sex = (row.get("sexdstn") or "").strip()
        if not division:
            continue
        total = _parse_int(row.get("sm"))
        if total is None:
            continue
        label = f"{division} {sex}".strip()
        out[f"직원 수 ({_norm(label)})"] = float(total)

    unregistered = 0
    for row in exctv_rows or []:
        reg = (row.get("rgist_exctv_at") or "").strip()
        if "미등기" in reg:
            unregistered += 1
    if unregistered > 0:
        out["미등기임원 수"] = float(unregistered)

    return out


def ownership_metrics(
    major_rows: list[dict] | None,
    minority_rows: list[dict] | None,
) -> dict[str, float]:
    """hyslrSttus + mrhlSttus → diff.ownership_metrics와 동일한 dict."""
    out: dict[str, float] = {}

    for row in major_rows or []:
        stock_knd = (row.get("stock_knd") or "").strip()
        if stock_knd not in _COMMON_STOCK:
            continue
        name = (row.get("nm") or "").strip()
        if not name or name.startswith("※") or _norm(name) in _SKIP_LABELS:
            continue
        share = _parse_num(row.get("trmend_posesn_stock_qota_rt"))
        if share is not None:
            out[f"{_norm(name)} 지분율"] = share

    for row in minority_rows or []:
        rate = _parse_num(row.get("hold_stock_rate"))
        if rate is not None:
            out["소액주주 소유주식 비율"] = rate
            break

    return out


def is_placeholder_only(rows: list[dict] | None) -> bool:
    """DART가 status 000(성공)으로 응답했지만 실질 데이터가 없는 경우 판별.

    분기보고서엔 기재의무가 없는 항목(직원현황 등)은 013(무자료)이 아니라
    모든 필드가 "-"인 placeholder 행 1건으로 내려온다 — 013과 동일하게
    "데이터 없음"으로 취급해야 report_facts에 의미 없는 값이 캐시되지 않는다.
    """
    if not rows:
        return True
    for row in rows:
        for key, value in row.items():
            if key in _META_KEYS or key in _LABEL_KEYS:
                continue
            if value not in _PLACEHOLDER_VALUES:
                return False
    return True


# fetch_report_facts / ingest가 순회하는 10개 엔드포인트
REPORT_FACT_API_IDS: tuple[str, ...] = (
    "alotMatter",
    "hyslrSttus",
    "hyslrChgSttus",
    "mrhlSttus",
    "empSttus",
    "tesstkAcqsDspsSttus",
    "irdsSttus",
    "stockTotqySttus",
    "exctvSttus",
    "accnutAdtorNmNdAdtOpinion",
)
