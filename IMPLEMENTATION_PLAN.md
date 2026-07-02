# 기업분석 백엔드 구현 계획 (DART 정기공시 파이프라인)

> 이 문서는 `darfin-front`의 `/company` 기업분석 기능을 실제 데이터로 구동하기 위한
> 백엔드 파이프라인의 설계 문서입니다. 프론트엔드 목데이터 계약(`darfin-front/src/mocks/companyAnalysis/types.js`)을
> 기준으로 작성되었으며, 구현 진행 시 이 문서를 갱신합니다.

## 0. 전체 그림

```
DART Open API ──▶ [darfin-company-analysis (Python/FastAPI)]          [darfin-main (Spring, :8080)]
                   Stage 1 수집 → Stage 2 파싱 → Stage 3 비교(diff)     Stage 5 조회 API
                   → Stage 4 LLM 요약                                   (MySQL 읽기 전용)
                              │                                              ▲
                              └────────────── MySQL (darfin) ────────────────┘
```

- **darfin-company-analysis (이 저장소)**: 파이프라인 워커. DART 수집 → XML 파싱 → 비교 → LLM 요약을 수행하고 결과를 MySQL에 기록.
- **darfin-main (Spring)**: 프론트가 호출하는 조회 API (`/api/v1/...`, Bearer 토큰 — `darfin-front/src/app/shared/api/apiClient.js` 패턴).
- **주의**: `/disclosure` (수시공시 뷰어) 기능은 별도 파이프라인(`darfin-disclosure`, `disclosure`/`ai_summary_result`/`ai_analysis_item` 테이블)이며 이 문서의 범위가 아님. 이 파이프라인은 **정기공시(사업/반기/분기보고서)만** 다룬다.

## 1. 프론트엔드 데이터 계약 (무엇을 만들어야 하는가)

`darfin-front/src/mocks/companyAnalysis/types.js`가 사실상의 API 스펙이다. 페이지별 요구 데이터:

### `/company` (CompaniesGrid)
- 기업 목록: `id, name, ticker, sector, latestFilingType, latestFilingDate, changeSummary(한 줄 LLM 요약), marketCapRank/kosdaqRank`
- `scores`: 4개 점수 컴포넌트(financialChange 0-40, riskEscalation, managementEmphasis, governance 0-10)의 분기별 히스토리 — 카드의 변동 신호 dot 계산에 사용 (`lib/scoring.js: dominantScoreChange()`)
- 관심기업은 localStorage 전용 — 백엔드 불필요 (현재 기준)

### `/company/:id` (CompanyDetailPage) — 3개 탭

| 탭 | 필요 데이터 | 성격 |
|---|---|---|
| 개요 | `profile`, `strategyShifts[]`, `recentFilings[]`, `overview`(segments/products/customers/regions/risks/shareholders/dividend + 각 `insight` + `FilingExcerptRef`), `findings[]`(재무제표→주석→MD&A hop 체인) | 대부분 LLM 파생, 모든 주장에 원문 근거 필수 |
| 재무 추이 | `FinancialMetric[]`: `concept`(예: `ifrs-full_Revenue`), label, unit, 분기별 `series`(실제 원화 스케일) | 순수 수치, LLM 불필요 |
| 공시 변경 | `SectionDiffEntry[]`: 12개 섹션(`lib/comparison.js DIFF_SECTION_CONFIG`) × QoQ/YoY, 유형 `structural/text/numeric/text_numeric/headcount/ownership/event` | 수치 diff는 계산, 서술형 diff는 LLM |

### 계약상 핵심 제약 3가지
1. **모든 주장은 `sourceRef` 필수** — 발췌문 + 섹션 라벨(`SourceExcerptDialog`에 표시). 따라서 요약 후에도 파싱된 섹션 텍스트를 보존해야 한다.
2. **비교 기준(baseline)은 섹션×기준별로 다름** — QoQ = 직전 공시(1분기보고서의 QoQ는 전년 사업보고서), YoY = 전년 동분기. **분기 수치를 연간 수치와 같은 종류인 것처럼 diff하지 않는다.**
3. **"변한 것"만이 아니라 "검사한 것 전부"를 반환** — `groupDiffsBySection()`은 변경 0건인 섹션도 렌더링하므로 API는 전체 섹션 그리드를 반환해야 한다.

## 2. 파이프라인 단계 (filings.pipeline_status 상태 머신)

