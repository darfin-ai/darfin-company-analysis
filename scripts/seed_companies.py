"""커버 대상 기업 시딩 CLI — KOSPI/KOSDAQ 시가총액 상위 15개사(우선주 제외).

stock/companies 테이블에 등록만 한다(공시 수집은 하지 않음). 등록 후
run_daily_scan.py가 이 회사들을 커버 대상으로 집어간다:

    python scripts/seed_companies.py            # 등록 (멱등)
    python scripts/run_daily_scan.py --from 20230101   # 백필

corp_code는 이름이 아니라 corpCode.xml의 종목코드 매핑으로 결정하고,
DART 쪽 회사명과 대조해 어긋나면 등록하지 않는다 — 과거 수동 시딩에서
SK하이닉스 행에 현대차 corp_code가 들어갔던 사고의 재발 방지.

목록은 2026-07-08 네이버 금융 시가총액 순위 기준. 순위는 바뀌어도
커버리지가 목적이라 주기적 갱신은 불필요(회사 추가는 이 파일에 행 추가).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dart_pipeline import DartClient, db
from dart_pipeline.company_names import canonical_company_name
from dart_pipeline.corp_codes import load_corp_codes

# (종목코드, 회사명, 시장, 업종) — 업종은 카드 표시용 라벨
SEED_COMPANIES = [
    # KOSPI 시가총액 상위 15 (보통주 기준)
    ("005930", "삼성전자", "KOSPI", "전기전자"),
    ("000660", "SK하이닉스", "KOSPI", "전기전자"),
    ("402340", "SK스퀘어", "KOSPI", "지주회사"),
    ("009150", "삼성전기", "KOSPI", "전기전자"),
    ("005380", "현대자동차", "KOSPI", "자동차"),
    ("373220", "LG에너지솔루션", "KOSPI", "2차전지"),
    ("032830", "삼성생명", "KOSPI", "보험"),
    ("028260", "삼성물산", "KOSPI", "종합상사·건설"),
    ("207940", "삼성바이오로직스", "KOSPI", "바이오"),
    ("000270", "기아", "KOSPI", "자동차"),
    ("105560", "KB금융", "KOSPI", "금융지주"),
    ("329180", "HD현대중공업", "KOSPI", "조선"),
    ("012450", "한화에어로스페이스", "KOSPI", "방위산업"),
    ("055550", "신한지주", "KOSPI", "금융지주"),
    ("034020", "두산에너빌리티", "KOSPI", "발전설비"),
    # KOSDAQ 시가총액 상위 15
    ("196170", "알테오젠", "KOSDAQ", "바이오"),
    ("247540", "에코프로비엠", "KOSDAQ", "2차전지 소재"),
    ("086520", "에코프로", "KOSDAQ", "지주회사"),
    ("277810", "레인보우로보틱스", "KOSDAQ", "로봇"),
    ("036930", "주성엔지니어링", "KOSDAQ", "반도체 장비"),
    ("950160", "코오롱티슈진", "KOSDAQ", "바이오"),
    ("028300", "HLB", "KOSDAQ", "바이오"),
    ("058470", "리노공업", "KOSDAQ", "반도체 부품"),
    ("240810", "원익IPS", "KOSDAQ", "반도체 장비"),
    ("298380", "에이비엘바이오", "KOSDAQ", "바이오"),
    ("141080", "리가켐바이오", "KOSDAQ", "바이오"),
    ("319660", "피에스케이", "KOSDAQ", "반도체 장비"),
    ("000250", "삼천당제약", "KOSDAQ", "제약"),
    ("039030", "이오테크닉스", "KOSDAQ", "반도체 장비"),
    ("222800", "심텍", "KOSDAQ", "반도체 기판"),
]


def _normalize_name(name: str) -> str:
    return name.replace(" ", "").replace("(주)", "").replace("주식회사", "")


def main() -> int:
    ap = argparse.ArgumentParser(description="커버 대상 기업 시딩 (stock/companies 등록)")
    ap.add_argument("--dry-run", action="store_true", help="DB 기록 없이 corp_code 매핑만 확인")
    args = ap.parse_args()

    book = load_corp_codes(DartClient())

    failed = 0
    with db.connection() as conn:
        for stock_code, name, market, sector in SEED_COMPANIES:
            entry = book.by_stock_code(stock_code)
            if entry is None:
                print(f"✗ {name}({stock_code}): corpCode.xml에 종목코드 없음 — 스킵")
                failed += 1
                continue
            dart_name = canonical_company_name(stock_code, entry.corp_name)
            seed_name = canonical_company_name(stock_code, name)
            if _normalize_name(dart_name) != _normalize_name(seed_name):
                # 종목코드로 찾은 DART 회사명이 시드 목록과 다르면 목록 오타나
                # 코드 재사용 가능성 — 눈으로 확인 전까지 등록하지 않는다.
                print(f"✗ {name}({stock_code}): DART 회사명 불일치 ('{entry.corp_name}') — 스킵")
                failed += 1
                continue

            if args.dry_run:
                print(f"  {name}({stock_code}) → corp_code {entry.corp_code} [{market}/{sector}]")
                continue

            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO stock (company_name, dart_corp_code, stock_code, market_type) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE company_name = VALUES(company_name), "
                    "  stock_code = VALUES(stock_code), market_type = VALUES(market_type)",
                    (seed_name, entry.corp_code, stock_code, market),
                )
                cur.execute(
                    "INSERT INTO companies (corp_code, sector) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE sector = VALUES(sector)",
                    (entry.corp_code, sector),
                )
            print(f"✓ {name}({stock_code}) → corp_code {entry.corp_code} [{market}/{sector}]")

    if not args.dry_run:
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM companies")
            print(f"\ncompanies 총 {cur.fetchone()[0]}개 — 백필: python scripts/run_daily_scan.py --from 20230101")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
