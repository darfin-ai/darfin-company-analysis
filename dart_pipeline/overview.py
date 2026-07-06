"""company_overview 패널 추출 — 순수 함수 (Stage 4 일부). DB/LLM 비의존.

segments/products/regions/shareholders/dividend는 tables_json에서 결정론적으로
뽑는다(LLM 불필요). risks는 프로즈만 있어 별도로 llm.py: extract_risks()가
담당하고 이 모듈은 다루지 않는다.

표 탐지는 diff.py의 headcount_metrics/ownership_metrics와 같은 방식(헤더 텍스트
매칭 + 라벨 상태 추적)을 따른다 — 위치 기반(N번째 표)이 아니라 헤더/라벨 키워드
기반이라 표 순서가 바뀌어도 안전하지만, 라벨 문구 자체가 연도·회사마다 다를 수
있다는 이 프로젝트의 기존 한계는 동일하게 적용된다.
"""

from __future__ import annotations

import json
import re

from dart_parser.tables import parse_number

from .diff import _leading_labels, _norm

_SKIP_LABELS = {"총계", "합계", "계", "기타"}
_REGION_KEYWORDS = {"내수", "수출", "미주", "유럽", "중국", "아시아", "아시아ㆍ아프리카", "국내", "아프리카"}
_REVENUE_TYPE_LABELS = {"제ㆍ상품", "용역및기타매출", "용역", "계", "합계"}

_TOP_SHAREHOLDERS = 8


def _parse_amount(text: str) -> int | float | None:
    """'△110,167' 같은 한국 회계 관례 음수 표기(△)까지 처리."""
    t = text.strip()
    negative = t.startswith("△")
    if negative:
        t = t[1:]
    n = parse_number(t)
    if n is None:
        return None
    return -n if negative else n


def _parse_percent(text: str) -> float | None:
    n = _parse_amount(text.strip().rstrip("%"))
    return float(n) if n is not None else None


def _tables(chunk: dict) -> list[dict]:
    if not chunk["tables_json"]:
        return []
    return json.loads(chunk["tables_json"])


def _header_text(rows: list[list[dict]], n: int = 1) -> str:
    return _norm(" ".join(c["text"] for r in rows[:n] for c in r))


# ── segments ────────────────────────────────────────────────────────────

def extract_segments(chunks: list[dict]) -> list[dict]:
    """'(부문|구분) | 주요제품 | 매출액 | 비중' 표에서 사업 부문 추출.

    부문 이름 열 헤더는 '부문'뿐 아니라 '구분'으로도 쓰이고(단일 사업부문
    회사가 흔히 이렇게 씀 — 예: SK하이닉스), 매출액/비중/주요제품 열의
    순서도 회사마다 달라(SK하이닉스는 주요제품이 맨 끝) 위치가 아니라
    헤더 라벨로 열을 찾는다.
    """
    for chunk in chunks:
        for table in _tables(chunk):
            rows = table["rows"]
            if not rows:
                continue
            header_cells = [_norm(c["text"]) for c in rows[0]]
            header = "".join(header_cells)
            if not (
                ("부문" in header or "구분" in header)
                and "주요제품" in header
                and "매출액" in header
                and "비중" in header
            ):
                continue

            share_idx = next((i for i, c in enumerate(header_cells) if "비중" in c), None)
            revenue_idx = next(
                (i for i, c in enumerate(header_cells) if "매출액" in c and i != share_idx), None
            )
            product_idx = next((i for i, c in enumerate(header_cells) if "주요제품" in c), None)
            if share_idx is None or revenue_idx is None:
                continue

            out = []
            for row in rows[1:]:
                cells = [c["text"].strip() for c in row]
                if len(cells) <= max(share_idx, revenue_idx):
                    continue
                name = cells[0]
                if _norm(name) in _SKIP_LABELS:
                    continue
                share = _parse_percent(cells[share_idx])
                revenue = _parse_amount(cells[revenue_idx])
                if share is None or revenue is None:
                    continue
                description = cells[product_idx] if product_idx is not None and product_idx < len(cells) else ""
                out.append(
                    {
                        "name": name,
                        "description": description,
                        "revenue": revenue,
                        "revenueShare": share,
                    }
                )
            if out:
                return out
    return []


