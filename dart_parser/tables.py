"""테이블 및 수치 셀 파싱.

DART 수치 셀 규약 (dart4.xsd, 픽스처로 검증):
  <TE ACODE="ifrs-full_CurrentAssets" ACONTEXT="CFY2026..." ADECIMAL="-6"
      ANEGATED="N" ...>306,220,075</TE>
  → 원문 숫자는 |ADECIMAL| 자릿수만큼 축약된 표기 (−6 = 백만 단위)
  → 실제 값 = 306,220,075 × 10^6 원
  ANEGATED="Y"는 표시 부호가 뒤집힌 셀 (개념값 = 표시값의 부호 반전).
"""

from __future__ import annotations

import re

from lxml import etree

from .models import Cell, Table

_CELL_TAGS = {"TD", "TE", "TU", "TH"}

# "306,220,075" / "(1,234)" / "-1,234" / "1,234.5" 를 수치로 인정
_NUM = re.compile(r"^\(?-?[\d,]+(?:\.\d+)?\)?$")


def cell_text(el: etree._Element) -> str:
    """셀 내부의 모든 텍스트(중첩 P/SPAN 포함)를 공백 정규화해 반환."""
    text = " ".join(t.strip() for t in el.itertext() if t.strip())
    return re.sub(r"\s+", " ", text.replace("　", " ")).strip()


def parse_number(text: str) -> int | float | None:
    """셀 텍스트를 숫자로 파싱. 회계 관례 괄호 = 음수. 실패 시 None."""
    t = text.replace("　", "").replace(" ", "").replace(",", "")
    if not t or t in {"-", "."}:
        return None
    negative = t.startswith("(") and t.endswith(")")
    if negative:
        t = t[1:-1]
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", t):
        return None
    num: int | float = float(t) if "." in t else int(t)
    return -num if negative else num


def scaled_value(text: str, adecimal: int | None, anegated: bool) -> int | float | None:
    num = parse_number(text)
    if num is None:
        return None
    if adecimal:
        num = num * 10 ** abs(adecimal)
    return -num if anegated else num


def parse_cell(el: etree._Element) -> Cell:
    acode = el.get("ACODE")
    acontext = el.get("ACONTEXT")
    adecimal_raw = el.get("ADECIMAL")

    # 일부 파일(2023 3분기보고서 등)은 ACONTEXT/ADECIMAL을 별도 속성으로
    # 두지 않고 ACODE 하나에 "concept|context|decimal|unit|" 형태로 압축한다.
    if acode and "|" in acode:
        concept, _, rest = acode.partition("|")
        parts = rest.split("|")
        acode = concept or None
        if acontext is None:
            acontext = parts[0] or None if len(parts) > 0 else None
        if adecimal_raw is None:
            adecimal_raw = parts[1] or None if len(parts) > 1 else None

    adecimal = int(adecimal_raw) if adecimal_raw and adecimal_raw.lstrip("-").isdigit() else None
    anegated = el.get("ANEGATED") == "Y"
    text = cell_text(el)

    value = None
    if acode and _NUM.match(text.replace("　", "").replace(" ", "")):
        value = scaled_value(text, adecimal, anegated)

    return Cell(
        text=text,
        tag=el.tag,
        acode=acode,
        acontext=acontext,
        adecimal=adecimal,
        anegated=anegated,
        value=value,
    )


def parse_table(el: etree._Element) -> Table:
    rows = []
    for tr in el.findall(".//TR"):
        cells = [parse_cell(c) for c in tr if c.tag in _CELL_TAGS]
        if cells:
            rows.append(cells)
    return Table(rows=rows, aclass=el.get("ACLASS"))


def row_label(cells: list[Cell]) -> str:
    """수치 사실(fact)의 행 라벨: 그 행에서 값이 없는 첫 텍스트 셀."""
    for c in cells:
        if c.value is None and c.text:
            return c.text
    return ""


# ── 라벨 기반 재무제표 추출 (ACODE 없는 2023~2024 형식) ──────────────────

STATEMENT_NAMES = ["재무상태표", "포괄손익계산서", "손익계산서", "자본변동표", "현금흐름표"]

