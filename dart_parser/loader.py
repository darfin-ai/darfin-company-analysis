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

# 본문 텍스트에 이스케이프 없이 섞인 '<삭제 2012.3.5>' 같은 개정이력 표기.
# DART 문서의 실제 태그명은 전부 영문 대문자(TITLE/SECTION-1/TD 등)라
# '<' 바로 뒤에 한글이 오면 태그가 아니라 텍스트로 확정할 수 있다 — 이걸
# 이스케이프하지 않으면 lxml이 가짜 태그로 오인해 attribute 파싱 에러를
# 내고, 에러가 한 섹션에 몰리면 recover 모드가 뒤따르는 섹션 전체(예:
# SK하이닉스 2025 사업보고서의 'VII. 주주에 관한 사항' 이후)를 통째로
# 유실한다.
_BARE_LT_KOREAN = re.compile(r"<(?=[가-힣])")

_DOC_END = "</DOCUMENT>"

_DECL_ENCODING = re.compile(rb'encoding\s*=\s*["\']([A-Za-z0-9._-]+)["\']')


def _sniff_encoding(raw: bytes) -> str:
    """XML 선언에서 인코딩을 읽는다. 픽스처는 UTF-8이지만 DART API로 받은
    구형 문서는 EUC-KR일 수 있다. 선언이 없으면 UTF-8로 가정."""
    if raw[:3] == b"\xef\xbb\xbf":
        return "utf-8"
    m = _DECL_ENCODING.search(raw[:200])
    if not m:
        return "utf-8"
    name = m.group(1).decode().lower()
    if name in ("euc-kr", "ks_c_5601-1987", "ksc5601"):
        return "cp949"  # EUC-KR의 상위집합 — 확장 한글까지 안전
    return name


def load_document(path: str | Path) -> tuple[etree._Element, list[str]]:
    """DART XML 파일 하나를 (루트 엘리먼트, 경고 목록)으로 로드한다."""
    path = Path(path)
    warnings: list[str] = []

    raw = path.read_bytes()
    encoding = _sniff_encoding(raw)
    text = raw.decode(encoding, errors="replace")
    if encoding != "utf-8":
        warnings.append(f"non-UTF-8 encoding: {encoding}")
    if "�" in text:
        warnings.append(f"invalid {encoding} bytes replaced: {text.count(chr(0xFFFD))}")

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

    # 2-b. 태그처럼 보이는 한글 시작 '<' 이스케이프 (개정이력 표기 등)
    text, n_lt = _BARE_LT_KOREAN.subn("&lt;", text)
    if n_lt:
        warnings.append(f"escaped {n_lt} bare '<' followed by Korean text")

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
