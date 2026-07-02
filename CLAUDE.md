# darfin-company-analysis

DART 정기공시(사업/반기/분기보고서) 기반 기업분석 파이프라인 워커 (Python/FastAPI).
수집 → 파싱 → 비교(diff) → LLM 요약을 수행하고 MySQL에 기록하며, 조회 API는 `darfin-main`(Spring)이 담당한다.

**작업 전 반드시 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)를 읽을 것** — 파이프라인 단계, 저장 전략, 스키마, 구현 순서, 프론트엔드 데이터 계약이 정리되어 있다. 설계가 바뀌면 그 문서를 함께 갱신한다.

관련 저장소 (../ 기준):
- `darfin-front` — 프론트엔드. 데이터 계약은 `src/mocks/companyAnalysis/types.js`
- `darfin-main` — Spring 메인 API + DB 스키마 (`ddl.sql`)
- `darfin-disclosure` — 수시공시 파이프라인 (별도 기능, 이 저장소 범위 아님)