def compute_segment_status(segments: list[dict], baseline_segments: list[dict] | None) -> list[dict]:
    """이름 정규화 비교로 added/existing 채움. baseline 없으면(최초 filing) 전부 added."""
    baseline_names = {_norm(s["name"]) for s in baseline_segments} if baseline_segments else set()
    result = []
    for s in segments:
        status = "added" if (baseline_segments is not None and _norm(s["name"]) not in baseline_names) else (
            "added" if baseline_segments is None else "existing"
        )
        result.append({**s, "status": status})
    return result


# ── products / regions (같은 '구분|기간들' 표 패턴을 라벨로 구분) ──────────

def _period_tables(chunks: list[dict]):
    """'사업의 내용' 섹션에서 헤더가 '구분|제NN기 ...|...' 형태(기간 열에 숫자
    포함)인 표를 순회한다 — 원재료·감사 등 무관한 '구분' 표(예: 유형자산
    기초/증감/기말 롤포워드)를 배제하기 위해 기간 열에 숫자가 있는지까지 확인."""
    for chunk in chunks:
        if chunk["canonical_label"] != "사업의 내용":
            continue
        for table in _tables(chunk):
            rows = table["rows"]
            if not rows:
                continue
            header_cells = [c["text"].strip() for c in rows[0]]
            if not header_cells or _norm(header_cells[0]) != "구분":
                continue
            if len(header_cells) < 2 or not any(re.search(r"\d", c) for c in header_cells[1:]):
                continue
            yield chunk, rows


def _row_label(row: list[dict]) -> str:
    return _leading_labels(row)


def extract_products(chunks: list[dict]) -> list[dict]:
    for chunk, rows in _period_tables(chunks):
        data_rows = rows[1:]
        labels = {_norm(_row_label(r)) for r in data_rows if _row_label(r)}
        if not labels:
            continue
        if labels & {_norm(k) for k in _REGION_KEYWORDS}:
            continue  # regions 표
        if labels <= {_norm(k) for k in _REVENUE_TYPE_LABELS}:
            continue  # 매출유형(제품/용역) 표 — 제품군 아님

        amounts = []
        for row in data_rows:
            label = _row_label(row)
            if not label or _norm(label) in _SKIP_LABELS:
                continue
            nums = [parse_number(c["text"]) for c in row]
            current = next((n for n in nums if n is not None), None)
            if current is None:
                continue
            amounts.append((label, current))
        total = sum(a for _, a in amounts)
        if not amounts or total <= 0:
            continue
        return [{"name": label, "share": round(amount / total * 100, 1)} for label, amount in amounts]
    return []


def extract_regions(chunks: list[dict], baseline_regions: list[dict] | None) -> list[dict]:
    for chunk, rows in _period_tables(chunks):
        data_rows = rows[1:]
        labels = {_norm(_row_label(r)) for r in data_rows if _row_label(r)}
        if not (labels & {_norm(k) for k in _REGION_KEYWORDS}):
            continue

        amounts = []
        for row in data_rows:
            raw_label = _row_label(row)
            if not raw_label or _norm(raw_label) in _SKIP_LABELS:
                continue
            # "내수 국내" -> "국내", "수출 미주" -> "미주"
            tokens = raw_label.split()
            tokens = [t for t in tokens if _norm(t) not in {"내수", "수출"}]
            region = " ".join(tokens) or raw_label
            nums = [parse_number(c["text"]) for c in row]
            current = next((n for n in nums if n is not None), None)
            if current is None:
                continue
            amounts.append((region, current))
        total = sum(a for _, a in amounts)
        if not amounts or total <= 0:
            continue

        baseline_shares = {r["region"]: r["share"] for r in baseline_regions} if baseline_regions else {}
        out = []
        for region, amount in amounts:
            share = round(amount / total * 100, 1)
            delta = round(share - baseline_shares[region], 1) if region in baseline_shares else 0.0
            out.append({"region": region, "share": share, "delta": delta})
        return out
    return []


# ── shareholders ────────────────────────────────────────────────────────