_UNIT_SCALES = [("백만원", 1_000_000), ("천원", 1_000), ("억원", 100_000_000), ("원", 1)]
_UNIT_RE = re.compile(r"단위\s*:\s*([^)]+)")
_GI_RE = re.compile(r"제\s*(\d+)\s*기")
_NOTE_REF_RE = re.compile(r"\s*\(주\s*[\d,\s]*\)\s*")  # "매출액 (주26)" → "매출액"


def unit_scale(text: str) -> int | None:
    m = _UNIT_RE.search(text)
    if not m:
        return None
    for name, scale in _UNIT_SCALES:
        if name in m.group(1):
            return scale
    return None


def statement_caption(table: Table) -> tuple[str, int | None] | None:
    """표가 재무제표 캡션 표(제목 + 기수 + 단위)면 (제목행, 단위 스케일) 반환.

    캡션 표 형태 (연도 공통, 2024 예):
        ['연결 재무상태표'] / ['제 56 기 1분기말 ...'] / ['제 55 기말 ...'] / ['(단위 : 백만원)']
    """
    if not table.rows or len(table.rows) > 8:
        return None
    if any(len(r) > 1 for r in table.rows):
        return None
    texts = [r[0].text for r in table.rows if r[0].text]
    title = next(
        (t for t in texts if any(t.endswith(n) or f" {n}" in t for n in STATEMENT_NAMES) and "단위" not in t),
        None,
    )
    if title is None:
        return None
    scale = next((s for t in texts if (s := unit_scale(t)) is not None), None)
    return title, scale


def clean_row_label(text: str) -> str:
    return _NOTE_REF_RE.sub(" ", text).strip()


def label_facts(table: Table, scale: int) -> list[tuple[str, int | float, bool | None, str | None, str]]:
    """머리글의 제N기 번호로 당기/전기를 판별하는 라벨 기반 수치 추출.

    반환: (행 라벨, 환산값, 당기 여부, 열 라벨, 원문 텍스트) 목록.

    지원 형태 (픽스처로 검증):
      ['', 제56기 1분기말, 제55기말]           ← 값 열과 기 머리글이 1:1
      ['', 제56기 1분기, 제55기 1분기]
      ['3개월', '누적', '3개월', '누적']         ← 기당 열 2개 (손익계산서)
    그 외 배치는 당기 여부 None으로 값만 보존한다.
    """
    rows = table.rows
    gi_row_idx = next(
        (i for i, r in enumerate(rows) if any(_GI_RE.search(c.text) for c in r)), None
    )
    if gi_row_idx is None:
        return []

    gis = [int(m.group(1)) for c in rows[gi_row_idx] if (m := _GI_RE.search(c.text))]
    gi_labels = [c.text for c in rows[gi_row_idx] if _GI_RE.search(c.text)]
    max_gi = max(gis)

    # 기 머리글 다음 행이 '3개월/누적' 류의 하위 머리글인지 (숫자 없는 행)
    sub_labels: list[str] = []
    data_start = gi_row_idx + 1
    if data_start < len(rows) and all(parse_number(c.text) is None for c in rows[data_start]):
        candidate = [c.text for c in rows[data_start]]
        if candidate and all(t and len(t) <= 6 for t in candidate):
            sub_labels = candidate
            data_start += 1

    facts = []
    for row in rows[data_start:]:
        if not row or parse_number(row[0].text) is not None:
            continue
        label = clean_row_label(row[0].text)
        if not label:
            continue
        values = [(c, parse_number(c.text)) for c in row[1:]]
        values = [(c, v) for c, v in values if v is not None]
        m = len(values)
        for k, (cell, num) in enumerate(values):
            if m == len(gis):
                gi, col = gis[k], gi_labels[k]
            elif sub_labels and m == 2 * len(gis):
                sub = sub_labels[k % 2] if k % 2 < len(sub_labels) else ""
                gi, col = gis[k // 2], f"{gi_labels[k // 2]} {sub}".strip()
            else:
                gi, col = None, None
            facts.append((label, num * scale, (gi == max_gi) if gi is not None else None, col, cell.text))
    return facts
