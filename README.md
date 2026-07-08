# darfin-company-analysis

DART 정기공시(사업/반기/분기보고서) 기반 기업분석 파이프라인 워커입니다. DART Open API로 공시를 수집하고, XML을 파싱해 비교(diff)한 뒤 Gemini로 요약해 MySQL에 기록합니다. 조회 API는 별도 저장소 `darfin-main`(Spring)이 담당하며, 이 저장소는 HTTP 서버를 띄우지 않습니다 — 두 저장소는 MySQL을 통해서만 연결됩니다.

파이프라인 단계, 저장 전략, 구현 순서는 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)에 정리되어 있습니다. 작업 전 먼저 읽어주세요.

---

## 🛠️ 개발 환경

- **Language:** Python 3.11+
- **AI Model:** Gemini 2.5 Flash (via `google-genai` SDK)
- **DB:** MySQL/MariaDB (스키마는 `darfin-main/ddl.sql`)

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
GEMINI_API_KEY=your_actual_gemini_api_key_here
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=...
DB_PASSWORD=...
DB_NAME=darfin_dev
```

> ⚠️ `.env`는 API 키 등 민감 정보를 포함하므로 절대 커밋하지 마세요(`.gitignore`에 이미 포함).

---

## 🏃 파이프라인 실행

이 저장소는 서버를 실행하는 게 아니라 CLI 스크립트를 순서대로(또는 cron으로) 실행합니다. 각 스크립트는 `--stock <종목코드>`로 특정 회사만 대상으로 실행할 수 있습니다.

```bash
python scripts/ingest_filings.py --stock 005930     # 1. DART 수집 (RAW)
python scripts/parse_filings.py --stock 005930      # 2. XML 파싱 (PARSED)
python scripts/fetch_metrics.py --stock 005930      # 재무 수치 적재
python scripts/diff_filings.py --stock 005930       # 3. 비교(diff)
python scripts/build_overview.py --stock 005930     # company_overview 결정론적 부분
python scripts/extract_findings.py --stock 005930   # findings + score_history (LLM)
```

운영 환경에서는 두 개의 cron 작업이 이를 대체합니다:

- `scripts/run_daily_scan.py` — 하루 1회, 커버 대상 전체에 대해 수집~diff~결정론적 overview까지(Gemini 호출 없음).
- `scripts/run_llm_worker.py` — 1분마다, `llm_jobs` 대기열(사용자가 `darfin-main`에서 회사 상세를 조회할 때만 등록되는 on-demand 큐)을 큐가 빌 때까지 소비하며 findings/risks/insights를 Gemini로 생성.

자세한 트리거 조건과 상태 머신은 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)를 참고하세요.

---

## 📂 프로젝트 구조

```
├── dart_pipeline/      # 파이프라인 각 단계(수집/파싱/diff/LLM) 로직
├── dart_parser/        # DART XML → 구조화 데이터 파서
├── scripts/            # CLI 진입점 (수동 실행 + cron 대상)
├── data/                # DART 원본 XML/ZIP 캐시 (gitignore)
├── requirements.txt
└── IMPLEMENTATION_PLAN.md  # 설계 문서 (항상 최신 상태로 갱신)
```
