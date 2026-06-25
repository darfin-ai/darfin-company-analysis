# DART 공시 분석 및 요약 API 서버 (FastAPI & Gemini)

이 프로젝트는 DART (금융감독원 전자공시시스템) API를 통해 받은 기업 공시 원문을 **Gemini 2.5 Flash** 모델로 분석하고 핵심 내용을 요약하는 FastAPI 기반의 백엔드 서버입니다.

---

## 🛠️ 개발 환경

- **Language:** Python 3.13.9
- **Language:** Python 3.11+
- **Framework:** FastAPI
- **AI Model:** Gemini 2.5 Flash (via `google-genai` SDK)

---

## 🚀 시작 가이드

### 1. 프로젝트 복제
```bash
git clone https://your-repository-url.git
cd darfin-company-analysis
```

### 2. 가상환경 생성 및 활성화

프로젝트의 독립적인 실행 환경을 위해 가상환경을 설정합니다.

**Windows:**
```bash
python -m venv venv
venv\Scripts\activate
```

**macOS / Linux:**
```bash
python -m venv venv
source venv/bin/activate
```

### 3. 필수 라이브러리 설치

`requirements.txt` 파일에 명시된 의존성 패키지를 설치합니다.
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. 환경 변수 설정

프로젝트 루트 디렉터리에 `.env` 파일을 생성하고, Google AI Studio에서 발급받은 API 키를 추가합니다.

```ini
# .env
GEMINI_API_KEY=your_actual_gemini_api_key_here
```
> ⚠️ **주의**: `.env` 파일은 API 키와 같은 민감한 정보를 포함하므로, `.gitignore`에 추가하여 Git 저장소에 업로드되지 않도록 주의하세요.

---

## 🏃 서버 실행 및 테스트

### 1. 서버 실행

Uvicorn을 사용하여 FastAPI 서버를 실행합니다. `--reload` 옵션을 사용하면 코드 변경 시 서버가 자동으로 재시작됩니다.
```bash
uvicorn main:app --reload
```
서버가 시작되면 브라우저에서 `http://127.0.0.1:8000` 주소로 접속할 수 있습니다.

### 2. API 테스트

FastAPI가 자동으로 생성해주는 Swagger UI 문서를 통해 API를 직접 테스트할 수 있습니다.

혹은 postman과 같은 API 클라이언트를 사용하여 `/api/analyze` 엔드포인트를 호출할 수도 있습니다.

1.  `http://127.0.0.1:8000/docs` 로 접속합니다.
2.  `/api/analyze` 엔드포인트를 선택하고 `Try it out`을 클릭합니다.
3.  `raw_text` 필드에 분석할 DART 공시 원문을 입력합니다.
4.  `Execute` 버튼을 눌러 분석 결과를 확인합니다.

---

## 📂 프로젝트 구조
```
├── main.py             # FastAPI 애플리케이션 및 Gemini 연동 소스 코드
├── .env                # API 키 등 환경변수 관리 파일 (보안 주의)
├── requirements.txt    # 설치 패키지 목록 정의 파일
└── README.md           # 프로젝트 가이드 문서 (현재 파일)