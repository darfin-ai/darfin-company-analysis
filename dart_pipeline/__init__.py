"""DART 정기공시 수집 파이프라인 (Stage 1).

사용법:
    from dart_pipeline import DartClient, ingest_company
"""

from .client import DartApiError, DartClient
from .ingest import ingest_company

__all__ = ["DartClient", "DartApiError", "ingest_company"]
