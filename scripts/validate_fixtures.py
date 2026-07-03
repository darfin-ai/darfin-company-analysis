"""파서 검증 스크립트 — 삼성전자 1분기보고서 4개년(2023~2026) 픽스처 대상.

실행:
    python scripts/validate_fixtures.py [픽스처 디렉터리]

검증 항목:
  1. 파일별: 로더 경고, 섹션 수, 앵커(AASSOCNOTE/ATOCID) 커버리지
  2. 12개 표준 섹션 라벨이 매년 모두 매핑되는가
  3. 수치 사실(fact) 추출 스팟체크 (유동자산/매출 당기값)
  4. 연도 간 AASSOCNOTE 매칭률 (diff 단계의 전제 조건)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dart_parser import parse_filing
from dart_parser.canonical import CANONICAL_LABELS

DEFAULT_FIXTURES = Path(__file__).resolve().parent.parent.parent / "darfin-front" / "삼성전자 분기보고서"

import re

# (표시명, 표준 섹션, concept, 라벨 기반 폴백용 행 라벨 정규식)
# 라벨 정규식은 앞부분 고정(^): "순이익"만 쓰면
# "법인세비용차감전순이익" 같은 다른 행에 오매칭된다.
SPOT_CHECKS = [
    ("유동자산", "재무상태표", "ifrs-full_CurrentAssets", r"^유동자산"),
    # "영업수익"은 2024.03 사업보고서(FY2023)에서 매출액 대신 쓰인 행 라벨.
    # 같은 회사가 분기보고서에서는 "매출액"을 쓰므로 별칭으로 함께 인정한다.
    ("매출액", "손익계산서", "ifrs-full_Revenue", r"^(수익\(매출액\)|매출액|영업수익)"),
    ("순이익", "손익계산서", "ifrs-full_ProfitLoss", r"^(당기|분기|반기)?순이익"),
]


def find_fact(filing, canonical, concept, label_pattern):
    """연결 재무제표의 당기값 하나를 찾는다. ACODE 우선, 없으면 행 라벨."""
    def consolidated(f):
        return "연결" in " ".join(f.section_breadcrumb)

    hits = [f for f in filing.facts if f.concept == concept and f.is_current_period and consolidated(f)]
    if not hits:
        hits = [
            f for f in filing.facts
            if f.concept is None and f.section_canonical == canonical
            and re.search(label_pattern, f.row_label) and f.is_current_period and consolidated(f)
        ]
    return hits[0] if hits else None


def krw(v: float) -> str:
    return f"{v / 1_0000_0000_0000:,.1f}조원"


def main() -> int:
    fixture_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FIXTURES
    paths = sorted(fixture_dir.glob("*.xml"))
    if not paths:
        print(f"픽스처 없음: {fixture_dir}")
        return 1

    filings = {}
    failed = False

    for path in paths:
        print(f"\n{'=' * 72}\n{path.name}")
        filing = parse_filing(path)
        filings[path.name] = filing

        print(f"  회사: {filing.company_name} / 문서: {filing.doc_name} (ACODE {filing.doc_acode})")
        print(f"  기간: {filing.period_from} ~ {filing.period_to}")
        for w in filing.warnings:
            print(f"  ⚠ {w}")

        n = len(filing.sections)
        with_note = sum(1 for s in filing.sections if s.assoc_note)
        with_atocid = sum(1 for s in filing.sections if s.atocid)
        n_tables = sum(len(s.tables) for s in filing.sections)
        print(f"  섹션 {n}개 (AASSOCNOTE {with_note}, ATOCID {with_atocid}) / 테이블 {n_tables}개 / fact {len(filing.facts)}개")

        # 표준 라벨 커버리지 — 자체 매칭 섹션(상속 제외) 기준
        missing = []
        for label in CANONICAL_LABELS:
            secs = filing.sections_by_canonical(label)
            direct = [s for s in secs if s.canonical == label]
            if not direct:
                missing.append(label)
        if missing:
            failed = True
            print(f"  ✗ 표준 섹션 미매핑: {missing}")
        else:
            print(f"  ✓ 표준 섹션 12/12 매핑")

        # 수치 스팟체크: 연결 재무제표의 당기값
        for name, canonical, concept, label in SPOT_CHECKS:
            fact = find_fact(filing, canonical, concept, label)
            if fact:
                src = fact.concept or f"라벨:{fact.row_label}"
                print(f"  {name}: {krw(fact.value)}  [{fact.section_title} / {src}]")
            else:
                failed = True
                print(f"  ✗ {name}: 당기값 없음")

    # 연도 간 AASSOCNOTE 매칭률
    print(f"\n{'=' * 72}\n연도 간 섹션 매칭 (AASSOCNOTE 기준)")
    names = sorted(filings)
    for prev, curr in zip(names, names[1:]):
        prev_notes = {s.assoc_note for s in filings[prev].sections if s.assoc_note}
        curr_notes = {s.assoc_note for s in filings[curr].sections if s.assoc_note}
        common = prev_notes & curr_notes
        rate = len(common) / len(curr_notes) if curr_notes else 0
        print(f"  {prev[:14]}→{curr[:14]}: {len(common)}/{len(curr_notes)} ({rate:.0%})"
              f"  신규 {sorted(curr_notes - prev_notes)} / 소멸 {sorted(prev_notes - curr_notes)}")

    # 매출 시계열 (diff/재무추이 탭의 원천이 되는 값)
    print(f"\n연도별 매출액(연결, 당기) 시계열:")
    revenue_label = next(label for _, _, concept, label in SPOT_CHECKS if concept == "ifrs-full_Revenue")
    for name in names:
        fact = find_fact(filings[name], "손익계산서", "ifrs-full_Revenue", revenue_label)
        print(f"  {name[:20]}: {krw(fact.value) if fact else 'N/A'}")

    print(f"\n{'검증 실패 항목 있음 ✗' if failed else '전체 검증 통과 ✓'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
