"""섹션 → 12개 표준 섹션 라벨 매핑.

표준 라벨은 프론트엔드의 DIFF_SECTION_CONFIG
(darfin-front/src/app/features/company-analysis/lib/comparison.js)와 1:1.

매칭 규칙: AASSOCNOTE 코드 매칭을 우선(연도 간 안정적)하고,
코드가 없는 섹션(로마숫자 최상위 등)은 정규화된 제목 패턴으로 보완한다.
하나의 표준 라벨에 여러 섹션이 매핑될 수 있다(예: 연결/별도 재무상태표).
"""

from __future__ import annotations

import re

# (표준 라벨, AASSOCNOTE 집합, 제목 정규식) — 먼저 매칭되는 규칙이 이긴다.
# 제목은 normalize_title()을 거친 상태로 비교된다.
_RULES: list[tuple[str, frozenset[str], re.Pattern | None]] = [
    ("주석", frozenset({"D-0-3-3-0", "D-0-3-5-0"}), re.compile(r"재무제표 주석")),
    ("재무상태표", frozenset(), re.compile(r"재무상태표")),
    ("손익계산서", frozenset(), re.compile(r"^(?!.*포괄).*손익계산서")),
    ("현금흐름표", frozenset(), re.compile(r"현금흐름표")),
    ("위험요인", frozenset({"L-0-2-5-L1"}), re.compile(r"위험관리 및 파생거래")),
    ("중요한 계약", frozenset({"L-0-2-6-L1"}), re.compile(r"주요계약")),
    ("사업의 내용", frozenset({"D-0-2-0-0"}), re.compile(r"^II\. 사업의 내용")),
    ("회사의 개요", frozenset(), re.compile(r"^I\. 회사의 개요")),
    ("계열회사 현황", frozenset({"D-0-9-0-0"}), re.compile(r"계열회사 등에 관한 사항")),
    ("임원 및 직원", frozenset(), re.compile(r"임원 및 직원 등에 관한 사항")),
    ("주주현황", frozenset({"D-0-7-0-0"}), re.compile(r"주주에 관한 사항")),
    ("지배구조", frozenset(), re.compile(r"이사회 등 회사의 기관에 관한 사항")),
]

CANONICAL_LABELS: list[str] = [
    "회사의 개요", "사업의 내용", "위험요인", "재무상태표", "손익계산서",
    "현금흐름표", "주석", "계열회사 현황", "중요한 계약", "임원 및 직원",
    "주주현황", "지배구조",
]


def normalize_title(title: str) -> str:
    """제목 비교용 정규화: 전각 공백 제거, 연속 공백 축약."""
    return re.sub(r"\s+", " ", title.replace("　", " ")).strip()


def canonical_label(title: str, assoc_note: str | None) -> str | None:
    norm = normalize_title(title)
    for label, notes, pattern in _RULES:
        if assoc_note and assoc_note in notes:
            return label
        if pattern is not None and pattern.search(norm):
            return label
    return None
