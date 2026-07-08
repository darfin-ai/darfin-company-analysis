"""fnlttSinglAcntAll.json 응답 → metrics 테이블 행 변환.

순수 변환 로직 (네트워크/DB 없음). dart_parser와 달리 XML을 다루지 않는다 —
이 API가 이미 IFRS concept과 당기 금액을 구조화해서 주기 때문에 XML 재무제표
테이블 파싱은 이 값들에 한해서는 필요 없다 (IMPLEMENTATION_PLAN.md §5 순서 2).
"""

from __future__ import annotations

_STATEMENT_LABELS = {
    "BS": "재무상태표",
    "IS": "손익계산서",
    "CIS": "손익계산서",  # 포괄손익계산서 — diff 단계는 손익/포괄손익을 구분하지 않음
    "CF": "현금흐름표",
}
# 자본변동표(SCE) 등은 12개 표준 섹션에 대응이 없어 저장 대상에서 제외한다.

_NO_STANDARD_CODE = "-표준계정코드 미사용-"


def _parse_ord(text: str | None) -> int | None:
    """재무제표 내 계정 나열 순서. 공시 원문 표 순서 그대로라 정렬 축으로 쓴다."""
    try:
        return int(text) if text else None
    except ValueError:
        return None


def _parse_amount(text: str | None) -> int | None:
    if not text:
        return None
    cleaned = text.replace(",", "").strip()
    if not cleaned or cleaned == "-":
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def transform(
    raw_rows: list[dict],
    *,
    rcept_no: str,
    corp_code: str,
    bsns_year: str,
    reprt_code: str,
    is_consolidated: bool,
) -> list[dict]:
    """fnlttSinglAcntAll의 list 응답을 metrics 테이블 삽입용 딕셔너리 목록으로 변환.

    분기/반기 보고서의 손익·현금흐름 항목은 당기 실적(thstrm_amount, 예: 3개월)과
    누적 실적(thstrm_add_amount)을 함께 주므로 두 행으로 분리해 저장한다.
    """
    rows: list[dict] = []
    for item in raw_rows:
        statement_type = _STATEMENT_LABELS.get(item.get("sj_div"))
        if statement_type is None:
            continue

        account_nm = (item.get("account_nm") or "").strip()
        if not account_nm:
            continue

        account_id = item.get("account_id")
        concept = None if not account_id or account_id == _NO_STANDARD_CODE else account_id

        base = {
            "rcept_no": rcept_no,
            "corp_code": corp_code,
            "bsns_year": bsns_year,
            "reprt_code": reprt_code,
            "concept": concept,
            "account_nm": account_nm,
            "statement_type": statement_type,
            "ord": _parse_ord(item.get("ord")),
            "is_consolidated": is_consolidated,
        }

        current = _parse_amount(item.get("thstrm_amount"))
        cumulative = _parse_amount(item.get("thstrm_add_amount"))

        if cumulative is not None:
            if current is not None:
                rows.append({**base, "period_qualifier": "3개월", "amount": current})
            rows.append({**base, "period_qualifier": "누적", "amount": cumulative})
        elif current is not None:
            rows.append({**base, "period_qualifier": None, "amount": current})

    return rows
