# 기업분석 백엔드 구현 계획 (DART 정기공시 파이프라인)

> 이 문서는 `darfin-front`의 `/company` 기업분석 기능을 실제 데이터로 구동하기 위한
> 백엔드 파이프라인의 설계 문서입니다. 프론트엔드 목데이터 계약(`darfin-front/src/mocks/companyAnalysis/types.js`)을
> 기준으로 작성되었으며, 구현 진행 시 이 문서를 갱신합니다.

## 0. 전체 그림

```
DART Open API ──▶ [darfin-company-analysis (Python, CLI + cron)]      [darfin-main (Spring, :8080)]
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
| 개요 | `profile`, `mdnaHistory[]`(경영진 설명이 있는 filing 원문 아카이브 — LLM 미사용, 결정론적), `recentFilings[]`, `overview`(segments/products/customers/regions/risks/shareholders/dividend + 각 `insight` + `FilingExcerptRef`), `findings[]`(재무제표→주석→MD&A hop 체인) | 대부분 LLM 파생, 모든 주장에 원문 근거 필수 |
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
- 산출물: 서술형 diff의 before/after 요약 + `changeType`, `findings`(추론 체인), overview 패널별 `insight`, 그리드 카드용 `changeSummary` 한 줄, `mdnaHistory`(LLM 미사용, 결정론적), 4개 점수 컴포넌트
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

1. ~~**파서 먼저, 오프라인으로**~~ ✅ **완료** — `dart_parser/` 패키지 + `scripts/validate_fixtures.py`. 4개년 픽스처 전체 검증 통과 (표준 섹션 12/12 매핑, 연도 간 AASSOCNOTE 매칭 100%, 재무 수치 스팟체크 실제 공시값과 일치). 구현하며 확인된 DART XML의 실제 구조(문서화 안 된 함정들):
   - **파일이 well-formed XML이 아님**: `</DOCUMENT>` 뒤 중복 잔여물(최대 1.3MB), 이스케이프 안 된 `&`(파일당 수백 개), 깨진 UTF-8 바이트 → `loader.py`가 절단·치환 후 lxml recover 모드로 파싱, 전 과정 warnings로 기록
   - **섹션 분할 단위는 SECTION 컨테이너가 아니라 TITLE**: 한 컨테이너에 TITLE 여러 개 (두 번째부터는 가상 하위 섹션)
   - **LIBRARY는 투명 래퍼**: 사업의 내용 하위 섹션·재무제표가 그 아래에 숨어 있음
   - **재무제표 인코딩이 연도마다 다름 (4가지 형식 확인됨)**: ①2023(사업/반기/1분기) = 캡션 표 + 본문 표 쌍(TITLE 없음, ACODE 없음) → 캡션 표에서 가상 섹션 합성 + 라벨 기반 수치 추출, ②2024(+2023 3분기) = TITLE 있는 TABLE-GROUP이지만 ACODE 없음 → 라벨 기반 추출, ③2025~2026 = TE 셀에 ACODE/ACONTEXT/ADECIMAL 완비 → concept 기반 추출, ④2023 3분기(`20231114002109`)만 별도 = TITLE 있는 TABLE-GROUP + ACODE는 있으나 `concept|context|decimal|unit|` 형태로 한 속성에 압축(ACONTEXT/ADECIMAL 속성 자체가 없음) → `dart_parser/tables.py: parse_cell()`에서 `|` 포함 시 분해해 보정 (검증: `scripts/validate_fixtures.py` 스팟체크 3종 전부 실제 공시값과 일치)
   - **라벨 기반 추출 시 행 라벨이 연도마다 다를 수 있음**: 매출액 행이 보통 "매출액"/"수익(매출액)"이지만 `20240312000736`(2024.03 사업보고서, FY2023)은 같은 회사·같은 개념인데 "영업수익"으로 표기 — 스팟체크 정규식에 별칭으로 추가함(`scripts/validate_fixtures.py`). 라벨 기반 추출에 의존하는 한 이런 표기 편차가 더 나올 수 있음.
   - **ATOCID는 2023 파일에 없음** → 앵커는 AASSOCNOTE > breadcrumb 순으로 사용 (계획대로)
   - **ACONTEXT 접두사(C/P)로 당기/전기 판별** 가능, 라벨 기반일 때는 머리글의 제N기 번호로 판별
   - **미확인**: 위 ④(압축 ACODE)와 "영업수익" 라벨 편차가 삼성전자 2023 3분기 한정인지, 아니면 그 분기에 DART 시스템 전반의 인코딩이 바뀌어 다른 회사에도 나타나는지 아직 모름 — 현재 픽스처가 삼성전자 1개사뿐이라 회사별 차이와 분기별 차이를 구분할 수 없음. **다음 회사 확장(SK하이닉스 등) 때 확인할 것**: SK하이닉스 등 3~4개사의 2023 3분기(및 인접 분기) 공시를 받아 동일한 파이프 압축 ACODE·"영업수익" 라벨이 나타나는지 대조 — 나타나면 시기(그 분기의 DART 시스템 변경) 문제, 특정 회사에서만 나타나면 회사별 편차 문제로 파서 대응 방식이 달라짐(전자는 시기별 조건 분기, 후자는 라벨/포맷 감지를 더 일반화해야 함)
1-b. **수집 파이프라인 (Stage 1)** ✅ **완료** — `dart_pipeline/` 패키지(client / corp_codes / db / ingest) + `scripts/ingest_filings.py` CLI. 검증: 삼성전자 2023~2026 정기공시 14건(사업 4·반기 3·분기 7)을 라이브 API로 수집, MariaDB `filings` 기록(RAW), 전 건 파싱 스모크 12/12 통과, 재실행 시 전 건 skip(멱등). 구현 노트:
   - 발견은 `list.json` + `pblntf_ty=A` + `last_reprt_at=Y`(정정공시는 최종본만), `reprt_code`/`bsns_year`는 `report_nm`의 "(YYYY.MM)"에서 추론
   - 문서 zip에서 본문 XML 선택: `{rcept_no}.xml` 우선, 없으면 최대 크기 `.xml`
   - 공시 1건 = 커밋 1건 (중단돼도 완료분 보존), 한 건 실패가 배치를 막지 않음
   - `dart_parser/loader.py`에 인코딩 스니핑 추가 (구형 문서 EUC-KR 대비)
   - corpCode.xml은 `data/corp_codes.zip`에 24시간 캐시
   - **미구현(5번에서)**: 일일 폴링 스케줄러
1-c. **파싱 결과 적재 (Stage 2 PARSED)** ✅ **완료** — `dart_pipeline/parse_ingest.py`(오케스트레이션) + `scripts/parse_filings.py` CLI. RAW filings의 `xml_path`를 `dart_parser`로 파싱해 `text_chunks`에 적재하고 `pipeline_status`를 PARSED로 갱신 — 1-b(수집)와 2(재무수치) 사이에 있었던 빈틈으로, diff 엔진(3번)이 텍스트/구조형 비교를 하려면 이 단계가 선행되어야 함. 이미 받아둔 XML을 대상으로 하므로 **DART API 호출 없이 완전히 오프라인으로 동작**(corp_code 조회용 corpCode.xml 캐시만 필요). 검증: 삼성전자 14건 전체 파싱 → `text_chunks` 1,584행 적재, 재실행 시 기본은 skip·`--force`로 재파싱해도 동일 결과(멱등), Q3 2023 파일도 표준 섹션 12/12가 `canonical_label`로 정상 보존됨, 연도 간 `assoc_note` 매칭도 DB 조회로 재확인(2025 3분기→2025 사업보고서 46건 일치). 구현 노트:
   - `section_title`/`breadcrumb`는 각각 VARCHAR(200)/(500)이라 truncate — 실측 최대값은 738자/784자(주석 섹션 중 일부는 TITLE 자리에 문단 전체가 들어오는 사례가 있어 예상보다 김). 표시용 라벨이 아니라 앵커 매칭(`assoc_note`/`atocid`)이 우선이라 truncate 자체는 문제 없음
   - `tables_json`은 표가 있는 섹션만 채움(NULL 허용), 실측 최대 크기 ~1.5MB — DB `max_allowed_packet`(16MB) 내로 여유 있음
   - **이번에 발견**: 로컬 개발 DB(`darfin_dev`)의 `filings` 테이블이 비어 있었음(1-b에서 라이브 수집했다는 기록과 불일치 — 이후 DB가 리셋된 것으로 보임). 이 환경(네트워크 이슈로 라이브 수집 재실행 불가)에선 로컬 XML 자체의 메타데이터(`rcept_no` 앞 8자리=접수일, `parse_filing()`의 `period_to`/`doc_acode`)로 `filings`를 재시딩해 테스트함. 단, 분기보고서는 XML 내부 `DOCUMENT-NAME ACODE`가 1분기/3분기 구분 없이 항상 "11013"이므로(`ingest.py: classify_report()`가 API의 `report_nm` 월로 구분하는 것과 같은 이유) `period_to` 종료월(03→11013, 09→11014)로 직접 보정 — 라이브 수집 경로(`ingest_company`)는 이미 `report_nm` 기반이라 이 문제 없음
2. `fnlttSinglAcntAll`로 `metrics` 적재 → **재무 추이 탭 end-to-end 연결** (LLM 불필요 — 가장 싼 풀스택 성과) ✅ **완료, 라이브 검증됨** *(2026-07-13 `metrics` 테이블 폐기 — `financial_facts` 단일 소스로 이관, §6 마지막 변경 내역 참고)* — `dart_pipeline/metrics.py`(순수 변환, XML 무관) + `metrics_ingest.py`(오케스트레이션) + `scripts/fetch_metrics.py` CLI. 연결(CFS)·별도(OFS) 각각 조회해 저장. 구현 노트:
   - 손익/현금흐름 항목의 분기 이중 열(당기 3개월 vs 누적)은 `thstrm_amount`/`thstrm_add_amount` 두 필드를 `period_qualifier`로 구분해 별도 행으로 저장
   - `account_id`가 `-표준계정코드 미사용-`이면 `concept=None`으로 저장 (라벨만 있는 계정)
   - 자본변동표(SCE)는 12개 표준 섹션에 대응이 없어 저장 대상에서 제외
   - 멱등성은 다른 단계와 동일하게 delete-then-insert (재실행 시 해당 rcept_no의 metrics를 지우고 다시 채움) — `dart_pipeline/db.py: delete_metrics/insert_metrics`, `darfin_dev`(로컬 개발 DB)에서 오프라인 단위 검증(변환 로직 + 재실행 시 행 수 불변) 완료
   - **라이브 검증 완료**: 네트워크 이슈(이전엔 `opendart.fss.or.kr` 커넥션 리셋) 해소됨. `python scripts/fetch_metrics.py --stock 005930`으로 삼성전자 14개 filings 전체 적재 → `metrics` 3,564행. 스팟체크: FY2023 사업보고서(`20240312000736`) 연결 자산총계 455.9조/자본총계 363.7조/부채총계 92.2조 — 실제 공시값과 일치. 이후 `diff_filings.py --stock 005930 --force`로 numeric diff 49건 생성 확인(3번의 "알려진 한계 ①" 해소) — Q3 2023 영업이익 QoQ 급증(6,685억→2조4,335억) 등 실제 반도체 업황 반등과 일치.
   - **부수 효과**: diff `--force` 재실행으로 13개 filings의 `pipeline_status`가 `SUMMARIZED`→`DIFFED`로 되돌아감(section_diffs 행 id가 바뀌어 이전 llm_summaries와의 대응이 끊김). `scripts/summarize_filings.py --stock 005930` 재실행 시 SUMMARIZED로 복구되나 Gemini API 재호출 비용 발생 — 사용자 판단으로 보류 중, 다음 작업 시 처리 필요
2-b. **DART 주요정보 API → `report_facts` (Tier 1)** ✅ **완료** — `dart_pipeline/client.py: report_api()`(10종 엔드포인트 공통 호출, 013→`None`, 020→예외) + `report_facts` 테이블(`ddl.sql`, corp×year×reprt×api_id 키, 013은 `payload_json=NULL` negative cache) + `dart_pipeline/report_facts.py`(순수 변환: `dividend_panel`/`shareholders_panel`/`headcount_metrics`/`ownership_metrics` — 기존 `overview.py`/`diff.py` 소비 형태와 동일) + `report_facts_ingest.py` + `scripts/fetch_report_facts.py` CLI. 소비자 연결: `overview_ingest`/`fast_path`의 dividend·shareholders는 `report_facts`가 있으면 API 경로, 없으면 legacy `tables_json` 추출(로그로 커버리지 측정). `diff_ingest`의 headcount/ownership은 **양쪽 filing 모두** report_facts가 있을 때만 API 경로, 아니면 legacy. `run_daily_scan.py`가 `fetch_metrics` 직후 `fetch_report_facts_for_stock` 호출(020 쿼터면 해당 회사 스킵·다음 run에서 이어서). 단위 테스트: `tests/test_report_facts.py` + 삼성/SK하이닉스 실 API fixture. **아직 범위 밖**: `dartOverview` 10종 전체를 Spring API로 내려주는 것(프론트는 DEV mock 병합), 자기주식/증자/감사의견 등 나머지 endpoint의 compose/transform.
3. 수치 + 해시 기반 diff 엔진 → 공시 변경 탭의 수치형 섹션 ✅ **완료 (재무 수치형은 metrics 적재 후 자동 활성화)** — `dart_pipeline/diff.py`(순수 비교 로직) + `diff_ingest.py`(오케스트레이션) + `scripts/diff_filings.py` CLI. 완전히 오프라인 동작(파싱된 text_chunks/metrics만 사용). 검증: 삼성전자 13건(baseline 있는 전체) diff → `section_diffs` 425행, `pipeline_status` DIFFED, 멱등(재실행 skip / `--force` 동일 결과). baseline 결정이 comparison.js 의미론과 일치함을 확인(1분기 QoQ→전년 사업보고서, 사업보고서 QoQ→3분기, YoY→전년 동유형; 2022는 사업보고서만 있어 2023년 공시들의 YoY 없음 — 정상). 구현 노트:
   - **분석 유형별 구현**: text/text_numeric/structural/event = content_hash 게이트 → 문단 단위 difflib으로 변경 구간 격리(표만 바뀐 주석 노트는 스킵 — 수치 churn 노이즈 방지), 하위섹션 추가/소멸 감지(1차 AASSOCNOTE+연결/별도 구분 키, 2차 번호 접두사 키로 제목만 바뀐 섹션을 modified로 승격). structural은 표 행 라벨 집합 비교 추가(계열회사 목록의 신규/제외 감지). headcount/ownership = tables_json에서 행 시그니처 기반 추출(colspan 정보가 저장되지 않아 열 위치 특정 불가 → 직원 합계는 소수점 셀(평균근속연수) 직전 정수, 지분율은 행의 마지막 소수점 수치 — 삼성전자 전 연도에서 실제 공시값과 일치 확인: DS 남 직원 53,520/미등기임원 1,015/삼성생명 지분율 8.51→8.41 등)
   - **분기 유량 vs 연간 유량 가드**: 손익/현금흐름은 기간 버킷(3M/누적)이 양쪽에서 같은 의미일 때만 비교 — reprt_code에 따라 period_qualifier NULL의 의미가 다름(1분기=3개월이자 누적, 사업=연간). 1분기 QoQ(baseline=사업보고서)는 재무상태표만 비교되고 손익/현금흐름은 비교 불가로 비움(합성 metrics로 4개 시나리오 단위 검증). 재무 수치는 핵심 계정 화이트리스트(BS 5·IS 4·CF 3, concept 우선 + account_nm 폴백)
   - **구조 개편 접기(collapse) 가드**: 한 표준 섹션 그룹에서 하위섹션 10개 이상·50% 이상이 미매칭이면 실제 공시 변경이 아니라 파서 분할 단위 차이(2023↔2024 XML 형식 전환에서 주석 63건씩 발생)로 보고 요약 엔트리 1개로 접음 — 전환 경계 노이즈 337건 → 실제 신규 51건으로 감소. 정상 상태(2025→2026) 연도 쌍은 5~9건 수준으로 유지
   - **저장 정책**: "변경 없음"은 저장 안 함 — 프론트 `groupDiffsBySection()`이 빈 (섹션, 기준) 쌍도 렌더링하므로 diff 행은 실제 변경만. before/after는 6,000자 클립(TEXT 컬럼·LLM 입력 예산)
   - **알려진 한계**: ①재무 수치형(numeric) 엔트리는 metrics 테이블이 비어 있어 아직 0건 — 2번의 라이브 검증(fetch_metrics) 후 `--force` 재실행하면 자동으로 채워짐. ②텍스트 섹션의 표만 바뀐 변경(부문별 매출 표 등)은 문단 diff 필터에 걸러짐 — LLM 단계에서 표 diff가 필요해지면 보완. ③structural modified가 분기마다 반복되는 기준일 문자열 변경("기준일: 2026년 3월 31일")도 포착 — 중요도 판단은 4번 LLM 단계의 몫
4. LLM 단계 (기존 `main.py`의 Gemini 스텁을 raw-text 엔드포인트에서 구조화된 diff 단위 호출로 확장) → 서술형 diff, findings, insights, scores, mdnaHistory(1단계 결정론적 필드로 이동). **완료**. `main.py`는 이후 감사에서 완전히 고립된 죽은 코드로 확인돼 삭제됨(§구현 순서 참고).
   - **서술형 diff 폴리싱** ✅ **완료** — `dart_pipeline/summarize_ingest.py` + `scripts/summarize_filings.py` CLI. DIFFED filings의 narrative section_diffs before/after를 Gemini로 다듬어 `llm_summaries`에 적재(필터당 개별 호출 대신 한 공시의 diff 항목 전체를 배열로 묶어 한 번에 호출).
   - **findings + score_history** ✅ **완료** — `dart_pipeline/llm.py`(`extract_findings`) + `dart_pipeline/scoring.py`(순수 계산) + `dart_pipeline/findings_ingest.py`(오케스트레이션) + `scripts/extract_findings.py` CLI. 삼성전자 13건 전체 라이브 검증: `findings` 55행(severity high 15/medium 27/low 13, component financialChange 22/governance 18/managementEmphasis 12/riskEscalation 3), `score_history` 52행(13개 분기 × 4개 컴포넌트, 빠짐없음). 재실행 시 기본 skip, `--force`로 재생성해도 행 수 정합(중복 없음). 구현 노트:
     - **기계적 앵커링 원칙**: `hops_json`의 `sectionLabel`/`excerpt`/`sourceRef`는 전부 DB 원본 행(`section_diffs`/`text_chunks`)에서 코드가 채운다 — LLM은 evidence_id 참조로만 hop을 선택하고, 실제 텍스트는 절대 모델이 생성하지 않는다(`FilingExcerptRef`를 "기계적으로" 채운다는 §2 원칙 그대로 구현). LLM이 실제로 만드는 건 (1) evidence 그룹핑, (2) severity/scoreComponent 분류, (3) `summary` 헤드라인 한두 문장뿐.
     - **증거 카탈로그**: 이번 filing의 QoQ `section_diffs` 전체(재무상태표/손익계산서/현금흐름표의 numeric 행 포함 — `financial_anomaly` hop에 필요) + MD&A 텍스트 청크 1건(`이사의 경영진단 및 분석의견` — 12개 표준 섹션에 없어 diff 엔진이 다루지 않으므로 `text_chunks`에서 제목으로 직접 조회). `hop_type`은 `canonical_label`로 코드가 결정(재무제표 3종→`financial_anomaly`, 그 외 전부→`note`, MD&A 청크→`mdna`), LLM에 맡기지 않음.
     - **score_history는 findings 집계일 뿐 LLM 호출 없음**: severity 가중치(`high=1.0/medium=0.6/low=0.3`)를 `score_component`별로 합산 후 `min(maxPoints, sum × maxPoints/3)`로 스케일(대략 high finding 3개가 만점). `maxPoints`는 문서 초안의 "governance 0-10" 서술과 달리 실제 프론트 mock(`samsungElectronics.js`)엔 `financialChange=40/riskEscalation=30/managementEmphasis=20/governance=10`으로 돼 있어 이 값을 그대로 사용.
     - **버그 하나 발견·수정**: `text_chunks` MD&A 조회 쿼리의 `LIKE '%경영진단%'`가 pymysql의 `%s` 파라미터 치환과 충돌해 `not enough arguments for format string` 에러 발생 → `%%경영진단%%`로 이스케이프.
   - **company_overview** ✅ **완료** — `dart_pipeline/overview.py`(순수 파싱: segments/products/regions/shareholders/dividend) + `dart_pipeline/llm.py`(`extract_risks`/`generate_panel_insights` 추가) + `dart_pipeline/overview_ingest.py`(오케스트레이션, 시간순 baseline 체이닝) + `scripts/build_overview.py` CLI. 삼성전자 14건(2022 사업보고서 포함) 전체 라이브 검증: 전 건 `built`, segments/products/regions/dividend 수치를 실제 공시 원문과 手動 대조해 일치 확인(DX 39.3%/DS 61.0%, 메모리 74.78조, 미주 32.5%, 삼성생명 8.41%, 주당배당금 372원 등). 재실행 시 멱등(skip/`--force` 동일 행 수). 구현 노트:
     - **customers 패널은 빈 배열**로 고정 — DART는 고객사별 매출비중을 공시하지 않음(영업비밀 사항). 프론트 mock의 "고객사 A 18%" 류는 실제 공시 대응값이 없어 사용자 판단으로 이번 범위에서 제외.
     - **결정론 vs LLM 분리**: segments/products/regions/shareholders/dividend는 `tables_json`에서 코드가 순수하게 뽑는다(수치는 LLM이 절대 만들지 않음). `risks`만 프로즈뿐이라(위험요인 섹션에 구조화된 표 없음) LLM 추출이 필요한데, 이때도 findings와 동일한 기계적 앵커링 원칙을 재사용: 청크를 문단 단위로 쪼개 evidence_id를 부여하고, LLM은 evidence_id 참조로만 리스크를 묶는다(title/description/severity만 모델이 씀, excerpt/sectionLabel/sourceRef는 코드가 채움). 5개 결정론적 패널의 `*Insight`("So what?" 한 줄)만 LLM이 쓰되, 입력은 코드가 만든 결정론적 수치 요약 문자열이라 숫자 자체는 지어낼 수 없음.
     - **표 탐지가 위치가 아니라 헤더/라벨 키워드 기반**(diff.py의 headcount_metrics/ownership_metrics와 같은 방식): "구분|제NN기..." 헤더를 쓰는 표가 사업의 내용 섹션 안에 여러 개(매출유형별/지역별/품목별) 있어 헤더뿐 아니라 행 라벨 키워드 집합(`{내수,수출,미주,유럽,중국,아시아}`→지역, `{제ㆍ상품,용역,계}`→매출유형이라 스킵)으로 구분하고, 기간 열에 숫자가 없는 표(유형자산 롤포워드 등 무관한 "구분" 표)는 정규식으로 배제. 회사 확장 시 라벨 문구가 달라질 수 있어 재검증 필요.
     - **rowspan 붕괴 대응**: 배당 표("[주요 배당지표]")와 주주현황 표 둘 다 보통주/우선주 하위 행이 라벨 없이(`['보통주',...]`만) 나온다 — 직전 라벨/이름을 상태로 이어받는 방식으로 처리(`current_label`/`current_name` 추적).
     - **QoQ baseline 체이닝**: `diff.py`의 `order_filings()`/`resolve_baselines()`를 그대로 재사용해 segments의 added/existing, regions의 delta를 직전 filing의 이미 저장된 `company_overview`와 비교해 계산. 한 실행 내에서 방금 만든 overview는 메모리 캐시에 담아 다음 filing이 DB 재조회 없이 바로 참조(정정공시 등으로 순서가 바뀌어도 일관).
     - **실전에서 확인한 것**: Gemini 일시적 503(과부하) 하나 발생 → 해당 filing만 `failed`로 기록되고 나머지는 계속 진행(장애 격리 원칙대로 동작). 다만 그 filing이 다음 filing의 QoQ baseline이라 재시도로 채운 뒤 `--force` 전체 재실행이 필요했음(baseline이 없으면 그 다음 filing이 "최초 filing"처럼 취급돼 상태/delta가 부정확해짐) — 순서 의존 파이프라인의 알려진 특성.
     - **알려진 한계**: ①`dividend.history`는 배당 표에 있는 당기/전기/전전기 3개 시점만 채움(진짜 연도별 배당 추이가 아니라 분기 시점 값 그대로 — 완전한 연간 추이는 여러 filing에 걸친 분기 배당 합산이 필요해 범위 밖). ②`risks`의 `status`는 'existing'/'new'만 코드가 판정(정규화 title 비교)하고, 이전엔 있었지만 이번엔 사라진 리스크는 'removed'로 남기지 않고 그냥 빠짐.
   - **사업 변화 흐름 타임라인 — v1: strategyShifts(LLM 판정)** ⚠️ **이후 두 차례 재설계로 완전히 대체됨(최종 형태는 아래 mdnaHistory 참고, 이 항목은 과정 기록용으로 보존)** — 최초 구현은 `dart_pipeline/llm.py`(`detect_strategy_shifts`) + `dart_pipeline/strategy_shifts_ingest.py`(오케스트레이션) + `db.update_overview_strategy_shifts`. 다른 5개 LLM 작업과 축이 다르다: filing 1건 대 직전 baseline(QoQ) 비교가 아니라 **회사 전체 filing 역사를 한 번에** 보고 "사업의 중심축이 바뀐" 전환점을 최대 3개까지 찾는 질문이라, `run_llm_worker.py`가 그 회사의 밀린 filing을 다 처리한 뒤(`processed_count > 0`일 때만 — 새로 처리한 filing이 없으면 결과가 같으므로 Gemini 호출을 아낌) 마지막에 한 번만 호출해 **최신 filing**의 `company_overview.overview_json['strategyShifts']`에 patch한다(다른 필드는 안 건드림). 구현 노트:
     - **digest 구성 요소 3가지, 전부 이미 결정론적으로 존재하는 데이터**(LLM 호출 없이 만들 수 있음): filing별 재무 변동(`section_diffs`의 재무제표 3종 `metrics_json`), 사업부문 매출비중(`company_overview.segments`), 경영진 설명 발췌(`text_chunks`의 MD&A). 이 셋을 filing마다 한 텍스트 블록으로 렌더링해 전체를 한 번의 Gemini 호출에 통째로 넣는다.
     - **분기·반기보고서엔 진짜 MD&A가 없다는 걸 실측으로 발견**: "이사의 경영진단 및 분석의견"은 사업보고서(연간)에만 기재 의무가 있고, 분기/반기는 "기재하지 않습니다(사업보고서에 기재 예정)"라는 66자 정형 문구만 들어있다(삼성전자 실측: 사업보고서는 1만자 이상, 분기/반기는 전부 66자 동일 문구). 이 문구를 실제 서술로 착각해 digest에 넣으면 LLM이 모든 전환의 rationale을 이 문구 그대로 반복하는 걸 확인 — 길이(`_MDNA_MIN_LEN=200`)와 문구 매칭(`기재하지 않습니다`) 둘 다로 걸러 실제 서술이 있는 filing만 `has_management_rationale=True`로 표시.
     - **프롬프트 지시만으로는 위반 사례 발생 → 기계적 필터 추가**: "경영진 설명이 있는 filing만 evidence로 쓰라"고 지시해도 모델이 재무/비중 수치만 있는 filing을 근거로 삼는 경우가 실측에서 확인돼, `detect_strategy_shifts`가 응답을 받은 뒤 `evidence_filing_id`가 `has_management_rationale=True`인 filing 집합에 속하는지 다시 한번 코드로 검증(findings/risks의 evidence_id 검증과 같은 원칙 — 프롬프트는 품질을, 코드는 정답성을 보장).
     - **`db.mdna_chunk_for_filing` 버그도 이 김에 발견·수정**: 기존 쿼리가 `section_title LIKE '%경영진단%'`로 매칭했는데, 실제 서술은 최상위 헤더 청크("IV. 이사의 경영진단 및 분석의견", content 대개 빈 문자열)가 아니라 그 하위 청크("3. 재무상태 및 영업실적" 등, section_title엔 "경영진단"이 없음)에 들어있어 일부 filing(특히 최신 XML 형식)에서 빈 결과를 반환했다. `breadcrumb LIKE '%경영진단%'`로 바꿔 하위 청크까지 잡음 — findings/risks 추출도 이 헬퍼를 공유하므로 그쪽 품질도 같이 개선됨.
     - **프론트 버그 하나 발견·수정**: `BusinessEvolutionTimeline.jsx`의 `deriveNodes()`가 노드 `key`로 `shift.quarter`를 그대로 썼는데, 한 filing(사업보고서 1건)에서 서로 다른 전환 2개가 감지되면 같은 quarter를 가진 노드가 2개 생겨 React key 충돌 경고 발생(실측: 삼성전자 2025Q4 사업보고서에서 전환 2건 동시 감지) — `${quarter}-${index}`로 바꿔 유일성 보장.
     - **실전 검증(삼성전자, 최초 구현 시점)**: 4개 사업보고서(2022~2025)만 실제 MD&A 보유, 그중 2025 연간(2026.03 제출) 1건에서 전환 2건 감지(①DX→반도체·디스플레이 설비투자 집중, ②매출·영업이익 대폭 성장) — 둘 다 실제 재무 수치와 경영진 서술이 일치함을 수동 대조로 확인. SK하이닉스는 같은 방식으로 실행했으나 0건 감지(모델이 조건에 맞는 명백한 전환을 못 찾음 — 정상적인 결과, 프론트는 빈 배열이면 기존 "사업의 내용" 카드로 조용히 폴백).
     - **전체 재계산 → 누적 로그로 재설계(2026-07-07)**: 위 방식은 회사 전체 이력을 매번 통째로 Gemini에 넘겨 top-3 전환만 뽑았는데, 그 결과 재실행할 때마다(신규 공시 1건이 추가돼 `run_llm_worker.py`가 이 함수를 다시 부를 때마다) **같은 과거 이력을 놓고 매번 비결정적으로 재계산**되고 있었다 — 사용자가 "타임라인이 왜 2건뿐이냐"고 물어본 걸 계기로 실측하니, 동일한 삼성전자 이력에서 원래 검증 때는 2건이었던 게 이번 재실행에서는 1건으로 줄어들어 있었다(재계산 자체가 원인 — 데이터가 바뀐 게 아님). 그래서 `detect_strategy_shifts`(전체 이력 일괄 top-3 추출)를 `detect_strategy_shift_increment`(직전 baseline 대 신규 공시 1건 비교)로 교체하고, 이미 판정을 마친 filing은 다시 묻지 않도록 `company_overview.overview_json`에 커서 2개를 추가했다: `strategyShiftsEvaluated`(판정 완료 filing_id 목록)와 `strategyShiftsBaseline`(다음 비교에 쓸, 가장 최근까지 파악된 사업 중심축 서술 — 전환이 감지되면 그 결과로, 안 되면 신규 filing 자신의 facts로 매번 롤포워드). `dart_pipeline/db.py: update_overview_strategy_shifts()`가 이 셋(로그/커서/baseline)을 함께 patch.
     - **알려진 트레이드오프(위 재설계 직후 실측)**: 이 방식은 급격한 단일 전환(적자→흑자 반전, 신사업 급성장 등)은 잘 잡지만 **여러 해에 걸쳐 서서히 진행되는 변화(삼성전자의 DX→DS 매출비중 이동처럼 연간 수 %p씩 3년에 걸쳐 벌어진 경우)는 놓칠 수 있다** — 인접 두 filing만 놓고 "명백한 전환이냐" yes/no로 묻는 구조화된 판정이, 전체 이력을 한 번에 보고 "가장 두드러진 흐름을 골라라"고 열어놓고 묻던 이전 방식보다 보수적으로 반응하기 때문(origin 대 최신 필링을 직접 비교해도 마찬가지로 감지 안 됨 — 프롬프트 프레이밍 자체의 차이로 보임). 실측 결과 삼성전자·SK하이닉스 둘 다 타임라인이 완전히 비어버림(사용자가 실제 화면에서 확인).
     - **LLM 판정 자체를 폐기 → 결정론적 필링 아카이브로 재설계(2026-07-07, 같은 날 두 번째 재설계)**: 위 트레이드오프를 실측으로 확인한 사용자가 "판정을 아예 하지 말고, 실제로 있었던 filing들을 시간순으로 보여주고 사용자가 직접 클릭해서 MD&A 원문을 읽게 하자"고 방향을 바꿨다 — LLM이 "이게 의미있는 전환이냐"를 판단하는 것 자체를 없앤 것. `dart_pipeline/strategy_shifts_ingest.py`와 `llm.py`의 `detect_strategy_shift_increment`/`FilingDigest`/`StrategyShiftCandidate` 등을 전부 삭제하고, `dart_pipeline/overview.py: build_mdna_entry()`(순수 함수, LLM 호출 없음)로 교체 — 경영진 설명이 실제 있는 filing이면(길이·정형문구 필터는 기존과 동일) `{rceptNo, quarter, reportLabel, excerpt(원문 그대로, 요약 아님), sourceRef}` 엔트리 1건을 만든다. 이걸 `overview_ingest.py`의 **1단계(결정론적, Gemini 호출 없음)** 루프에 얹어 baseline 체이닝으로 누적한다(segments/regions와 동일한 패턴) — `run_llm_worker.py`(2단계, LLM)에서는 완전히 빠짐. 필드명도 `strategyShifts` → `mdnaHistory`로 개명(Java `CompanyDetailResponse.mdnaHistory`, 프론트 `MdnaHistoryEntry`), `BusinessEvolutionTimeline.jsx`에서 metrics 칩과 LLM rationale 텍스트를 제거하고 filing 원문 발췌(스크롤 가능한 박스)를 그대로 보여주도록 전면 재작성. 결과: 이 컴포넌트는 이제 **Gemini 호출이 전혀 없다** — 결정론적 1단계에서 즉시 채워지므로 `aiInsightsReady` 여부와 무관하게 항상 보임(재현율/정밀도 트레이드오프 자체가 사라짐).
     - **재설계 중 발견한 사고**: 새 필드를 이미 존재하는 회사 overview 행에 채우려고 `build_deterministic_overview_for_stock(..., force=True)`를 돌렸는데, 이 함수는 1단계 전용이라 `aiInsightsReady`를 항상 `False`로, `risks`를 항상 `[]`로 쓴다 — `--force`가 이미 2단계(findings/risks/insights)까지 끝난 필링 14건 전부를 1단계 상태로 되돌려버렸다. 삼성전자·SK하이닉스 둘 다 `run_llm_worker.py` 재실행으로 복구 필요(다만 `mdnaHistory`는 1단계 필드라 이 사고와 무관하게 정상 채워짐). 교훈: 이미 처리된 필링에 결정론적 필드 하나만 추가할 땐 전체 `--force` 대신 좁은 backfill이 필요.
5. DART 폴링 스케줄러 + 파이프라인 상태 머신; Spring 조회 엔드포인트; 프론트 목데이터 교체. **거의 완료** — Spring API/프론트 연동/스케줄러+큐까지 구현·라이브 검증됨.
   - **Spring 조회 API** ✅ **완료** (`darfin-main`) — `GET /api/v1/companies`, `GET /api/v1/companies/{corpCode}`. `entity/analysis`의 기존 JPA 엔티티(Metrics/TextChunks/LlmSummaries)가 실제 ddl.sql과 어긋나 있었고 `ddl-auto=update`라 Hibernate가 이 파이프라인 테이블을 건드릴 위험이 있어 JdbcTemplate로만 구현(검증 중 실제로 `text_chunks`에 컬럼이 하나 추가되는 걸 확인해 3개 엔티티를 ddl.sql에 맞게 고침). `getCompanyDetail()`은 존재하지 않는 corp_code에 깨끗한 404 반환(회사가 없는 경우 vs 파이프라인 미처리 경우를 프론트가 구분).
   - **프론트 실제 API 연결** ✅ **완료** (`darfin-front`) — `/company`, `/company/:id`의 목데이터 호출을 실제 API로 교체.
   - **스케줄러 + on-demand 큐 + 동시 LLM 호출 + 부분 렌더링** ✅ **완료** — 처음엔 "일일 스캔이 새 filing을 찾으면 자동으로 LLM 큐에 넣는다"로 만들었지만, 이러면 아무도 안 볼 회사까지 매일 Gemini 비용을 쓰게 된다는 지적(사용자)에 따라 **LLM 단계는 순수 on-demand로만 트리거하게 재설계**했다: `scripts/run_daily_scan.py`는 1~3단계(수집/파싱/재무제표/diff, LLM 없음)만 커버 대상 전체에 매일 실행하고 `llm_jobs`엔 아무것도 등록하지 않는다. `darfin-main`의 `GET /api/v1/companies/{corpCode}`가 `overview` 없는(=이 회사의 최신 filing이 아직 LLM 미처리) 회사를 조회할 때만 `llm_jobs`에 등록 — 이게 **유일한** 큐 등록 경로이고, 등록 경로가 하나뿐이라 우선순위 개념 자체가 필요 없다(단순 FIFO, `requested_at` 순). `scripts/run_llm_worker.py`(cron이 1분마다 호출)가 큐를 소비한다. 한 filing의 LLM 호출 4개(폴리싱/findings/risks/insights)는 `dart_pipeline/fast_path.py`에서 `ThreadPoolExecutor`로 동시 실행(서로 입출력이 안 겹쳐서 가능, 순차 대비 대략 2~3배 빠름).
     - **`company_overview` 2단계 쓰기(`aiInsightsReady` 플래그)**: segments/products/regions/shareholders/dividend 수치는 `tables_json`에서 뽑는 결정론적 계산이라 LLM이 필요 없다 — 이걸 insight/risks(LLM 산출물)와 한 덩어리로 묶어서 쓰면 insight가 없는 동안 수치까지 같이 안 보이는 게 문제였다. 그래서 쓰기 자체를 분리: **1단계**(`overview_ingest.build_deterministic_overview_for_stock`, Gemini 호출 없음)가 수치만 계산해 `aiInsightsReady: false`로 저장 — 비용이 안 드니 `run_daily_scan.py`가 diff 직후 커버 대상 전체에 매일 실행. **2단계**(on-demand 큐 워커, `fast_path.process_filing_concurrent`)가 findings/risks/5개 패널 insight를 생성해 `db.update_overview_insights()`로 기존 행에 **UPDATE**(재작성 아님)하며 `aiInsightsReady: true`로 바꾼다. `filings_for_ai_insights()`가 2단계 대상(overview 자체가 없거나 `aiInsightsReady`가 false인 filing)을 판정. 하위호환: 이전 세션에서 원자적 1회 쓰기로 완성된 행엔 `aiInsightsReady` 필드 자체가 없는데, `overview.aiInsightsReady !== false`로 판단하면(필드 없음=`undefined`) 자동으로 "완료"로 처리돼 백필 불필요.
     - **부분 렌더링(패널 단위)**: 처음엔 overview 유무로 6개 패널+findings를 통째로 하나의 스켈레톤으로 가렸는데, "수치 자체는 이미 있는데 왜 안 보여주냐"는 지적(사용자)에 따라 **패널별로 쪼갰다**: 6개 패널(`ShareholderPanel`/`DividendPanel`/`BusinessSegmentPanel`/`ProductRevenuePanel`/`CustomerRegionPanel`/`KeyRisksPanel`)은 `overview`가 있으면 **항상 렌더링**되고, 그 안의 "So what?" 문단만 `isAiReady(overview)`(`lib/aiStatus.js`, `overview?.aiInsightsReady !== false`)가 false일 때 `Skeleton` 1~2줄로 대체(같은 레이블 유지, 내용만 스켈레톤). `KeyRisksPanel`만 예외 — `risks` 배열 자체가 100% LLM 산출물이라 부분 표시가 불가능해서 패널 전체를 스켈레톤 카드로 대체. `findings`(AI 분석 근거, `ReasoningChainFeed`)도 통째로 LLM 산출물이라 `!isAiReady(overview)`면 스켈레톤 카드 3개로 대체. 폴링 조건도 `!detail.overview` → `!isAiReady(detail.overview)`로 바뀌어, 1단계만 끝난 새 행은 계속 폴링하고 이미 완료된 옛 행(필드 없음)은 폴링하지 않는다.
     - **실전 검증 중 발견한 버그 2개**(둘 다 SK하이닉스로 파이프라인을 처음부터 다시 돌리며 발견 — 사용자가 "버그 없는지 확인해보자"고 요청):
       1. `stock` 테이블에 SK하이닉스 이름/티커로 등록된 행의 `dart_corp_code`가 실제로는 현대차의 corp_code였음(둘 다 이 세션 이전부터 있던 기존 문제, 원인 불명 — 수동 시딩으로 추정). 데이터 삭제 후 실제 SK하이닉스 corp_code(00164779)로 재수집해 해결.
       2. `polish_diff_entries` 등 4개 LLM 호출 전부 `thinking_config` 없이 호출되고 있었는데, 20건 배치 하나가 Gemini 2.5 Flash의 thinking 토큰만 244초 태우다 `MAX_TOKENS`로 잘려 파싱 실패 — `thinking_config=ThinkingConfig(thinking_budget=0)`으로 끄고 `max_output_tokens`도 모델 실제 한도(`client.models.get()`으로 확인한 65536)까지 올려 해결. thinking을 끄면서 지연시간도 추가로 줄어듦(부수 효과).
     - **알려진 한계**: `llm_jobs`가 15분 넘게 'running'이면 방치된 것으로 보고 다시 집어가지만(워커 크래시 대비), 정교한 재시도 백오프는 없음. `darfin-main`/파이프라인이 아직 서로 다른 DB(`darfin`/`darfin_dev`)를 보는 문제는 미해결 — 실제 배포 전 통일 필요. `BusinessEvolutionTimeline`(profile 기반)은 overview 없으면 빈 문자열로 조용히 비어 보임(별도 스켈레톤 없음 — 사소한 러프엣지로 남김).
     - **우선순위 큐 제거 + 워커 지연시간 단축(2026-07-07)**: 등록 경로가 on-demand(`darfin-main`의 클릭 시 조회) 하나뿐이라는 게 이미 사실이었는데도 `llm_jobs.priority` 컬럼과 "더 급한 쪽으로 승격" 로직이 남아있었다 — 실사용 경로에선 항상 같은 값(0)만 쓰여 아무 효과가 없는 죽은 코드였음. `priority` 컬럼(`ddl.sql`, 기존 DB엔 가드된 마이그레이션으로 제거)과 `dart_pipeline/db.py`의 `enqueue_llm_job()`(호출하는 곳이 전혀 없었음 — 실제 등록은 `CompanyAnalysisService.enqueueOnDemandJob()`이 SQL을 직접 재구현해서 씀)을 삭제하고 `claim_next_job()`은 `requested_at` 순 FIFO로 단순화. 더 중요한 변경은 워커 자체: `run_llm_worker.py`가 예전엔 cron 호출당 job 1개만 처리하고 종료했는데(cron 주기 1~2분), 프론트 폴링 창(12초 × 10회 = 최대 2분, `CompanyDetailPage.jsx`)과 거의 겹쳐서 클릭 직후 큐에 올라간 job이 다음 cron 틱까지 최대 2분을 기다리면 폴링이 이미 끝나버릴 수 있었다. `run_llm_worker.py`를 "호출 시점부터 50초(`TIME_BUDGET_SECONDS`) 동안, 혹은 큐가 빌 때까지 계속 다음 job을 이어서 처리"하도록 바꾸고 cron 주기를 1분으로 단축 — 클릭 직후 등록된 job이 대기하는 시간이 사실상 다음 cron 틱(최대 1분)으로 줄어 프론트 폴링 창 안에서 끝날 확률이 크게 올라간다. (동시에 여러 cron 인스턴스가 겹쳐 돌아도 `claim_next_job`의 `FOR UPDATE` row lock 덕에 서로 다른 job을 집어가지 서로 침범하지 않음.)
     - **죽은 코드 정리(2026-07-07)**: `main.py`(FastAPI, `/api/analyze` 엔드포인트 하나만 있는 초기 프로토타입)는 이 저장소의 다른 어떤 코드에서도 import/호출되지 않는 완전히 고립된 파일이었음 — 실제 파이프라인은 처음부터 CLI 스크립트 + cron 구조였고 HTTP 서버가 필요했던 적이 없다. 삭제하고 `requirements.txt`에서 `fastapi`/`uvicorn`도 제거(둘 다 `main.py` 말고는 쓰는 곳 없음 — `pydantic`은 `dart_pipeline/llm.py`가 실사용하므로 유지). `README.md`도 이 프로토타입 기준으로 작성돼 있어 CLI 스크립트 구조에 맞게 다시 씀. `.env.prod.bak`(추적 안 되는 스트레이 파일)도 함께 삭제. `dart_pipeline/summarize_ingest.py`(`scripts/summarize_filings.py`)는 겉보기엔 `fast_path.py`로 대체된 것 같지만, `darfin-main`의 그리드 카드 `changeSummary`가 이게 만드는 `llm_summaries` 테이블을 유일하게 읽는 소스라 **삭제하지 않음** — 다만 `run_daily_scan.py`/`run_llm_worker.py` 어디에도 자동으로 연결돼 있지 않아 새 filing의 `changeSummary`가 채워지려면 이 스크립트를 수동으로 돌려야 하는 상태(기존에도 있던 한계, 이번에 재확인).
     - **changeSummary 소스 교체 + summarize 스테이지 삭제(2026-07-07, 두 번째 정리)**: `darfin-main`의 그리드 카드 `changeSummary`가 `llm_summaries` 아무 1건을 읽던 임시 로직 탓에 '주식 사항' 섹션의 날짜 나열 원문이 카드에 그대로 노출되는 문제가 있었음 — `CompanyAnalysisService.latestChangeSummary()`를 최신 filing의 findings 중 최고 심각도 summary를 읽도록 교체(비어 있으면 빈 문자열, 프론트가 대체 문구 표시). 이로써 `llm_summaries`를 읽는 코드가 전무해져, 위 항목에서 "유일한 소비자 때문에 삭제하지 않음"이라 판단했던 `dart_pipeline/summarize_ingest.py`+`scripts/summarize_filings.py`를 삭제(어느 워커에도 연결 안 된 수동 스크립트였음). `llm_summaries` 테이블과 기존 데이터는 그대로 둠(스키마 변경 없음). 같은 날 `darfin-main`의 `entity/analysis` JPA 엔티티 5개(Companies/Filings/LlmSummaries/Metrics/TextChunks)도 삭제 — 어디서도 import되지 않는 데다 `ddl-auto=update` 아래에서 아래 ①번 스키마 드리프트를 일으킨 바로 그 원인이었음. 파이프라인 테이블 조회는 전부 JdbcTemplate로 유지. 재무 추이 API(`financials()`)도 같은 날 재작성 — 기존엔 account_nm만으로 묶어 동명 계정(손익 '당기순이익' vs 현금흐름 '당기순이익', 재무상태 '비지배지분' vs 포괄손익 '비지배지분')이 한 시계열에 섞여 톱니 차트가 됐고, 사업보고서의 연간 총액이 분기 축에 그대로 올라가 매 Q4가 4배 스파이크로 보였음. (statement_type, 정규화된 account_nm)로 묶고(표기 변형 '수익(매출액)'/'(손실)' 접미/분기·반기순이익/공백·각주 마커 접기), 손익은 3개월 행 우선 + 연간은 Q4=연간−3분기누적으로 환산, 현금흐름은 누적→분기 차분으로 환산. 프론트(`FinancialTrendCharts`)는 주요 9개 지표만 기본 노출하고 나머지는 토글로 접음. → 같은 날 2차 개편: `FinancialMetric`에 `statementType`(재무상태표/손익계산서/현금흐름표) 추가, 배열은 (재무제표 장 순서, 원문 내 계정 ord)으로 정렬해 내려줌(`types.js` 갱신). 프론트는 주요 지표 차트 대시보드 + 재무제표별 탭의 스파크라인 행 목록(행 클릭 시 차트 확장, 계정명 검색)으로 재구성 — 전체 차트 일괄 렌더링(구 `FinancialTrendTable` 포함) 제거. `fnlttSinglAcntAll`의 `ord` 필드를 `metrics.ord`에 저장하고 `fetch_metrics.py --force`로 재적재 완료(삼성전자·SK하이닉스 28 filings, 라이브 API에서 ord 100% 확인).
     - **`darfin` DB를 처음부터 재시딩하며 발견한 스키마 드리프트 2건**(사용자가 "darfin db에도 실제 데이터를 채워보자"고 요청 — pipeline/companies/filings/text_chunks/metrics/section_diffs/llm_summaries/findings/company_overview/score_history/llm_jobs를 TRUNCATE 후 `ingest_filings.py`→`run_daily_scan.py`로 처음부터 재수집): ①`text_chunks.section_nm` 컬럼이 `ddl.sql`에 없는데도 `darfin`에 NOT NULL로 남아있어 모든 filing이 파싱 단계에서 즉시 FAILED — `darfin-main`을 예전에 `ddl-auto=update`로 `darfin`에 대고 띄웠을 때 생긴 Hibernate 드리프트(같은 버그가 이전 세션엔 `darfin_dev`에서 발생했었는데 이번엔 `darfin`에도 있었음 — 두 DB 모두 이 위험에 노출됐다는 뜻). `ALTER TABLE text_chunks DROP COLUMN section_nm`으로 제거. ②`metrics.concept`이 `VARCHAR(100)`으로 `ddl.sql`의 `VARCHAR(300)`보다 좁아 재무제표 저장이 전부 `Data too long` 실패 — `ALTER TABLE metrics MODIFY COLUMN concept VARCHAR(300)`으로 맞춤. 두 경우 다 `darfin`이 `ddl.sql` 최신본으로 한 번도 제대로 재생성된 적이 없다는 신호 — 근본 해결은 `darfin`/`darfin_dev` 통일과 함께, `ddl.sql`을 소스오브트루스로 주기적으로 재적용하는 절차 마련.
     - **`metrics` 테이블 폐기 — 재무 데이터 소유권을 darfin-main으로 이관(2026-07-13)**: 재무 추이 서빙이 `metrics`(파이프라인 배치) + `financial_facts`(darfin-main 라이브 read-through) 이중 소스로 병합되던 구조를 `financial_facts` 단일 소스로 통일. 방향: **Python은 재무 데이터 파이프라인이기를 그만두고 순수 LLM 분석 워커로, DART 데이터 서빙은 전부 darfin-main이 소유**. 변경 내역 — ①파이프라인: `metrics_ingest.py`/`scripts/fetch_metrics.py` 삭제, 후신은 `financial_facts_ingest.py`/`scripts/warm_financial_facts.py`(fnlttSinglAcntAll 원본 rows를 가공 없이 `financial_facts`에 upsert — `report_facts`와 동일한 dual-writer 관례, darfin-main `FinancialFactDao`와 컬럼·negative cache 의미론 동일). `run_daily_scan.py`의 해당 스테이지 교체. ②diff의 수치형 입력: `diff_ingest.py`가 `metrics` 테이블 대신 `financial_facts` payload를 읽어 `metrics.transform()`(순수 함수, 유지 — darfin-main `FinancialFactTransformer.java`와 로직 동기)으로 인메모리 변환. 캐시가 없으면(워밍이 쿼터 초과로 빠진 경우 등) 수치형 엔트리 없이 텍스트 diff만 생성(과거 "알려진 한계 ①"과 동일한 degrade). ③darfin-main: `CompanyAnalysisService.financials()`의 metrics 병합 제거(단일 소스), `FinancialFactsService`는 lookback(730일) 내 stale 기간만 refresh하되 **서빙은 캐시된 전체 기간**(`FinancialFactDao.allFinancialFacts`) — 온보딩/일일 스캔이 lookback 밖 과거 기간을 덥혀 두면 차트 히스토리가 그대로 보존된다. ④`ddl.sql`에서 `metrics` CREATE 제거(신규 설치엔 없음; 기존 DB의 테이블·데이터는 무해하니 그대로 둠, 원하면 수동 DROP). 기존 `metrics`의 과거 데이터는 마이그레이션하지 않는다 — `warm_financial_facts.py`(또는 다음 daily scan)가 filings 전체 기간을 `financial_facts`로 재적재하면 동일 데이터가 채워진다.
   - **범위 밖(다음 작업)**: 커버 대상 회사 확장(현재 삼성전자·SK하이닉스 2개사만).

이 순서의 이유: LLM 비용을 쓰기 전에 모든 단계를 실제 공시로 테스트할 수 있고, LLM은 변경된 구간에 대해서만 과금된다.

## 5.5 AI분석 리스크 레이어 (2026-07-14 신설, job_type='risk_analysis')

기업분석 AI분석 탭의 prescriptive 레이어 — 스키마는 `darfin-main/ddl.sql` §8
(`derived_metrics`/`risk_states`/`text_extractions`/`dossier_events`,
`llm_jobs.job_type` 추가). 전 종목 backfill 없음 — 재무추이와 동일한 on-demand.

역할 분담(결정론/LLM 분리 원칙, §2.1과 동일):
- **Layer 1(Java, `darfin-main`)**: `RiskAnalysisService`가 요청 시점에
  `financial_facts` read-through(5년 lookback)로 분기 시계열을 얻고
  `MetricsCalculator`(비율/Altman Z'/Piotroski F/DuPont/accruals/12Q 자기
  z-score, Q4=연간−3분기누적)와 `RiskStateMachine`(6개 카테고리 ×
  신규발생/악화/지속/개선/해소/정상/데이터부족, 8Q 미만 데이터부족)을 돌려
  `derived_metrics`/`risk_states`에 upsert. 정정공시로 수치가 5% 넘게 바뀌면
  `correction_material` dossier_event(결정론적). LLM 호출 없음.
- **Layer 2(이 저장소)**: `GET .../ai-analysis`가 내러티브 미준비 회사를
  `llm_jobs(job_type='risk_analysis')`에 등록 → `run_llm_worker.py`가
  dispatch → `dart_pipeline/risk_analysis.py`:
  1. `risk_extraction.py` — text_chunks(주석/사업의 내용/위험요인/지배구조/
     주주현황)에서 10개 카테고리 구조화 추출. **item_key가 핵심**: 직전
     공시의 키 목록을 프롬프트에 넣어 새 키 발명 대신 매칭을 강제 —
     분기 간 set-diff로 `item_appeared`/`item_disappeared` 이벤트 생성
     (우발부채가 해소 언급 없이 주석에서 사라지는 패턴 — 단일 공시 분석이
     구조적으로 못 잡는 신호). 모든 항목은 breadcrumb를 `source_section`으로
     기계 첨부(출처 표시 — 유사투자자문업 방어선).
  2. `risk_narrative.py` — 최신 분기 `risk_states` 행에 `narrative_ko`와
     `watch_next_ko`("차기 분기 확인 사항") 생성. 입력은 이미 계산된 상태와
     신호 스냅샷뿐 — LLM은 절대 계산하지 않는다. 출력은 전 사용자 동일
     (개인화 없음).
  quant 컬럼은 Java 소유, 이 워커는 text_signals/narrative/watch_next/
  llm_updated_at만 쓴다. filing 단위 멱등(replace).

운영 안전장치(2026-07-15): 모든 Gemini 호출은 공통 런타임을 통해 요청당
60초 타임아웃과 transient 오류(429/5xx/timeout) 1회 재시도를 적용하고,
작업별 출력 토큰 상한 및 내용 비노출 latency telemetry를 기록한다. polish diff는
5건, findings/risks와 risk extraction 섹션은 10건씩 처리하되 findings/risks는 전체 결과를 결정론적으로 중복 제거·
정렬한 뒤 최대 5건으로 제한한다. 최신 분기 6개 카테고리의 narrative/watch_next가
모두 최신이고 `llm_updated_at >= computed_at`일 때만 API가 `complete`를
반환한다. 일반 기업 상세 조회는 UI가 소비하지 않는 legacy `ai_insights` 잡을 더
이상 자동 등록하지 않는다.
실패 작업은 GET 조회로 암묵 재등록하지 않고, 열람권을 확인하는 명시적
`POST .../ai-analysis/retry`에서만 새 잡을 등록한다.

알려진 한계/열린 결정: 상태 임계값은 placeholder(`RiskStateMachine.Thresholds`,
팀 결정 대상), peer 백분위는 v1 제외(자기 12Q z-score만 —
`derived_metrics.peer_percentile_json` 예약), restatement_gap은 전기(frmtrm)
컬럼 미추출로 후속 과제, 주석 섹션은 청크당 20K/총 200K자 상한으로 절단,
수시공시는 범위 밖.

## 6. 참고 자료 위치

- 프론트 데이터 계약: `darfin-front/src/mocks/companyAnalysis/types.js`
- 비교(diff) 프레임워크 의미론: `darfin-front/src/app/features/company-analysis/lib/comparison.js`
- 점수 계산: `darfin-front/src/app/features/company-analysis/lib/scoring.js`
- 컴포넌트 단위 프론트 감사표: `darfin-front/company-page-audit.csv`
- 테스트 픽스처: `darfin-front/삼성전자 분기보고서/*.xml` (2023–2026 1분기보고서)
- DB 스키마: `darfin-main/ddl.sql` §7 (기업분석 파이프라인)
- API 클라이언트 패턴: `darfin-front/src/app/shared/api/apiClient.js`