`RAW → PARSED → DIFFED → SUMMARIZED` (기존 ddl.sql의 enum에 `DIFFED` 추가, `STORED`는 `PARSED`에 흡수)

각 단계는 멱등(idempotent)하게: 재실행 시 해당 rcept_no의 기존 산출물을 지우고 다시 생성. 파서 개선 시 원본 XML에서 PARSED 이후 단계만 재실행 가능해야 한다.

### Stage 1 — 수집 (RAW)
- 스케줄 잡이 DART `list.json` 폴링: 커버 기업 대상, `pblntf_ty=A`(정기공시), reprt_code 11011(사업)/11012(반기)/11013(1분기)/11014(3분기)
- `document.xml` zip 다운로드 (기업당 3~5MB) → 디스크/오브젝트 스토리지에 저장, `filings` 행 삽입 (`xml_path`, status `RAW`)
- 원본 파일은 **절대 DB에 넣지 않는다** — 재처리 보험용으로 파일시스템에 보관

### Stage 2 — 파싱 (PARSED)
- XML을 `TITLE ATOC="Y"` / `SECTION-1/2` 계층으로 분할
- **`AASSOCNOTE` 코드가 연도 간 안정적인 섹션 매칭 키** (예: `D-0-2-0-0` = 사업의 내용). 삼성전자 2023~2026 샘플로 검증됨. 제목 텍스트 정규화는 보조 수단.
- 섹션별 저장: 서술 텍스트(테이블 마크업 제거), `content_hash`, 앵커(`ATOCID` + breadcrumb 경로 — `sourceRef`/`sourceLabel`용), 12개 표준 섹션 라벨로의 매핑
- **재무제표 수치는 XML 테이블 파싱 대신 DART `fnlttSinglAcntAll.json` API 사용** — account_id(IFRS concept), 당기/전기 금액이 이미 구조화되어 있음. XML 테이블 파싱(`TE ACODE` + `ADECIMAL` 스케일링: `Number(raw.replace(/,/g,'')) * 10 ** abs(ADECIMAL)`)은 본문에만 있는 표에 한정: 주주현황, 임원·직원 현황, 배당, 부문/지역별 매출.

### Stage 3 — 비교 (DIFFED)
- baseline 결정: QoQ = 직전 filing, YoY = 전년 동분기 (`comparison.js getFilingContext()` 의미론 그대로)
- 분석 유형별:
  - **numeric/headcount/ownership**: 저장된 수치 사실(facts)의 순수 계산 → `NumericDeltaMetric[]`. LLM 불필요.
  - **text/structural**: 먼저 `content_hash` 비교 (동일 → "변경 없음" 기록 후 스킵). 변경 시 문단 단위 diff(difflib)로 변경 구간 격리 → LLM 입력 후보.
  - **event**: 규칙 기반 감지 (신규 하위섹션 출현, 최대주주 변경 행 등)

### Stage 4 — LLM 요약 (SUMMARIZED)
- **비용 통제의 핵심: LLM은 공시 전문을 절대 보지 않는다. diff된 구간과 추출된 사실만 입력.**
- 산출물: 서술형 diff의 before/after 요약 + `changeType`, `findings`(추론 체인), overview 패널별 `insight`, 그리드 카드용 `changeSummary` 한 줄, `strategyShifts`, 4개 점수 컴포넌트
- 모든 출력은 어느 섹션 청크에서 나왔는지 인용하게 하여 `FilingExcerptRef`를 **기계적으로** 채운다 (모델 신뢰 X)
- 행마다 model name / tokens / cost 기록 (기존 `ai_summary_result` 테이블 패턴 복사)
- (rcept_no, section, baseline) 단위 캐시로 중복 호출 방지

### Stage 5 — 조회 API (darfin-main / Spring)
- `GET /api/v1/companies` — 목록 + 최신 scores + changeSummary
- `GET /api/v1/companies/{corpCode}` — `CompanyDetail` 형태 그대로 (또는 탭별 분리: `/financials`, `/diffs`)
- 프론트는 `mocks/companyAnalysis` import를 API 호출로 교체하기만 하면 됨

## 3. 저장 전략 (무엇을 언제 저장하는가)

