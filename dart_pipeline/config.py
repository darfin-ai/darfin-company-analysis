"""환경 설정 (.env). 저장소 루트의 .env를 읽는다."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

DART_API_KEY = os.environ.get("DART_API_KEY", "")
DART_BASE_URL = "https://opendart.fss.or.kr/api"

DATA_DIR = Path(os.environ.get("DATA_DIR", REPO_ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "3306")),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "database": os.environ.get("DB_NAME", "darfin"),
    "charset": "utf8mb4",
}
