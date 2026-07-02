"""파서 산출물 데이터 모델.

프론트엔드 계약(darfin-front/src/mocks/companyAnalysis/types.js)과
DB 스키마(darfin-main/ddl.sql §7 text_chunks/metrics)의 중간 형태.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Cell:
    """테이블 셀 하나. TE/TU/TD/TH 공통.

    acode/adecimal이 있는 수치 셀은 value에 실제 원화 스케일로 환산된 값을 갖는다.
    (원문 텍스트 "306,220,075" + ADECIMAL="-6" → value = 306_220_075_000_000)
    """

    text: str
    tag: str  # TE / TU / TD / TH
    acode: str | None = None  # IFRS concept, 예: ifrs-full_CurrentAssets
    acontext: str | None = None  # 기간 컨텍스트, 예: CFY2026eFQA_... (C=당기, P=전기)
    adecimal: int | None = None
    anegated: bool = False
    value: int | float | None = None  # 스케일 환산 완료된 값


@dataclass
class Table:
    rows: list[list[Cell]] = field(default_factory=list)
    aclass: str | None = None  # 예: EXTRACTION / NORMAL


@dataclass
class Section:
    """공시 문서의 한 섹션 (TITLE ATOC="Y" 단위).

    anchor 우선순위: assoc_note(연도 간 안정) > breadcrumb(항상 존재).
    atocid는 2023년 파일에는 없으므로 단독 앵커로 쓰면 안 된다.
    """

    title: str
    level: int  # SECTION-1 → 1, SECTION-2 → 2, ...
    order: int  # 문서 내 등장 순서 (0부터)
    assoc_note: str | None  # AASSOCNOTE, 예: D-0-2-0-0
    atocid: str | None
    breadcrumb: tuple[str, ...]  # 조상 제목 + 자기 제목
    canonical: str | None  # 12개 표준 섹션 라벨 중 하나 (canonical.py)
    paragraphs: list[str] = field(default_factory=list)  # 테이블 밖 서술 텍스트
    tables: list[Table] = field(default_factory=list)
    content_hash: str = ""  # sha256, diff 단계의 "변경 없음" 판정용

    @property
    def narrative(self) -> str:
        return "\n".join(self.paragraphs)


@dataclass
class NumericFact:
    """수치 셀 하나를 평탄화한 것 → metrics 테이블의 원천.

    두 가지 출처가 있다:
      - ACODE 셀 (2025~): concept에 IFRS 코드가 있고 context로 당기/전기 판별
      - 라벨 기반 추출 (2023~2024 재무제표): concept=None, row_label과
        열 머리글(col_label)의 제N기 번호로 당기/전기 판별
    """

    concept: str | None  # ACODE, 라벨 기반 추출이면 None
    context: str | None  # ACONTEXT 원문
    is_current_period: bool | None  # 당기=True, 전기=False, 판별불가=None
    value: int | float
    raw_text: str
    row_label: str  # 같은 행의 라벨 셀 텍스트, 예: "유동자산" (주석 참조 제거됨)
    col_label: str | None  # 라벨 기반 추출의 열 머리글, 예: "제 56 기 1분기 누적"
    section_title: str
    section_breadcrumb: tuple[str, ...]
    section_canonical: str | None


@dataclass
class ParsedFiling:
    source_path: str
    company_name: str | None
    doc_name: str | None  # 예: 분기보고서
    doc_acode: str | None  # 예: 11013
    period_from: str | None  # YYYYMMDD
    period_to: str | None
    sections: list[Section] = field(default_factory=list)
    facts: list[NumericFact] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def sections_by_canonical(self, label: str) -> list[Section]:
        return [s for s in self.sections if s.canonical == label]

    def find_by_assoc_note(self, note: str) -> Section | None:
        return next((s for s in self.sections if s.assoc_note == note), None)
