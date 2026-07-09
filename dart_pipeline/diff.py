"""섹션 diff 엔진 — 순수 비교 로직 (Stage 3, 네트워크/DB 없음).

입력은 db.load_chunks()/load_metrics()의 행 딕셔너리, 출력은 section_diffs
테이블 삽입용 딕셔너리(= 프론트 SectionDiffEntry와 1:1, ddl.sql §7).

의미론은 darfin-front lib/comparison.js를 따른다:
  - QoQ = 직전 공시 (1분기보고서의 QoQ는 전년 사업보고서)
  - YoY = 전년 동분기 (같은 reprt_code, 연도-1)
  - 분기 유량(손익/현금흐름)을 연간 유량과 같은 종류처럼 diff하지 않는다
    → 손익/현금흐름은 기간 한정자(3개월/누적)가 양쪽에서 같은 의미일 때만 비교.
    재무상태표는 시점 수치라 항상 비교 가능.
  - "검사했지만 변경 없음"은 저장하지 않는다 — 프론트 groupDiffsBySection()이
    빈 (섹션, 기준) 쌍도 그리드에 렌더링하므로 diff 행은 실제 변경만 담으면 된다.
"""

from __future__ import annotations

import difflib
import json
import re

from dart_parser.tables import parse_number

# ── 비교 대상 구성 (프론트 DIFF_SECTION_CONFIG와 1:1) ─────────────────────

SECTION_COMPARISONS: dict[str, tuple[str, list[str]]] = {
    "회사의 개요": ("structural", ["QoQ"]),
    "사업의 내용": ("text", ["QoQ", "YoY"]),
    "위험요인": ("text", ["QoQ", "YoY"]),
    "재무상태표": ("numeric", ["QoQ", "YoY"]),
    "손익계산서": ("numeric", ["QoQ", "YoY"]),
    "현금흐름표": ("numeric", ["QoQ", "YoY"]),
    "주석": ("text_numeric", ["QoQ", "YoY"]),
    "계열회사 현황": ("structural", ["QoQ"]),
    "중요한 계약": ("text", ["QoQ"]),
    "임원 및 직원": ("headcount", ["QoQ", "YoY"]),
    "주주현황": ("ownership", ["QoQ", "YoY"]),
    "지배구조": ("event", ["QoQ"]),
}

_STATEMENT_LABELS = {"재무상태표", "손익계산서", "현금흐름표"}

_CLIP = 6000  # before/after 텍스트 상한 (TEXT 컬럼 및 LLM 입력 예산)


# ── 공시 순서 및 baseline 결정 ─────────────────────────────────────────────

# 한 사업연도 안의 공시 순서: 1분기 → 반기 → 3분기 → 사업(연간)
_REPRT_SEQ = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}


def order_filings(filings: list[dict]) -> list[dict]:
    """(연도, 보고서 순서)로 정렬. 정정공시(같은 연도·유형 복수 행)는 filed_date 최신만."""
    latest: dict[tuple, dict] = {}
    for f in filings:
        key = (f["bsns_year"], f["reprt_code"])
        if key not in latest or f["filed_date"] > latest[key]["filed_date"]:
            latest[key] = f
    return sorted(latest.values(), key=lambda f: (int(f["bsns_year"]), _REPRT_SEQ[f["reprt_code"]]))


def resolve_baselines(ordered: list[dict], rcept_no: str) -> dict[str, dict | None]:
    """QoQ = 정렬 순서상 직전 공시, YoY = 같은 reprt_code의 전년 공시."""
    idx = next((i for i, f in enumerate(ordered) if f["rcept_no"] == rcept_no), None)
    if idx is None:
        return {"QoQ": None, "YoY": None}
    current = ordered[idx]
    qoq = ordered[idx - 1] if idx > 0 else None
    yoy = next(
        (
            f for f in ordered[:idx]
            if f["reprt_code"] == current["reprt_code"]
            and int(f["bsns_year"]) == int(current["bsns_year"]) - 1
        ),
        None,
    )
    return {"QoQ": qoq, "YoY": yoy}


# ── 섹션 매칭 ──────────────────────────────────────────────────────────────

