"""섹션 트리 추출 — 파서의 본체.

문서 구조 (dart4.xsd, 삼성전자 2023~2026 픽스처로 검증):

    DOCUMENT
      DOCUMENT-NAME / COMPANY-NAME / SUMMARY
      BODY
        COVER                         ← 표지 (PERIODFROM/PERIODTO 추출)
        SECTION-1                     ← 최상위 섹션 (I. 회사의 개요, ...)
          TITLE ATOC="Y" [AASSOCNOTE] [ATOCID]
          P / TABLE-GROUP / TABLE ...
          TITLE ...                   ← 같은 컨테이너 안에 TITLE이 여러 개!
          SECTION-2 ...

핵심 주의점 4가지:
  - 섹션 분할의 실제 단위는 SECTION 컨테이너가 아니라 TITLE이다.
    하나의 컨테이너에 TITLE이 여러 개 올 수 있고, 두 번째 이후의 TITLE은
    "가상 하위 섹션"(레벨+1)으로 취급한다.
  - LIBRARY는 투명한 래퍼다. "II. 사업의 내용"의 하위 섹션들과
    "III. 재무에 관한 사항"의 재무제표들은 SECTION-1 > LIBRARY > SECTION-2
    아래에 있다 — LIBRARY를 뚫고 들어가지 않으면 통째로 놓친다.
  - 재무제표 각각("2-1. 연결 재무상태표" 등)은 TITLE을 첫 자식으로 갖는
    TABLE-GROUP이다. 이런 TABLE-GROUP은 TITLE + 내용으로 풀어서
    가상 섹션 경계로 취급한다.
  - ATOCID는 2023년 파일에 없다. 연도 간 섹션 매칭은 AASSOCNOTE,
    그것도 없으면 정규화된 breadcrumb으로 한다.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from lxml import etree

from .canonical import canonical_label, normalize_title
from .loader import load_document
from .models import NumericFact, ParsedFiling, Section
from .tables import (
    clean_row_label,
    label_facts,
    parse_table,
    row_label,
    statement_caption,
    unit_scale,
)

_SECTION_TAGS = {"SECTION-1", "SECTION-2", "SECTION-3"}
_TABLE_TAGS = {"TABLE-GROUP", "TABLE"}


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("　", " ")).strip()


def _paragraphs_of(elements: list[etree._Element]) -> list[str]:
    """서술 텍스트 수집. 테이블/하위섹션 서브트리는 제외."""
    parts = []
    for el in elements:
        if el.tag in _SECTION_TAGS or el.tag in _TABLE_TAGS:
            continue
        text = _norm_text(" ".join(el.itertext()))
        if text:
            parts.append(text)
    return parts


def _tables_of(elements: list[etree._Element]) -> list[etree._Element]:
    """TABLE 엘리먼트 수집. 하위섹션 서브트리는 제외."""
    found = []
    for el in elements:
        if el.tag in _SECTION_TAGS:
            continue
        if el.tag == "TABLE":
            found.append(el)
        else:
            found.extend(el.iter("TABLE"))
    return found


def _content_hash(section: Section) -> str:
    h = hashlib.sha256()
    h.update(section.narrative.encode("utf-8"))
    for table in section.tables:
        for row in table.rows:
            h.update(("|".join(c.text for c in row)).encode("utf-8"))
    return h.hexdigest()


def _content_stream(container: etree._Element) -> list[etree._Element]:
    """컨테이너의 자식을 섹션 분할용 아이템 목록으로 평탄화한다.

    - LIBRARY: 투명 — 자식들을 그 자리에 인라인.
    - TITLE을 첫 자식으로 갖는 TABLE-GROUP: TITLE과 나머지 내용으로 언랩
      (재무제표가 이 형태). 그 TITLE이 새 가상 섹션의 경계가 된다.
    """
    items: list[etree._Element] = []
    for c in container:
        if not isinstance(c.tag, str):
            continue
        if c.tag == "LIBRARY":
            items.extend(_content_stream(c))
        elif c.tag == "TABLE-GROUP":
            kids = [k for k in c if isinstance(k.tag, str)]
            if kids and kids[0].tag == "TITLE":
                items.extend(kids)
            else:
                items.append(c)
        else:
            items.append(c)
    return items


class _Walker:
    def __init__(self) -> None:
        self.sections: list[Section] = []

    def process_container(
        self,
        container: etree._Element,
        breadcrumb: tuple[str, ...],
        inherited_canonical: str | None,
    ) -> None:
        """SECTION-n 컨테이너 하나를 TITLE 단위 세그먼트로 쪼개 섹션을 만든다."""
        level = int(container.tag[-1])
        children = _content_stream(container)
        title_idxs = [i for i, c in enumerate(children) if c.tag == "TITLE"]

        if not title_idxs:  # 제목 없는 컨테이너 (표지 등) — 통째로 익명 섹션
            self._emit(None, children, level, breadcrumb, inherited_canonical)
            return

        bounds = title_idxs + [len(children)]

        # 첫 TITLE = 컨테이너 자신의 섹션. 첫 TITLE 앞의 내용도 여기에 귀속.
        head_content = children[: title_idxs[0]] + children[title_idxs[0] + 1 : bounds[1]]
        head_crumb, head_canonical = self._emit(
            children[title_idxs[0]], head_content, level, breadcrumb, inherited_canonical
        )

        # 이후 TITLE들 = 가상 하위 섹션 (레벨+1, 첫 섹션의 자식)
        for k in range(1, len(title_idxs)):
            content = children[title_idxs[k] + 1 : bounds[k + 1]]
            self._emit(children[title_idxs[k]], content, level + 1, head_crumb, head_canonical)

    def _emit(
        self,
        title_el: etree._Element | None,
        content: list[etree._Element],
        level: int,
        breadcrumb: tuple[str, ...],
        inherited_canonical: str | None,
    ) -> tuple[tuple[str, ...], str | None]:
        title = normalize_title(" ".join(title_el.itertext())) if title_el is not None else ""
        assoc_note = title_el.get("AASSOCNOTE") if title_el is not None else None
        atocid = title_el.get("ATOCID") if title_el is not None else None
        crumb = breadcrumb + (title,) if title else breadcrumb

        # 자기 규칙 매칭 우선, 없으면 조상 라벨 상속.
        # 단, 주석 내부는 항상 주석 (주석 안의 "현금흐름표" 관련 노트가
        # 재무제표 본문으로 오인되는 것을 방지).
        if inherited_canonical == "주석":
            canonical = "주석"
        else:
            canonical = canonical_label(title, assoc_note) or inherited_canonical

        section = Section(
            title=title,
            level=level,
            order=len(self.sections),
            assoc_note=assoc_note,
            atocid=atocid,
            breadcrumb=crumb,
            canonical=canonical,
            paragraphs=_paragraphs_of(content),
        )
        section.tables = [parse_table(t) for t in _tables_of(content)]
        section.content_hash = _content_hash(section)
        self.sections.append(section)

        # 이 세그먼트에 속한 실제 SECTION-(n+1) 컨테이너로 재귀
        for el in content:
            if el.tag in _SECTION_TAGS:
                self.process_container(el, crumb, canonical)

        return crumb, canonical


def _synthesize_statement_sections(sections: list[Section]) -> list[Section]:
    """캡션 표로만 구분된 재무제표를 가상 섹션으로 승격시킨다 (2023 형식).

    "2. 연결재무제표" 같은 컨테이너 섹션이 [캡션표, 본문표, 캡션표, ...]를
    통째로 들고 있으면, 캡션마다 자식 섹션을 만들어 표를 옮긴다.
    2024+ 형식(TITLE 있는 TABLE-GROUP)은 이미 섹션이므로 건드리지 않는다.
    """
    result: list[Section] = []
    for section in sections:
        result.append(section)
        captions = [(i, cap) for i, t in enumerate(section.tables) if (cap := statement_caption(t))]
        # 캡션이 2개 이상 = 여러 재무제표를 품은 컨테이너. 1개면 자기 자신이
        # 이미 그 재무제표의 섹션이므로(2024+ 형식) 분리하지 않는다.
        if len(captions) < 2:
            continue

        bounds = [i for i, _ in captions] + [len(section.tables)]
        kept = section.tables[: bounds[0]]
        for k, (idx, (title, _scale)) in enumerate(captions):
            child = Section(
                title=title,
                level=section.level + 1,
                order=0,  # 마지막에 일괄 재부여
                assoc_note=None,
                atocid=None,
                breadcrumb=section.breadcrumb + (title,),
                canonical=canonical_label(title, None) or section.canonical,
                tables=section.tables[idx : bounds[k + 1]],
            )
            child.content_hash = _content_hash(child)
            result.append(child)
        section.tables = kept
        section.content_hash = _content_hash(section)

    for order, section in enumerate(result):
        section.order = order
    return result


def _acode_facts(section: Section) -> list[NumericFact]:
    facts = []
    for table in section.tables:
        for row in table.rows:
            label = clean_row_label(row_label(row))
            for cell in row:
                if cell.acode is None or cell.value is None:
                    continue
                ctx = cell.acontext
                facts.append(
                    NumericFact(
                        concept=cell.acode,
                        context=ctx,
                        is_current_period=ctx.startswith("C") if ctx else None,
                        value=cell.value,
                        raw_text=cell.text,
                        row_label=label,
                        col_label=None,
                        section_title=section.title,
                        section_breadcrumb=section.breadcrumb,
                        section_canonical=section.canonical,
                    )
                )
    return facts


_STATEMENT_CANONICALS = {"재무상태표", "손익계산서", "현금흐름표"}


def _label_facts(section: Section) -> list[NumericFact]:
    scale = next(
        (s for t in section.tables for r in t.rows for c in r if (s := unit_scale(c.text))), 1
    )
    facts = []
    for table in section.tables:
        if statement_caption(table):
            continue
        for label, value, is_current, col, raw in label_facts(table, scale):
            facts.append(
                NumericFact(
                    concept=None,
                    context=None,
                    is_current_period=is_current,
                    value=value,
                    raw_text=raw,
                    row_label=label,
                    col_label=col,
                    section_title=section.title,
                    section_breadcrumb=section.breadcrumb,
                    section_canonical=section.canonical,
                )
            )
    return facts


def _extract_facts(sections: list[Section]) -> list[NumericFact]:
    facts = []
    for section in sections:
        section_facts = _acode_facts(section)
        # ACODE가 없는 옛 형식의 재무제표는 라벨 기반으로 보완
        if not section_facts and section.canonical in _STATEMENT_CANONICALS:
            section_facts = _label_facts(section)
        facts.extend(section_facts)
    return facts


def _header_value(root: etree._Element, xpath: str) -> str | None:
    el = root.find(xpath)
    if el is None:
        return None
    text = _norm_text(" ".join(el.itertext()))
    return text or None


def parse_filing(path: str | Path) -> ParsedFiling:
    """DART 정기공시 XML 파일 하나를 ParsedFiling으로 변환한다."""
    root, warnings = load_document(path)

    doc_name_el = root.find("DOCUMENT-NAME")
    period_from = root.find('.//TU[@AUNIT="PERIODFROM"]')
    period_to = root.find('.//TU[@AUNIT="PERIODTO"]')

    walker = _Walker()
    body = root.find("BODY")
    if body is None:
        warnings.append("no BODY element found")
    else:
        for child in body:
            if isinstance(child.tag, str) and child.tag in _SECTION_TAGS:
                walker.process_container(child, (), None)

    sections = _synthesize_statement_sections(walker.sections)

    filing = ParsedFiling(
        source_path=str(path),
        company_name=_header_value(root, "COMPANY-NAME"),
        doc_name=_header_value(root, "DOCUMENT-NAME"),
        doc_acode=doc_name_el.get("ACODE") if doc_name_el is not None else None,
        period_from=period_from.get("AUNITVALUE") if period_from is not None else None,
        period_to=period_to.get("AUNITVALUE") if period_to is not None else None,
        sections=sections,
        warnings=warnings,
    )
    filing.facts = _extract_facts(sections)
    return filing
