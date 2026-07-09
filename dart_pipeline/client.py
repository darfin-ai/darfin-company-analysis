"""DART Open API 클라이언트.

사용 엔드포인트:
  - corpCode.xml : 전체 기업 고유번호 매핑 (zip)
  - list.json    : 공시 목록 검색 (신규 공시 발견)
  - document.xml : 공시 원본 파일 다운로드 (zip)
  - fnlttSinglAcntAll.json : 재무제표 수치
  - report_api() : 정기보고서 주요정보 10종 (alotMatter, hyslrSttus, …)

일일 쿼터 20,000건 — 호출 사이에 짧은 지연을 두고, 일시 오류는 재시도한다.
파일 엔드포인트의 오류 응답은 zip이 아니라 XML로 오므로 매직 바이트(PK)로 구분.
"""

from __future__ import annotations

import re
import time

import requests

from .config import DART_API_KEY, DART_BASE_URL

# DART status 코드 중 "결과 없음"은 정상 흐름, 나머지는 오류
_NO_DATA = "013"

_STATUS_MESSAGES = {
    "010": "등록되지 않은 키",
    "011": "사용할 수 없는 키",
    "012": "접근할 수 없는 IP",
    "020": "요청 제한 초과 (일일 쿼터)",
    "100": "필드의 부적절한 값",
    "800": "시스템 점검 중",
}


class DartApiError(Exception):
    def __init__(self, status: str, message: str):
        self.status = status
        super().__init__(f"DART API error {status}: {message}")


class DartClient:
    def __init__(self, api_key: str = DART_API_KEY, delay_seconds: float = 0.3):
        if not api_key:
            raise ValueError("DART_API_KEY가 설정되지 않았습니다 (.env 확인)")
        self.api_key = api_key
        self.delay_seconds = delay_seconds
        self.session = requests.Session()
        self._last_call = 0.0

    def _get(self, endpoint: str, **params) -> requests.Response:
        # 호출 간 최소 간격 유지 + 일시 오류 재시도 (지수 백오프)
        for attempt in range(4):
            wait = self.delay_seconds - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                res = self.session.get(
                    f"{DART_BASE_URL}/{endpoint}",
                    params={"crtfc_key": self.api_key, **params},
                    timeout=60,
                )
                self._last_call = time.monotonic()
                if res.status_code >= 500:
                    raise requests.RequestException(f"HTTP {res.status_code}")
                res.raise_for_status()
                return res
            except requests.RequestException:
                if attempt == 3:
                    raise
                time.sleep(2**attempt)
        raise AssertionError("unreachable")

    def list_filings(
        self,
        corp_code: str,
        bgn_de: str,
        end_de: str,
        pblntf_ty: str = "A",  # A = 정기공시
    ) -> list[dict]:
        """기간 내 공시 목록 전체 (페이지네이션 처리 완료된 평탄한 리스트).

        last_reprt_at=Y — 정정공시가 있으면 최종본만 받는다.
        """
        items: list[dict] = []
        page_no = 1
        while True:
            data = self._get(
                "list.json",
                corp_code=corp_code,
                bgn_de=bgn_de,
                end_de=end_de,
                pblntf_ty=pblntf_ty,
                last_reprt_at="Y",
                page_no=page_no,
                page_count=100,
            ).json()

            status = data.get("status")
            if status == _NO_DATA:
                return items
            if status != "000":
                raise DartApiError(status, data.get("message", _STATUS_MESSAGES.get(status, "")))

            items.extend(data.get("list", []))
            if page_no >= int(data.get("total_page", 1)):
                return items
            page_no += 1

    def _get_zip(self, endpoint: str, **params) -> bytes:
        res = self._get(endpoint, **params)
        content = res.content
        if content[:2] != b"PK":  # 오류 응답은 zip이 아닌 XML
            m = re.search(rb"<status>(\d+)</status>.*?<message>([^<]*)</message>", content, re.S)
            if m:
                status = m.group(1).decode()
                raise DartApiError(status, m.group(2).decode("utf-8", "replace"))
            raise DartApiError("???", f"zip이 아닌 응답: {content[:200]!r}")
        return content

    def corp_codes_zip(self) -> bytes:
        """전체 기업 고유번호 파일 (zip 안에 CORPCODE.xml)."""
        return self._get_zip("corpCode.xml")

    def report_api(
        self, api_id: str, corp_code: str, bsns_year: str, reprt_code: str
    ) -> list[dict] | None:
        """정기보고서 주요정보 API 공통 호출 (alotMatter, hyslrSttus, empSttus 등).

        status 000 → list, 013(무자료) → None, 그 외(020 쿼터 포함) → DartApiError.
        """
        data = self._get(
            f"{api_id}.json",
            corp_code=corp_code,
            bsns_year=bsns_year,
            reprt_code=reprt_code,
        ).json()

        status = data.get("status")
        if status == _NO_DATA:
            return None
        if status != "000":
            raise DartApiError(status, data.get("message", _STATUS_MESSAGES.get(status, "")))
        return data.get("list", [])

    def fnltt_singl_acnt_all(
        self, corp_code: str, bsns_year: str, reprt_code: str, fs_div: str
    ) -> list[dict]:
        """단일회사 전체 재무제표. fs_div: CFS(연결) / OFS(별도).

        XML 테이블 파싱과 달리 IFRS concept(account_id)과 당기/전기 금액이
        이미 구조화되어 있다 (IMPLEMENTATION_PLAN.md §5 구현 순서 2).
        """
        data = self._get(
            "fnlttSinglAcntAll.json",
            corp_code=corp_code,
            bsns_year=bsns_year,
            reprt_code=reprt_code,
            fs_div=fs_div,
        ).json()

        status = data.get("status")
        if status == _NO_DATA:
            return []
        if status != "000":
            raise DartApiError(status, data.get("message", _STATUS_MESSAGES.get(status, "")))
        return data.get("list", [])

    def document_zip(self, rcept_no: str) -> bytes:
        """공시 원본 파일 (zip 안에 본문 XML)."""
        return self._get_zip("document.xml", rcept_no=rcept_no)