_NUM_PREFIX = re.compile(r"^\s*(\d+(?:[-.]\d+)*)[.\s]")


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _section_key(chunk: dict) -> str:
    """연도 간 섹션 매칭 키. assoc_note 우선, 없으면 정규화 제목(60자).

    주석은 연결/별도 서브트리에 같은 제목의 노트가 반복되므로
    breadcrumb의 연결 여부를 키에 포함해 구분한다.
    """
    scope = "C" if "연결" in chunk["breadcrumb"] else "S"
    if chunk["assoc_note"]:
        return f"{scope}|{chunk['assoc_note']}"
    return f"{scope}|{_norm(chunk['section_title'])[:60]}"


def _prefix_key(chunk: dict) -> str | None:
    """제목 본문이 바뀌어도 붙잡을 수 있는 번호 접두사 키 (예: '2.17')."""
    m = _NUM_PREFIX.match(chunk["section_title"])
    if not m:
        return None
    scope = "C" if "연결" in chunk["breadcrumb"] else "S"
    return f"{scope}|№{m.group(1)}"


def match_sections(
    cur: list[dict], base: list[dict]
) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """(매칭 쌍, 신규, 소멸). 1차 assoc_note/제목 키, 2차 번호 접두사 키."""
    cur_map = {}
    for c in cur:
        cur_map.setdefault(_section_key(c), c)
    base_map = {}
    for b in base:
        base_map.setdefault(_section_key(b), b)

    pairs = [(cur_map[k], base_map[k]) for k in cur_map.keys() & base_map.keys()]
    added = [cur_map[k] for k in cur_map.keys() - base_map.keys()]
    removed = [base_map[k] for k in base_map.keys() - cur_map.keys()]

    # 2차: 제목 텍스트가 바뀐 같은 번호의 섹션을 modified로 승격
    added_by_prefix = {pk: c for c in added if (pk := _prefix_key(c))}
    still_removed = []
    for b in removed:
        pk = _prefix_key(b)
        if pk and pk in added_by_prefix:
            c = added_by_prefix.pop(pk)
            pairs.append((c, b))
            added.remove(c)
        else:
            still_removed.append(b)
    return pairs, added, still_removed


# ── 텍스트 diff ────────────────────────────────────────────────────────────

