"""환경 설정 (.env). 저장소 루트의 .env를 읽는다."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

DART_API_KEY = os.environ.get("DART_API_KEY", "")
DART_BASE_URL = "https://opendart.fss.or.kr/api"


def _resolve_data_dir() -> Path:
    """DATA_DIR을 쓸 수 있는지 미리 확인한다.

    Render Persistent Disk(예: /var/data)를 프로비저닝하지 않고 DATA_DIR 환경변수만
    남아있으면 마운트되지 않은 경로라 쓰기 권한이 없어 매 요청마다
    PermissionError로 onboard_ingest job이 계속 실패한다(재시도해도 회복 안 됨).
    여기서 저장하는 건 DART corp_code 매핑/원본 zip처럼 다시 받아올 수 있는
    캐시성 데이터라(진짜 결과는 MySQL에 있음), 못 쓰면 컨테이너 로컬 경로로
    조용히 대체해 무한 실패 루프를 막는다.
    """
    configured = Path(os.environ.get("DATA_DIR", REPO_ROOT / "data"))
    try:
        configured.mkdir(parents=True, exist_ok=True)
        return configured
    except OSError as e:
        fallback = REPO_ROOT / "data"
        print(
            f"[config] DATA_DIR={configured} 쓰기 실패({e}) — {fallback}로 대체. "
            f"재시작 시 캐시가 초기화되니 영구 보존이 필요하면 Persistent Disk를 붙여주세요."
        )
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


DATA_DIR = _resolve_data_dir()
RAW_DIR = DATA_DIR / "raw"

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "3306")),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "database": os.environ.get("DB_NAME", "darfin"),
    "charset": "utf8mb4",
}
