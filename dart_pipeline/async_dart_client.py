"""Async DART Open API client (httpx)."""

from __future__ import annotations

import asyncio
import time

import httpx

from .client import DartApiError, _NO_DATA, _STATUS_MESSAGES
from .config import DART_API_KEY, DART_BASE_URL


class AsyncDartClient:
    def __init__(self, api_key: str = DART_API_KEY, delay_seconds: float = 0.3):
        if not api_key:
            raise ValueError("DART_API_KEY가 설정되지 않았습니다 (.env 확인)")
        self.api_key = api_key
        self.delay_seconds = delay_seconds
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self.delay_seconds - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    async def list_filings(
        self,
        client: httpx.AsyncClient,
        corp_code: str,
        bgn_de: str,
        end_de: str,
        pblntf_ty: str = "A",
    ) -> list[dict]:
        """기간 내 공시 목록 (페이지네이션 완료). last_reprt_at=Y — 정정공시 최종본."""
        items: list[dict] = []
        page_no = 1
        while True:
            await self._throttle()
            for attempt in range(4):
                try:
                    res = await client.get(
                        f"{DART_BASE_URL}/list.json",
                        params={
                            "crtfc_key": self.api_key,
                            "corp_code": corp_code,
                            "bgn_de": bgn_de,
                            "end_de": end_de,
                            "pblntf_ty": pblntf_ty,
                            "last_reprt_at": "Y",
                            "page_no": page_no,
                            "page_count": 100,
                        },
                        timeout=60.0,
                    )
                    if res.status_code >= 500:
                        raise httpx.HTTPError(f"HTTP {res.status_code}")
                    res.raise_for_status()
                    data = res.json()
                    break
                except (httpx.HTTPError, httpx.TimeoutException):
                    if attempt == 3:
                        raise
                    await asyncio.sleep(2**attempt)
            else:
                raise AssertionError("unreachable")

            status = data.get("status")
            if status == _NO_DATA:
                return items
            if status != "000":
                raise DartApiError(
                    status, data.get("message", _STATUS_MESSAGES.get(status, ""))
                )

            items.extend(data.get("list", []))
            if page_no >= int(data.get("total_page", 1)):
                return items
            page_no += 1

    async def report_api(
        self,
        client: httpx.AsyncClient,
        api_id: str,
        corp_code: str,
        bsns_year: str,
        reprt_code: str,
    ) -> list[dict] | None:
        """status 000 → list, 013 → None, 그 외 → DartApiError."""
        await self._throttle()
        for attempt in range(4):
            try:
                res = await client.get(
                    f"{DART_BASE_URL}/{api_id}.json",
                    params={
                        "crtfc_key": self.api_key,
                        "corp_code": corp_code,
                        "bsns_year": bsns_year,
                        "reprt_code": reprt_code,
                    },
                    timeout=60.0,
                )
                if res.status_code >= 500:
                    raise httpx.HTTPError(f"HTTP {res.status_code}")
                res.raise_for_status()
                data = res.json()
                status = data.get("status")
                if status == _NO_DATA:
                    return None
                if status != "000":
                    raise DartApiError(
                        status, data.get("message", _STATUS_MESSAGES.get(status, ""))
                    )
                return data.get("list", [])
            except (httpx.HTTPError, httpx.TimeoutException):
                if attempt == 3:
                    raise
                await asyncio.sleep(2**attempt)
        raise AssertionError("unreachable")
