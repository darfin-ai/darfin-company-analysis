"""DART 기업 고유번호(corp_code) 매핑.

corpCode.xml은 전체 상장·비상장 기업 ~10만 건의 매핑이라 매 실행마다
받지 않고 로컬에 캐시한다 (기본 TTL 24시간).

CORPCODE.xml 구조:
    <result><list>
      <corp_code>00126380</corp_code>
      <corp_name>삼성전자</corp_name>
      <stock_code>005930</stock_code>   ← 비상장이면 공백
      <modify_date>20260614</modify_date>
    </list>...</result>
"""

from __future__ import annotations

import io
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from .client import DartClient
from .config import DATA_DIR

_CACHE_TTL_SECONDS = 24 * 3600


@dataclass(frozen=True)
class CorpEntry:
    corp_code: str
    corp_name: str
    stock_code: str | None  # 상장사만 존재


class CorpCodeBook:
    def __init__(self, entries: list[CorpEntry]):
        self._by_stock = {e.stock_code: e for e in entries if e.stock_code}
        self._by_corp = {e.corp_code: e for e in entries}

    def by_stock_code(self, stock_code: str) -> CorpEntry | None:
        return self._by_stock.get(stock_code)

    def by_corp_code(self, corp_code: str) -> CorpEntry | None:
        return self._by_corp.get(corp_code)

    def __len__(self) -> int:
        return len(self._by_corp)


def load_corp_codes(client: DartClient, cache_dir: Path = DATA_DIR) -> CorpCodeBook:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / "corp_codes.zip"

    if not cache.exists() or time.time() - cache.stat().st_mtime > _CACHE_TTL_SECONDS:
        cache.write_bytes(client.corp_codes_zip())

    with zipfile.ZipFile(io.BytesIO(cache.read_bytes())) as zf:
        xml_bytes = zf.read("CORPCODE.xml")

    root = etree.fromstring(xml_bytes)
    entries = []
    for item in root.iter("list"):
        stock = (item.findtext("stock_code") or "").strip()
        entries.append(
            CorpEntry(
                corp_code=(item.findtext("corp_code") or "").strip(),
                corp_name=(item.findtext("corp_name") or "").strip(),
                stock_code=stock or None,
            )
        )
    return CorpCodeBook(entries)