| 계층 | 시점 | 내용 | 위치 | 규모 |
|---|---|---|---|---|
| 원본(Raw) | 수집 시 | XML/zip 원본 | 디스크/S3, 경로만 `filings.xml_path` | 기업당 연 4건 × ~4MB. 50개사 ≈ 연 800MB |
| 파싱(Parsed) | 파싱 시 | 섹션 서술 텍스트 + 앵커 + 해시, 수치 사실 | `text_chunks`, `metrics` | filing당 DB에 ~0.5–1MB (텍스트만) |
| 파생(Derived) | diff/LLM 시 | diffs, findings, overview, scores, 요약 | 신규 테이블 | 소량. API가 서빙하는 대상 |

- "전부 저장하면 너무 많다" 문제는 범위 설정으로 해소: **정기공시만** + **파일럿 유니버스(KOSPI 상위 30~50개사)**부터. 전체 상장사(~2,600)로 확장해도 원본은 연 ~40GB 수준의 오브젝트 스토리지 문제일 뿐, DB에는 텍스트와 파생 행만 들어간다.
- **Parsed 계층은 반드시 영구 보존** — 다음 분기 diff에 이번 분기 섹션 텍스트가 필요하고, `sourceRef` 발췌는 영원히 필요하다.
- Raw는 재처리 보험 (파서 개선 → 재다운로드 없이 PARSED 이후 재실행)

## 4. 스키마 변경 (darfin-main/ddl.sql §7 기준)

기존 골격(`companies`, `filings`, `text_chunks`, `metrics`, `llm_summaries`)에 다음을 반영:

- `filings.pipeline_status`: `DIFFED` 추가
- `text_chunks` 확장: `section_code`(12개 표준 섹션 라벨), `assoc_note`(DART 안정 코드), `atocid`/`breadcrumb`(sourceLabel용), `content_hash`
- `metrics` 확장: `reprt_code`/기간, 재무제표 종류(BS/IS/CF), `account_id`(IFRS concept — 프론트의 `concept` 필드), UNIQUE(corp_code, account_id, period) → 분기 시계열이 단순 쿼리가 되게
- 신규 테이블:
  - `section_diffs`: rcept_no, baseline_rcept_no, section_code, comparison_type(QoQ/YoY), analysis_type, change_type, before/after TEXT, metrics JSON, 원문 앵커
  - `findings` (+ hops는 JSON 컬럼 또는 별도 테이블): severity, score_component, summary, hop별 excerpt/anchor
  - `company_overview`: filing당 1행, 패널 데이터를 JSON으로 (LLM 스냅샷이므로 정규화 실익 없음)
  - `score_history`: corp_code, quarter, component, value

## 5. 구현 순서

1. **파서 먼저, 오프라인으로** — `darfin-front/삼성전자 분기보고서/`의 2023~2026 XML 4개가 완벽한 테스트 픽스처. 섹션 분할 + `AASSOCNOTE` 매칭 + 표 추출기를 연도 간 포맷 변화에 대해 검증.
2. `fnlttSinglAcntAll`로 `metrics` 적재 → **재무 추이 탭 end-to-end 연결** (LLM 불필요 — 가장 싼 풀스택 성과)
3. 수치 + 해시 기반 diff 엔진 → 공시 변경 탭의 수치형 섹션
4. LLM 단계 (기존 `main.py`의 Gemini 스텁을 raw-text 엔드포인트에서 구조화된 diff 단위 호출로 확장) → 서술형 diff, findings, insights, scores
5. DART 폴링 스케줄러 + 파이프라인 상태 머신; Spring 조회 엔드포인트; 프론트 목데이터 교체

이 순서의 이유: LLM 비용을 쓰기 전에 모든 단계를 실제 공시로 테스트할 수 있고, LLM은 변경된 구간에 대해서만 과금된다.

## 6. 참고 자료 위치

- 프론트 데이터 계약: `darfin-front/src/mocks/companyAnalysis/types.js`
- 비교(diff) 프레임워크 의미론: `darfin-front/src/app/features/company-analysis/lib/comparison.js`
- 점수 계산: `darfin-front/src/app/features/company-analysis/lib/scoring.js`
- 컴포넌트 단위 프론트 감사표: `darfin-front/company-page-audit.csv`
- 테스트 픽스처: `darfin-front/삼성전자 분기보고서/*.xml` (2023–2026 1분기보고서)
- DB 스키마: `darfin-main/ddl.sql` §7 (기업분석 파이프라인)
- API 클라이언트 패턴: `darfin-front/src/app/shared/api/apiClient.js`
