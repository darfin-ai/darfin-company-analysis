"""DART XML 로딩 및 정제 (파싱 전 단계).

DART가 주는 XML은 실제로는 well-formed가 아니다. 삼성전자 픽스처 4개년 기준
확인된 문제와 대응:

1. `</DOCUMENT>` 뒤에 중복/잘린 내용이 붙어 있음 (2023: 227KB, 2024: 13KB,
   2025: 1.3MB) → 첫 `</DOCUMENT>`에서 잘라낸다.
2. 이스케이프 안 된 & (예: "AT&T") — 파일당 300~650개 → &amp;로 치환.
3. 유효하지 않은 UTF-8 바이트 (2024 파일 2바이트) → errors="replace"로 흡수.
4. 그 밖의 구조 오류(문서 꼬리가 셀 내부에 끼어드는 등)는 lxml recover 모드가
   자동 복구하되, 에러 로그를 warnings로 남긴다.
"""

from __future__ import annotations

import re
from pathlib import Path

from lxml import etree

# XML 사전정의 엔티티(&amp; &lt; ...)와 문자 참조(&#39; &#x27;)가 아닌 맨 & 탐지
_BARE_AMP = re.compile(r"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)")

_DOC_END = "</DOCUMENT>"


def load_document(path: str | Path) -> tuple[etree._Element, list[str]]:
    """DART XML 파일 하나를 (루트 엘리먼트, 경고 목록)으로 로드한다."""
    path = Path(path)
    warnings: list[str] = []

    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    if "�" in text:
        warnings.append(f"invalid UTF-8 bytes replaced: {text.count(chr(0xFFFD))}")

    # 1. 문서 종료 태그 이후의 잔여물 제거
    end = text.find(_DOC_END)
    if end != -1:
        trailing = len(text) - (end + len(_DOC_END))
        if trailing > 0:
            warnings.append(f"discarded {trailing} chars after {_DOC_END}")
        text = text[: end + len(_DOC_END)]
    else:
        warnings.append(f"no {_DOC_END} found — file may be truncated")

    # 2. 맨 & 이스케이프
    text, n_amp = _BARE_AMP.subn("&amp;", text)
    if n_amp:
        warnings.append(f"escaped {n_amp} bare '&'")

    # 3. 관대한 파싱. 문자열에 encoding 선언이 있으면 lxml이 거부하므로
    #    다시 bytes로 인코딩해서 넘긴다.
    parser = etree.XMLParser(recover=True, huge_tree=True)
    root = etree.fromstring(text.encode("utf-8"), parser=parser)
    if root is None:
        raise ValueError(f"unrecoverable XML: {path}")

    n_errors = len(parser.error_log)
    if n_errors:
        first = parser.error_log[0]
        warnings.append(
            f"recovered from {n_errors} XML errors (first: line {first.line}: {first.message})"
        )

    return root, warnings
