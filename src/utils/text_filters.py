import re
from src.configs.filter_configs import (
    _NUMERIC_TOKEN, _SIDEBAR_MARKERS, _SIDEBAR_SECTION_TERMS, 
    _STOCK_DATA_LABELS, _DISCLAIMER_PATTERNS, _CAPTION_RE, 
    _FIN_LABEL_RE, _COMPLIANCE_MARKERS
)

def is_sidebar_block(text: str) -> bool:
    """STOCK DATA/COMPANY DATA 블록, 짧은 섹션 헤더 블록, 또는 재무제표 레이블 블록이면 True."""
    if any(m in text for m in _SIDEBAR_MARKERS):
        return True
    # 짧은 블록(< 300자)에 섹션 헤더가 있으면
    if len(text) < 300 and any(term in text for term in _SIDEBAR_SECTION_TERMS):
        return True
    # 라인의 75% 이상이 짧은 재무 레이블이면 실질 재무제표 블록
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if len(lines) >= 6:
        short_label = sum(
            1 for l in lines
            if len(l) < 25 and 2 <= len(re.findall(r'[\uac00-\ud7a3]', l)) < 10
        )
        if short_label / len(lines) >= 0.75:
            return True
    return False


def is_noise_line(line: str) -> bool:
    """
    제거 대상 라인 판별. True → 제거 / False → 유지.
    """
    stripped = line.strip()
    if not stripped:
        return False
    # 규칙 0a: 면접문 / 준법 고지
    if any(p in stripped for p in _DISCLAIMER_PATTERNS):
        return True
    # 규칙 0b: 각주 / 자료 캐션
    if _CAPTION_RE.match(stripped):
        return True
    # 규칙 0c: 주식 정보 사이드바 레이블 (전체 일치)
    if stripped in _STOCK_DATA_LABELS:
        return True
    korean = len(re.findall(r'[\uac00-\ud7a3]', stripped))
    # 규칙 0d: 재무제표 행 레이블 (짧은 라인 + 재무 키워드 포함)
    if len(stripped) < 35 and _FIN_LABEL_RE.search(stripped):
        if korean == 0 or (1 <= korean < 14):
            return True
    if korean >= 13:
        return False  # 충분히 긴 문장 → 유지
    # 규칙 2: 짧은 레이블 라인
    if len(stripped) < 20 and korean <= 6:
        return True
    # 규칙 3: 숫자 토큰 비율 체크
    tokens = stripped.split()
    if not tokens:
        return False
    numeric_count = sum(1 for t in tokens if _NUMERIC_TOKEN.match(t))
    return numeric_count / len(tokens) >= 0.5


def strip_compliance(text: str) -> str:
    """
    Compliance Notice 섹션 이후 텍스트를 제거합니다.
    여러 증권사의 대표 시작 키워드를 기준으로 잘라냅니다.
    """
    earliest = len(text)
    for marker in _COMPLIANCE_MARKERS:
        idx = text.find(marker)
        if 0 < idx < earliest:
            earliest = idx
    return text[:earliest].rstrip()