# 보통주와 동급으로 취급하는 라벨. 일부 회사(예: SK하이닉스)는 보통주/우선주
# 구분 없이 '의결권 있는 주식'으로만 표기한다 — 실질은 보통주와 동일.
_COMMON_SHARE_LABELS = {"보통주", "의결권 있는 주식"}
_PREFERRED_SHARE_LABELS = {"우선주"}


def extract_shareholders(chunks: list[dict]) -> list[dict]:
    """'소유주식수및지분율' 표에서 보통주 기준 상위 주주를 뽑는다.

    rowspan 붕괴로 같은 주주의 우선주 행이 이름 없이(주식종류 라벨만) 나올
    수 있어 직전 이름/관계를 상태로 이어받는다.
    """
    share_labels = _COMMON_SHARE_LABELS | _PREFERRED_SHARE_LABELS
    for chunk in chunks:
        for table in _tables(chunk):
            rows = table["rows"]
            if not rows:
                continue
            header = _header_text(rows, n=2)
            if "소유주식수및지분율" not in header:
                continue

            out = []
            current_name, current_relation = None, None
            for row in rows:
                cells = [c["text"].strip() for c in row]
                if not cells:
                    continue
                share_type_idx = next((i for i, c in enumerate(cells) if c in share_labels), None)
                if share_type_idx is None:
                    continue
                share_type = cells[share_type_idx]

                if share_type_idx == 0:
                    name, relation = current_name, current_relation
                else:
                    name = cells[0]
                    relation = cells[1] if len(cells) > 1 and share_type_idx > 1 else None
                    current_name, current_relation = name, relation

                if share_type not in _COMMON_SHARE_LABELS or not name or name.startswith("※") or _norm(name) in _SKIP_LABELS:
                    continue

                floats = [parse_number(c) for c in cells if parse_number(c) is not None and "." in c]
                if not floats:
                    continue
                out.append({"name": name, "detail": relation or "", "share": floats[-1]})

            if out:
                out.sort(key=lambda s: s["share"], reverse=True)
                return out[:_TOP_SHAREHOLDERS]
    return []


# ── dividend ────────────────────────────────────────────────────────────

_DIVIDEND_LABELS = {
    "주당현금배당금(원)": "perShareKrw",
    "현금배당수익률(%)": "yieldPct",
    "(연결)현금배당성향(%)": "payoutRatioPct",
}


def extract_dividend(dividend_chunk: dict | None) -> dict | None:
    """'[주요 배당지표]' 표에서 보통주 기준 당기/전기/전전기 값을 뽑는다.

    rowspan 붕괴로 '보통주'/'우선주' 하위 행이 라벨 없이 나오므로 직전 라벨을
    상태로 이어받는다.
    """
    if dividend_chunk is None:
        return None
    for table in _tables(dividend_chunk):
        rows = table["rows"]
        if not rows:
            continue
        header = _header_text(rows)
        if not ("당기" in header and "전기" in header):
            continue

        values: dict[str, list[float]] = {}
        current_key = None
        for row in rows[1:]:
            cells = [c["text"].strip() for c in row]
            if not cells:
                continue
            label_norm = _norm(cells[0])
            if label_norm in _DIVIDEND_LABELS:
                current_key = _DIVIDEND_LABELS[label_norm]
                data_cells = cells[1:]
            elif cells[0] in ("보통주", "우선주"):
                data_cells = cells[1:]
            else:
                current_key = None
                continue

            if current_key is None or cells[0] == "우선주":
                continue
            nums = [parse_number(c) for c in data_cells]
            nums = [n for n in nums if n is not None]
            if nums:
                values[current_key] = nums

        per_share = values.get("perShareKrw")
        if not per_share:
            continue
        yield_pct = values.get("yieldPct")
        payout = values.get("payoutRatioPct")
        history = [
            {"year": label, "perShareKrw": (per_share[i] if i < len(per_share) else None)}
            for i, label in enumerate(("당기", "전기", "전전기"))
        ]
        return {
            "perShareKrw": per_share[0],
            "yieldPct": yield_pct[0] if yield_pct else 0.0,
            "payoutRatioPct": payout[0] if payout else 0.0,
            "history": history,
        }
    return None
