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
2. `fnlttSinglAcntAll`로 `metrics` 적재 → **재무 추이 탭 end-to-end 연결** (LLM 불필요 — 가장 싼 풀스택 성과) ✅ **코드 완료, 라이브 검증 대기** — `dart_pipeline/metrics.py`(순수 변환, XML 무관) + `metrics_ingest.py`(오케스트레이션) + `scripts/fetch_metrics.py` CLI. 연결(CFS)·별도(OFS) 각각 조회해 저장. 구현 노트:
   - 손익/현금흐름 항목의 분기 이중 열(당기 3개월 vs 누적)은 `thstrm_amount`/`thstrm_add_amount` 두 필드를 `period_qualifier`로 구분해 별도 행으로 저장
   - `account_id`가 `-표준계정코드 미사용-`이면 `concept=None`으로 저장 (라벨만 있는 계정)
   - 자본변동표(SCE)는 12개 표준 섹션에 대응이 없어 저장 대상에서 제외
   - 멱등성은 다른 단계와 동일하게 delete-then-insert (재실행 시 해당 rcept_no의 metrics를 지우고 다시 채움) — `dart_pipeline/db.py: delete_metrics/insert_metrics`, `darfin_dev`(로컬 개발 DB)에서 오프라인 단위 검증(변환 로직 + 재실행 시 행 수 불변) 완료
   - **미검증**: 이 환경(사용자 Mac)에서 `opendart.fss.or.kr` 자체가 네트워크 단에서 커넥션 리셋되는 문제로 실제 API 라이브 호출은 아직 못 함 (다른 기기/네트워크에선 정상 접속됨 — 이 Mac 특정 문제로 보임). 네트워크 이슈 해소 후 `python scripts/fetch_metrics.py --stock 000660` 로 라이브 검증 필요
3. 수치 + 해시 기반 diff 엔진 → 공시 변경 탭의 수치형 섹션 ✅ **완료 (재무 수치형은 metrics 적재 후 자동 활성화)** — `dart_pipeline/diff.py`(순수 비교 로직) + `diff_ingest.py`(오케스트레이션) + `scripts/diff_filings.py` CLI. 완전히 오프라인 동작(파싱된 text_chunks/metrics만 사용). 검증: 삼성전자 13건(baseline 있는 전체) diff → `section_diffs` 425행, `pipeline_status` DIFFED, 멱등(재실행 skip / `--force` 동일 결과). baseline 결정이 comparison.js 의미론과 일치함을 확인(1분기 QoQ→전년 사업보고서, 사업보고서 QoQ→3분기, YoY→전년 동유형; 2022는 사업보고서만 있어 2023년 공시들의 YoY 없음 — 정상). 구현 노트:
   - **분석 유형별 구현**: text/text_numeric/structural/event = content_hash 게이트 → 문단 단위 difflib으로 변경 구간 격리(표만 바뀐 주석 노트는 스킵 — 수치 churn 노이즈 방지), 하위섹션 추가/소멸 감지(1차 AASSOCNOTE+연결/별도 구분 키, 2차 번호 접두사 키로 제목만 바뀐 섹션을 modified로 승격). structural은 표 행 라벨 집합 비교 추가(계열회사 목록의 신규/제외 감지). headcount/ownership = tables_json에서 행 시그니처 기반 추출(colspan 정보가 저장되지 않아 열 위치 특정 불가 → 직원 합계는 소수점 셀(평균근속연수) 직전 정수, 지분율은 행의 마지막 소수점 수치 — 삼성전자 전 연도에서 실제 공시값과 일치 확인: DS 남 직원 53,520/미등기임원 1,015/삼성생명 지분율 8.51→8.41 등)
   - **분기 유량 vs 연간 유량 가드**: 손익/현금흐름은 기간 버킷(3M/누적)이 양쪽에서 같은 의미일 때만 비교 — reprt_code에 따라 period_qualifier NULL의 의미가 다름(1분기=3개월이자 누적, 사업=연간). 1분기 QoQ(baseline=사업보고서)는 재무상태표만 비교되고 손익/현금흐름은 비교 불가로 비움(합성 metrics로 4개 시나리오 단위 검증). 재무 수치는 핵심 계정 화이트리스트(BS 5·IS 4·CF 3, concept 우선 + account_nm 폴백)
   - **구조 개편 접기(collapse) 가드**: 한 표준 섹션 그룹에서 하위섹션 10개 이상·50% 이상이 미매칭이면 실제 공시 변경이 아니라 파서 분할 단위 차이(2023↔2024 XML 형식 전환에서 주석 63건씩 발생)로 보고 요약 엔트리 1개로 접음 — 전환 경계 노이즈 337건 → 실제 신규 51건으로 감소. 정상 상태(2025→2026) 연도 쌍은 5~9건 수준으로 유지
   - **저장 정책**: "변경 없음"은 저장 안 함 — 프론트 `groupDiffsBySection()`이 빈 (섹션, 기준) 쌍도 렌더링하므로 diff 행은 실제 변경만. before/after는 6,000자 클립(TEXT 컬럼·LLM 입력 예산)
   - **알려진 한계**: ①재무 수치형(numeric) 엔트리는 metrics 테이블이 비어 있어 아직 0건 — 2번의 라이브 검증(fetch_metrics) 후 `--force` 재실행하면 자동으로 채워짐. ②텍스트 섹션의 표만 바뀐 변경(부문별 매출 표 등)은 문단 diff 필터에 걸러짐 — LLM 단계에서 표 diff가 필요해지면 보완. ③structural modified가 분기마다 반복되는 기준일 문자열 변경("기준일: 2026년 3월 31일")도 포착 — 중요도 판단은 4번 LLM 단계의 몫
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
