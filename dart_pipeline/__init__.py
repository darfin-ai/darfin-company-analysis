"""DART 정기공시 수집 파이프라인 (Stage 1).

사용법:
    from dart_pipeline import DartClient, ingest_company
"""

from .client import DartApiError, DartClient

__all__ = ["DartClient", "DartApiError", "ingest_company"]


def __getattr__(name: str):
    if name == "ingest_company":
        from .ingest import ingest_company

        return ingest_company
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
