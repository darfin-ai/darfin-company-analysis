"""외부 소스의 법인명을 서비스 표시명으로 정규화한다."""

from __future__ import annotations


_DISPLAY_NAME_BY_STOCK_CODE = {
    "000660": "SK하이닉스",
}


def canonical_company_name(stock_code: str | None, source_name: str) -> str:
    """종목코드별 공식 표시명이 있으면 우선하고, 아니면 원본 이름을 유지한다."""
    return _DISPLAY_NAME_BY_STOCK_CODE.get(stock_code, source_name)