def paragraph_diff(before_content: str, after_content: str) -> tuple[str, str]:
    """문단 단위 difflib으로 변경 구간만 격리해 (before, after)를 돌려준다."""
    before_pars = [p for p in before_content.split("\n") if p.strip()]
    after_pars = [p for p in after_content.split("\n") if p.strip()]
    removed, added = [], []
    sm = difflib.SequenceMatcher(None, before_pars, after_pars, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op in ("replace", "delete"):
            removed.extend(before_pars[i1:i2])
        if op in ("replace", "insert"):
            added.extend(after_pars[j1:j2])
    return "\n".join(removed)[:_CLIP], "\n".join(added)[:_CLIP]


def _excerpt(chunk: dict) -> str:
    text = chunk["content"].strip() or chunk["section_title"]
    return text[:500]


# ── 표 행 라벨 (구조형 섹션의 표 기반 변경 감지: 계열회사 목록 등) ─────────

def _table_rows(chunk: dict) -> list[list[dict]]:
    if not chunk["tables_json"]:
        return []
    return [row for t in json.loads(chunk["tables_json"]) for row in t["rows"]]


def table_row_labels(chunks: list[dict]) -> set[str]:
    """섹션 그룹 내 모든 표의 행 라벨(첫 비수치 셀). 항목 추가/삭제 감지용."""
    labels = set()
    for chunk in chunks:
        for row in _table_rows(chunk):
            if len(row) < 2:
                continue
            first = row[0]["text"].strip()
            if first and parse_number(first) is None and not first.startswith(("※", "(", "-")):
                labels.add(_norm(first))
    return labels


# ── 인력(headcount) 추출 ───────────────────────────────────────────────────

def _leading_labels(row: list[dict]) -> str:
    parts = []
    for cell in row:
        if parse_number(cell["text"]) is not None:
            break
        if cell["text"].strip():
            parts.append(cell["text"].strip())
    return " ".join(parts)


def headcount_metrics(chunks: list[dict]) -> dict[str, float]:
    """임원 및 직원 섹션에서 인원수 지표를 추출한다.

    - 직원 현황 표(머리글에 '직원수'): 행별 직원 합계.
      colspan 정보가 저장되지 않아 열 위치를 특정할 수 없으므로, 소수점 셀
      (평균근속연수, 예: 13.7) 직전의 정수를 합계로 삼는다 — 표 구조상
      '직원 수 합계' 열이 항상 근속연수 바로 앞이다 (삼성전자 전 연도 검증).
    - 미등기임원 표(머리글 '구분'+'인원수'): 인원수 = 라벨 뒤 첫 정수.
    """
    out: dict[str, float] = {}
    for chunk in chunks:
        if not chunk["tables_json"]:
            continue
        for table in json.loads(chunk["tables_json"]):
            rows = table["rows"]
            if not rows:
                continue
            header = _norm(" ".join(c["text"] for r in rows[:2] for c in r))
            if "직원수" in header and "사업부문" in header:
                for row in rows:
                    label = _leading_labels(row)
                    if not label or label.startswith("※"):
                        continue
                    nums = [parse_number(c["text"]) for c in row]
                    dot_idx = next(
                        (i for i, (c, n) in enumerate(zip(row, nums))
                         if isinstance(n, float) and "." in c["text"]),
                        None,
                    )
                    if dot_idx is None:
                        continue
                    total = next(
                        (n for i in range(dot_idx - 1, -1, -1)
                         if isinstance((n := nums[i]), int)),
                        None,
                    )
                    if total is not None:
                        out[f"직원 수 ({_norm(label)})"] = total
            elif "구분" in header and "인원수" in header:
                for row in rows:
                    label = _leading_labels(row)
                    if _norm(label) == "미등기임원":
                        n = next((v for c in row if isinstance((v := parse_number(c["text"])), int)), None)
                        if n is not None:
                            out["미등기임원 수"] = n
    return out


# ── 지분(ownership) 추출 ───────────────────────────────────────────────────

def _row_floats(row: list[dict]) -> list[float]:
    """행에서 소수점 표기 수치(지분율)만 뽑는다. 주식수(정수)는 제외."""
    return [
        n for c in row
        if isinstance((n := parse_number(c["text"])), float) and "." in c["text"]
    ]


def ownership_metrics(chunks: list[dict]) -> dict[str, float]:
    """주주현황 섹션에서 지분율(%) 지표를 추출한다.

    - 최대주주 및 특수관계인 표(머리글 '소유주식수 및 지분율'): 행별 기말
      지분율 = 행의 마지막 소수점 수치 (기초/기말 두 열 중 기말이 뒤).
    - 5% 이상 주주 표(머리글 '주주명'+'지분율'): 주주별 지분율.
    - 소액주주 표(머리글 '소액주주수'): 소액주주 소유주식 비율 = 마지막 소수점 수치.
    """
    out: dict[str, float] = {}
    for chunk in chunks:
        if not chunk["tables_json"]:
            continue
        for table in json.loads(chunk["tables_json"]):
            rows = table["rows"]
            if not rows:
                continue
            header = _norm(" ".join(c["text"] for r in rows[:2] for c in r))
            if "소유주식수및지분율" in header:
                for row in rows:
                    label = _leading_labels(row)
                    floats = _row_floats(row)
                    if label and floats and not label.startswith("※"):
                        out[f"{_norm(label)} 지분율"] = floats[-1]
            elif "주주명" in header and "지분율" in header:
                for row in rows:
                    label = _leading_labels(row)
                    floats = _row_floats(row)
                    # rowspan 붕괴로 '5% 이상 주주' 같은 구분 셀이 라벨 앞에 붙을 수 있음
                    if label and floats:
                        name = label.split()[-1]
                        out[f"{name} 지분율"] = floats[-1]
            elif "소액주주수" in header or "소액주식수" in header:
                for row in rows:
                    if "소액주주" in _leading_labels(row):
                        floats = _row_floats(row)
                        if floats:
                            out["소액주주 소유주식 비율"] = floats[-1]
    return out


# ── 재무제표 수치 (metrics 테이블) ─────────────────────────────────────────

_KEY_ACCOUNTS: dict[str, list[tuple[str, str]]] = {
    "재무상태표": [
        ("ifrs-full_CurrentAssets", "유동자산"),
        ("ifrs-full_Assets", "자산총계"),
        ("ifrs-full_CurrentLiabilities", "유동부채"),
        ("ifrs-full_Liabilities", "부채총계"),
        ("ifrs-full_Equity", "자본총계"),
    ],
    "손익계산서": [
        ("ifrs-full_Revenue", "매출액"),
        ("ifrs-full_GrossProfit", "매출총이익"),
        ("dart_OperatingIncomeLoss", "영업이익"),
        ("ifrs-full_ProfitLoss", "당기순이익"),
    ],
    "현금흐름표": [
        ("ifrs-full_CashFlowsFromUsedInOperatingActivities", "영업활동 현금흐름"),
        ("ifrs-full_CashFlowsFromUsedInInvestingActivities", "투자활동 현금흐름"),
        ("ifrs-full_CashFlowsFromUsedInFinancingActivities", "재무활동 현금흐름"),
    ],
}


def _flow_bucket(reprt_code: str, qualifier: str | None) -> str:
    """유량(손익/현금흐름) 행의 기간 의미. reprt_code에 따라 NULL의 뜻이 다르다."""
    if qualifier == "3개월":
        return "3M"
    if qualifier == "누적":
        return "CUM"
    # qualifier 없음: 1분기는 3개월=누적(BOTH), 그 외는 보고 기간 전체(누적으로 취급)
    return "BOTH" if reprt_code == "11013" else "CUM"


def statement_values(metrics_rows: list[dict], reprt_code: str) -> dict[str, dict[str, dict[str, float]]]:
    """metrics 행 → {재무제표: {기간 버킷: {concept/계정명: 금액}}} (연결만).

    재무상태표는 시점 수치라 버킷 'PT' 하나. 유량은 3M/CUM (BOTH는 양쪽에 채움).
    """
    out: dict[str, dict[str, dict[str, float]]] = {}
    for row in metrics_rows:
        if not row["is_consolidated"] or row["amount"] is None:
            continue
        st = row["statement_type"]
        if st not in _STATEMENT_LABELS:
            continue
        buckets = (
            ["PT"] if st == "재무상태표"
            else ["3M", "CUM"] if _flow_bucket(reprt_code, row["period_qualifier"]) == "BOTH"
            else [_flow_bucket(reprt_code, row["period_qualifier"])]
        )
        keys = [k for k in (row["concept"], _norm(row["account_nm"])) if k]
        for bucket in buckets:
            slot = out.setdefault(st, {}).setdefault(bucket, {})
            for key in keys:
                slot.setdefault(key, row["amount"])
    return out


def numeric_metrics(
    statement: str,
    comparison_type: str,
    cur_values: dict,
    base_values: dict,
    annual_pair: bool = False,
) -> list[dict]:
    """핵심 계정 화이트리스트를 양쪽에서 찾아 NumericDeltaMetric 목록으로.

    재무상태표(시점)는 항상 비교. 손익/현금흐름(유량)은 QoQ=3개월끼리,
    YoY=누적끼리만 — 버킷이 한쪽에 없으면(예: 1분기 QoQ의 baseline이
    사업보고서) 그 (섹션, 기준)은 비교 불가로 비운다.
    annual_pair=True(사업보고서끼리 YoY)면 연간 전체 비교라 기간 접미사 생략.
    """
    if statement == "재무상태표":
        bucket, suffix = "PT", ""
    elif comparison_type == "QoQ":
        bucket, suffix = "3M", " (3개월)"
    else:
        bucket, suffix = "CUM", "" if annual_pair else " (누적)"

    cur_slot = cur_values.get(statement, {}).get(bucket, {})
    base_slot = base_values.get(statement, {}).get(bucket, {})
    metrics = []
    for concept, label in _KEY_ACCOUNTS[statement]:
        cur_v = cur_slot.get(concept, cur_slot.get(_norm(label)))
        base_v = base_slot.get(concept, base_slot.get(_norm(label)))
        if cur_v is not None and base_v is not None:
            metrics.append(
                {"label": f"{label}{suffix}", "current": cur_v, "baseline": base_v, "unit": "KRW"}
            )
    return metrics


# ── diff 엔트리 조립 ───────────────────────────────────────────────────────

def _source_ref(rcept_no: str, chunk: dict | None) -> str:
    if chunk is None:
        return rcept_no
    anchor = chunk["assoc_note"] or chunk["atocid"] or f"ord{chunk['section_order']}"
    return f"{rcept_no}#{anchor}"


def _entry(
    *,
    canonical: str,
    comparison_type: str,
    analysis_type: str,
    rcept_no: str,
    change_type: str | None = None,
    before: str | None = None,
    after: str | None = None,
    metrics: list[dict] | None = None,
    chunk: dict | None = None,
) -> dict:
    return {
        "canonical_label": canonical,
        "comparison_type": comparison_type,
        "analysis_type": analysis_type,
        "change_type": change_type,
        "before_text": before,
        "after_text": after,
        "metrics_json": json.dumps(metrics, ensure_ascii=False) if metrics else None,
        "source_label": chunk["breadcrumb"][:500] if chunk else None,
        "source_ref": _source_ref(rcept_no, chunk)[:200],
    }


def _count_metrics(cur: dict[str, float], base: dict[str, float], unit: str, unit_label: str | None) -> list[dict]:
    metrics = []
    for label in cur.keys() & base.keys():
        if cur[label] != base[label]:
            m = {"label": label, "current": cur[label], "baseline": base[label], "unit": unit}
            if unit_label:
                m["unitLabel"] = unit_label
            metrics.append(m)
    return sorted(metrics, key=lambda m: m["label"])


def _diff_text_sections(
    canonical: str,
    analysis_type: str,
    comparison_type: str,
    rcept_no: str,
    cur_secs: list[dict],
    base_secs: list[dict],
    *,
    table_labels: bool = False,
) -> list[dict]:
    """텍스트/구조/이벤트형 공통: 해시 게이트 → 문단 diff, 하위섹션 추가/소멸."""
    entries = []
    pairs, added, removed = match_sections(cur_secs, base_secs)

    # 대량 미매칭 = 실제 공시 변경이 아니라 섹션 구조 개편(연도 간 XML 형식
    # 차이로 파서의 분할 단위가 달라진 경우 등). 하위섹션별 스팸 대신
    # 요약 엔트리 하나로 접는다. (2023↔2024 형식 전환에서 주석 60여 건씩 발생)
    def _collapse(chunks: list[dict], total: int) -> bool:
        return len(chunks) >= 10 and len(chunks) >= 0.5 * max(total, 1)

    for group, change_type, collapse in (
        (added, "added", _collapse(added, len(cur_secs))),
        (removed, "removed", _collapse(removed, len(base_secs))),
    ):
        if collapse:
            titles = ", ".join(c["section_title"][:30] for c in group[:8])
            summary = f"하위섹션 {len(group)}개 일괄 {'신규' if change_type == 'added' else '소멸'} (구조 개편): {titles} …"
            entries.append(
                _entry(
                    canonical=canonical, comparison_type=comparison_type,
                    analysis_type=analysis_type, rcept_no=rcept_no,
                    change_type=change_type,
                    before=summary if change_type == "removed" else None,
                    after=summary if change_type == "added" else None,
                    chunk=group[0],
                )
            )
            group.clear()

    for cur_chunk, base_chunk in pairs:
        if cur_chunk["content_hash"] == base_chunk["content_hash"]:
            continue
        before, after = paragraph_diff(base_chunk["content"], cur_chunk["content"])
        if before or after:
            entries.append(
                _entry(
                    canonical=canonical, comparison_type=comparison_type,
                    analysis_type=analysis_type, rcept_no=rcept_no,
                    change_type="modified", before=before or None, after=after or None,
                    chunk=cur_chunk,
                )
            )
    for chunk in added:
        entries.append(
            _entry(
                canonical=canonical, comparison_type=comparison_type,
                analysis_type=analysis_type, rcept_no=rcept_no,
                change_type="added", after=_excerpt(chunk), chunk=chunk,
            )
        )
    for chunk in removed:
        entries.append(
            _entry(
                canonical=canonical, comparison_type=comparison_type,
                analysis_type=analysis_type, rcept_no=rcept_no,
                change_type="removed", before=_excerpt(chunk), chunk=chunk,
            )
        )

    # 구조형: 표 행 라벨 집합 비교 (계열회사 목록의 신규/제외 회사 등)
    if table_labels:
        cur_labels = table_row_labels(cur_secs)
        base_labels = table_row_labels(base_secs)
        new_rows = sorted(cur_labels - base_labels)[:30]
        gone_rows = sorted(base_labels - cur_labels)[:30]
        if new_rows or gone_rows:
            anchor = cur_secs[0] if cur_secs else None
            entries.append(
                _entry(
                    canonical=canonical, comparison_type=comparison_type,
                    analysis_type=analysis_type, rcept_no=rcept_no,
                    change_type="modified",
                    before=", ".join(gone_rows) or None,
                    after=", ".join(new_rows) or None,
                    chunk=anchor,
                )
            )
    return entries


def diff_pair(
    *,
    rcept_no: str,
    reprt_code: str,
    baseline_reprt_code: str,
    comparison_type: str,
    cur_chunks: list[dict],
    base_chunks: list[dict],
    cur_metrics: list[dict],
    base_metrics: list[dict],
    cur_headcount: dict[str, float] | None = None,
    base_headcount: dict[str, float] | None = None,
    cur_ownership: dict[str, float] | None = None,
    base_ownership: dict[str, float] | None = None,
) -> list[dict]:
    """공시 한 쌍의 전체 섹션 diff. section_diffs 삽입용 행(부분) 목록을 돌려준다."""
    cur_by_label: dict[str, list[dict]] = {}
    for c in cur_chunks:
        if c["canonical_label"]:
            cur_by_label.setdefault(c["canonical_label"], []).append(c)
    base_by_label: dict[str, list[dict]] = {}
    for c in base_chunks:
        if c["canonical_label"]:
            base_by_label.setdefault(c["canonical_label"], []).append(c)

    cur_values = statement_values(cur_metrics, reprt_code)
    base_values = statement_values(base_metrics, baseline_reprt_code)

    entries: list[dict] = []
    for canonical, (analysis_type, comparisons) in SECTION_COMPARISONS.items():
        if comparison_type not in comparisons:
            continue
        cur_secs = cur_by_label.get(canonical, [])
        base_secs = base_by_label.get(canonical, [])

        if analysis_type == "numeric":
            metrics = numeric_metrics(
                canonical, comparison_type, cur_values, base_values,
                annual_pair=(reprt_code == "11011" and baseline_reprt_code == "11011"),
            )
            if metrics:
                anchor = next((s for s in cur_secs if "연결" in s["breadcrumb"]), cur_secs[0] if cur_secs else None)
                entries.append(
                    _entry(
                        canonical=canonical, comparison_type=comparison_type,
                        analysis_type="numeric", rcept_no=rcept_no,
                        metrics=metrics, chunk=anchor,
                    )
                )
        elif analysis_type == "headcount":
            cur_hc = cur_headcount if cur_headcount is not None else headcount_metrics(cur_secs)
            base_hc = base_headcount if base_headcount is not None else headcount_metrics(base_secs)
            metrics = _count_metrics(cur_hc, base_hc, "count", "명")
            if metrics:
                entries.append(
                    _entry(
                        canonical=canonical, comparison_type=comparison_type,
                        analysis_type="headcount", rcept_no=rcept_no,
                        metrics=metrics, chunk=cur_secs[0] if cur_secs else None,
                    )
                )
        elif analysis_type == "ownership":
            cur_own = cur_ownership if cur_ownership is not None else ownership_metrics(cur_secs)
            base_own = base_ownership if base_ownership is not None else ownership_metrics(base_secs)
            metrics = _count_metrics(cur_own, base_own, "%", None)
            if metrics:
                entries.append(
                    _entry(
                        canonical=canonical, comparison_type=comparison_type,
                        analysis_type="ownership", rcept_no=rcept_no,
                        metrics=metrics, chunk=cur_secs[0] if cur_secs else None,
                    )
                )
        else:  # text / text_numeric / structural / event
            entries.extend(
                _diff_text_sections(
                    canonical, analysis_type, comparison_type, rcept_no,
                    cur_secs, base_secs,
                    table_labels=(analysis_type == "structural"),
                )
            )
    return entries
