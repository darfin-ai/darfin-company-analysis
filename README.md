# darfin-company-analysis

DART 정기공시(사업/반기/분기보고서) 기반 기업분석 파이프라인 워커입니다. DART Open API로 공시를 수집하고, XML을 파싱해 비교(diff)한 뒤 Gemini로 요약·리스크 분석해 MySQL에 기록합니다. 조회 API는 별도 저장소 `darfin-main`(Spring)이 담당합니다 — 두 저장소는 MySQL을 통해서만 연결됩니다.

이 저장소는 `main.py`(FastAPI)를 통해 최소한의 프로세스 하나만 띄우는데, 이는 외부에 API를 제공하기 위해서가 아니라 **파이프라인 스케줄링을 프로세스 내부에 내장**하기 위해서입니다(아래 "파이프라인 실행" 참고). 조회 트래픽을 받는 서버가 아닙니다.

파이프라인 단계, 저장 전략, 구현 이력은 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)에 정리되어 있습니다. 작업 전 먼저 읽어주세요.

---

## 🛠️ 개발 환경

- **Language:** Python 3.11+
- **AI Model:** Gemini 2.5 Flash (via `google-genai` SDK)
- **DB:** MySQL/MariaDB (스키마는 `darfin-main/ddl.sql`)
- **스케줄링:** FastAPI(`main.py`) + APScheduler — 별도 외부 cron 없이 한 프로세스 안에서 일일 스캔과 LLM 워커 루프를 돌립니다.

---

## 🚀 시작 가이드

### 1. 가상환경 생성 및 의존성 설치

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 2. 환경 변수 설정

프로젝트 루트에 `.env` 파일을 생성합니다.

```ini
# .env
DART_API_KEY=your_actual_dart_open_api_key_here
GEMINI_API_KEY=your_actual_gemini_api_key_here
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=...
DB_PASSWORD=...
DB_NAME=darfin_dev
# 선택: DART 원본 XML/zip 캐시 경로 (기본: ./data)
# DATA_DIR=/path/to/cache
```

> ⚠️ `.env`는 API 키 등 민감 정보를 포함하므로 절대 커밋하지 마세요(`.gitignore`에 이미 포함).

---

## 🏃 파이프라인 실행

### 운영 환경

```bash
uvicorn main:app --workers 1
```

**반드시 워커 1개, `--reload` 없이** 실행합니다 — 두 번째 프로세스가 뜨면 스케줄러와 워커 루프가 중복 실행되어 DART API 호출과 LLM 잡이 두 배로 나갑니다. 이 한 프로세스가 내부적으로 두 가지를 계속 돌립니다(`dart_pipeline/scheduler.py`):

- **일일 스캔** (`scripts/run_daily_scan.py`, 06:00/18:00 KST cron) — 커버 대상 회사 전체에 대해 수집 → 파싱 → 재무제표/주요정보 워밍 → diff까지(1~3단계, Gemini 호출 없음).
- **LLM 워커 루프** (`scripts/run_llm_worker.py`, ~12초 주기 반복 호출) — `llm_jobs` 대기열을 소비. 이 큐는 사용자가 `darfin-main`에서 회사 상세/AI분석을 조회할 때만 등록되는 순수 on-demand 큐라, 아무도 안 보는 회사에는 매일 Gemini 비용이 들지 않습니다. 잡 종류:
  - `overview`/기본 상세 — findings/risks/panel insight 생성(`dart_pipeline/fast_path.py`)
  - `risk_analysis` — AI분석 탭용 리스크 내러티브 생성(`dart_pipeline/risk_analysis.py`)
  - `onboard_ingest` — 신규 관심기업 등록 시 5년치 즉시 backfill(`dart_pipeline/onboard_ingest.py`)

### 수동/단계별 실행 (로컬 개발·디버깅용)

각 스크립트는 `--stock <종목코드>`로 특정 회사만 대상으로 실행할 수 있습니다. 순서대로 실행하면 운영 파이프라인과 동일한 산출물이 만들어집니다.

```bash
python scripts/ingest_filings.py --stock 005930 --from 20230101   # 1. DART 수집 (RAW)
python scripts/parse_filings.py --stock 005930                    # 2. XML 파싱 (PARSED)
python scripts/warm_financial_facts.py --stock 005930             # 재무제표 캐시 워밍 (financial_facts)
python scripts/fetch_report_facts.py --stock 005930               # 배당/주주/임직원 등 주요정보 (report_facts)
python scripts/diff_filings.py --stock 005930                     # 3. 비교 (DIFFED)
python scripts/build_overview.py --stock 005930                   # company_overview 결정론적 부분 + risks(LLM)
python scripts/extract_findings.py --stock 005930                 # findings + score_history (LLM)
```

기타 유틸리티 스크립트:

- `scripts/seed_companies.py` — 커버 대상 기업 등록(KOSPI/KOSDAQ 시가총액 상위 30개사, 멱등).
- `scripts/validate_fixtures.py` — `dart_parser` 회귀 검증(고정 픽스처 대상, DART API 호출 없음).

자세한 트리거 조건과 상태 머신은 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)를 참고하세요.

---

## 🧪 테스트

```bash
python -m pytest tests/
```

`tests/fixtures/`의 고정 DART API 응답으로 순수 변환 로직(파서, `report_facts`, LLM 안전장치)을 오프라인 검증합니다.

---

## 📂 프로젝트 구조

```
├── main.py              # FastAPI 진입점 — 조회 API가 아니라 스케줄러 구동용 (uvicorn main:app)
├── dart_pipeline/       # 파이프라인 각 단계 로직
│   ├── client.py            # DART Open API 클라이언트
│   ├── ingest.py             # Stage 1: 수집 (RAW)
│   ├── parse_ingest.py       # Stage 2: 파싱 적재 (PARSED)
│   ├── financial_facts_ingest.py / report_facts*.py  # 재무제표/주요정보 캐시
│   ├── diff.py / diff_ingest.py               # Stage 3: 비교 (DIFFED)
│   ├── llm.py / llm_runtime.py / fast_path.py # Stage 4: LLM 요약 (findings/risks/insight)
│   ├── overview.py / overview_ingest.py       # company_overview 결정론적+LLM 패널
│   ├── risk_extraction.py / risk_narrative.py / risk_analysis.py  # AI분석 리스크 레이어
│   ├── onboard_ingest.py     # 신규 관심기업 즉시 backfill
│   ├── scheduler.py          # main.py에 내장되는 일일 스캔 cron + LLM 워커 루프
│   └── scoring.py / db.py / config.py 등
├── dart_parser/         # DART XML → 구조화 데이터 파서 (loader/parser/tables/canonical)
├── scripts/             # CLI 진입점 (수동 실행 + main.py 내장 스케줄러가 호출)
├── tests/               # pytest — 고정 픽스처 기반 오프라인 검증
├── data/                # DART 원본 XML/ZIP 캐시 (gitignore)
├── requirements.txt
└── IMPLEMENTATION_PLAN.md  # 설계 문서 (항상 최신 상태로 갱신)
```
