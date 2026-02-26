"""embed_pipeline.py 패치: FIN_LABEL_RE 앵커 제거 + Disclaimer/Compliance 패턴 추가"""
import re

path = "embed_pipeline.py"
content = open(path, encoding="utf-8").read()

# ── 1. _FIN_LABEL_RE 교체 ───────────────────────────────────────────────────
# 현재 블록 찾기: "# 재무제표 행 레이블 접미사 정규식" 부터 닫는 ) 포함 줄까지
fin_old = re.search(
    r"# 재무제표 행 레이블 접미사 정규식.*?_FIN_LABEL_RE = re\.compile\(.*?\)",
    content, re.DOTALL
)
if fin_old:
    new_fin = (
        "# 재무제표 행 레이블 감지 정규식 -- search() 방식 (앵커 없음)\n"
        "# 짧은 라인(< 30자)에 아래 키워드가 포함되면 재무 레이블로 판단 후 제거\n"
        "_FIN_LABEL_RE = re.compile(\n"
        "    r'손익|순이익|포괄이익|지분이익'\n"
        "    r'|지분법이익|지분법손실|지분법관련'\n"
        "    r'|사업이익|계속사업|세전계속|세전이익'\n"
        "    r'|유동자산|비유동자산|유동부채|비유동부채|유동성장기'\n"
        "    r'|증가율|감소율|성장률|이익률|수익률|회전율|보상배율'\n"
        "    r'|현금흐름|현금및현금성|현금성자산|현금 및 현금'\n"
        "    r'|누계액|주주지분|이자보상'\n"
        "    r'|자산부채|매출채권|매입채무|미지급금|선급금|선수금'\n"
        "    r'|판매비와관리비|판관비|감가상각|상각비'\n"
        "    r'|관계기업|종속기업|지분법'\n"
        "    r'|비현금항목|비현금성|유가증권평가'\n"
        "    r'|기타채권|기타채무|기타자산|기타부채'\n"
        "    r'|투자자산|투자부채'\n"
        r"    r'|처분\(|취득\('" "\n"
        "    r'|의\\s*가감|의\\s*증감|의\\s*변동'\n"
        "    r'|재무제표|요약재무|연결재무'\n"
        "    r'|Financial Ratio|Peer Valuation|K-IFRS'\n"
        ")"
    )
    content = content[:fin_old.start()] + new_fin + content[fin_old.end():]
    print("FIN_LABEL_RE 교체 성공")
else:
    print("FIN_LABEL_RE 블록을 찾지 못함 — 수동 확인 필요")

# ── 2. _DISCLAIMER_PATTERNS 끝에 패턴 추가 ──────────────────────────────────
dis_additions = [
    '    "당 보고서 공표일 기준으로",',
    '    "회사는 해당 종목을 1%이상 보유",',
    '    "금융투자분석사와 그 배우자",',
    '    "E-mail등을 통하여 사전에 배포",',
    '    "주관사로 참여하지 않았습니다",',
    '    "본 분석자료는 투자자의 증권투자를",',
    '    "종목투자의견은 향후 12개월간",',
    '    "추천일 종가대비",                     # Buy/Hold/Sell 등급 정의 라인',
    '    "무단전재 등으로 인한 분쟁발생시",',
    '    "법적 책임이 있음을 주지",',
]
# _DISCLAIMER_PATTERNS 마지막 줄 `]` 바로 앞에 삽입
dis_end = re.search(r'(_DISCLAIMER_PATTERNS\s*=\s*\[.*?)\n]', content, re.DOTALL)
if dis_end:
    insert_text = "\n".join(dis_additions)
    # 이미 있는 항목 제외
    to_add = [l for l in dis_additions if l.split('"')[1] not in content]
    if to_add:
        new_block = dis_end.group(0).rstrip("]") + "\n" + "\n".join(to_add) + "\n]"
        content = content[:dis_end.start()] + new_block + content[dis_end.end():]
        print(f"DISCLAIMER 패턴 {len(to_add)}개 추가 성공")
    else:
        print("DISCLAIMER 패턴 이미 존재 — 스킵")
else:
    print("DISCLAIMER_PATTERNS 블록을 찾지 못함")

# ── 3. _COMPLIANCE_MARKERS 끝에 마커 추가 ───────────────────────────────────
comp_additions = [
    '    "목표주가 변동추이",',
    '    "종목추천 투자등급",',
    '    "본 분석자료는 투자자의 증권투자",',
    '    "당 보고서 공표일 기준",',
]
comp_end = re.search(r'(_COMPLIANCE_MARKERS\s*=\s*\[.*?)\n]', content, re.DOTALL)
if comp_end:
    to_add = [l for l in comp_additions if l.split('"')[1] not in content]
    if to_add:
        new_block = comp_end.group(0).rstrip("]") + "\n" + "\n".join(to_add) + "\n]"
        content = content[:comp_end.start()] + new_block + content[comp_end.end():]
        print(f"COMPLIANCE_MARKERS 마커 {len(to_add)}개 추가 성공")
    else:
        print("COMPLIANCE_MARKERS 마커 이미 존재 — 스킵")
else:
    print("COMPLIANCE_MARKERS 블록을 찾지 못함")

open(path, "w", encoding="utf-8").write(content)
print("파일 저장 완료")

# 검증
import subprocess
result = subprocess.run(
    ["python", "-c", "import embed_pipeline; print('import 성공')"],
    capture_output=True, text=True, encoding="utf-8"
)
print(result.stdout.strip() or result.stderr.strip())
