import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
from google import genai
from google.genai import types

# .env 파일에서 환경변수 로드
load_dotenv()

app = FastAPI(
    title="DART Text Analyzer with Gemini",
    description="DART 공시 원문을 받아 Gemini 2.5 Flash로 분석하는 API 서버",
    version="1.0.0"
)

# 1. Gemini Client 초기화
# load_dotenv()를 통해 GEMINI_API_KEY가 환경변수에 등록되므로 별도 인자 없이 호출 가능합니다.
try:
    client = genai.Client()
except Exception as e:
    client = None

# 2. 데이터 요청 객체 정의 (Pydantic Model)
class AnalysisRequest(BaseModel):
    raw_text: str  # DART에서 받아온 정제된 원문 텍스트

# 3. 고정 분석 프롬프트
ANALYSIS_PROMPT = """
당신은 기업 공시 분석 전문가입니다. 
제공된 DART 공시 원문을 바탕으로 아래의 핵심 내용을 요약 및 분석해주세요.

[요구사항]
1. 주요 골자 및 핵심 내용 요약 (3줄 이내)
2. 기업에 미칠 잠재적 긍정적/부정적 영향 분석
3. 투자자가 주의 깊게 봐야 할 리스크 요인

반드시 제공된 텍스트만을 기반으로 사실에 입각하여 답변하세요.
"""

# 4. 분석 API 엔드포인트
@app.post("/api/analyze", summary="DART 원문 분석")
async def analyze_dart_text(request: AnalysisRequest):
    """
    DART 원문을 입력받아 Gemini 2.5 Flash 모델을 통해 분석 결과를 반환합니다.
    """
    if not os.environ.get("GEMINI_API_KEY"):
        raise HTTPException(
            status_code=500, 
            detail="Gemini API Key가 설정되지 않았습니다. .env 파일의 GEMINI_API_KEY를 확인하세요."
        )
        
    if not request.raw_text.strip():
        raise HTTPException(status_code=400, detail="분석할 원문 내용이 비어있습니다.")

    try:
        # 비동기(Async) 방식으로 Gemini API 호출 (FastAPI의 효율적인 이벤트 루프 활용)
        response = await client.aio.models.generate_content(
            model='gemini-2.5-flash',
            contents=request.raw_text,
            config=types.GenerateContentConfig(
                system_instruction=ANALYSIS_PROMPT,
                temperature=0.2,  # 일관성 있고 객관적인 분석을 위해 낮은 창의성 설정
            ),
        )
        
        return {
            "status": "success",
            "analysis_result": response.text
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gemini API 호출 중 오류 발생: {str(e)}")

# 루트 엔드포인트 (서버 헬스 체크용)
@app.get("/")
def read_root():
    return {"message": "DART Analysis Server is running."}