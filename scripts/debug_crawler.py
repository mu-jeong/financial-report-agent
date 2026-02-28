import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import re

base_url = "https://finance.naver.com"
list_url = base_url + "/research/company_list.naver"
headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

res = requests.get(list_url, headers=headers, params={"page": 1})
res.encoding = "euc-kr"
soup = BeautifulSoup(res.text, "html.parser")

# ─── 1) 테이블 탐색 ──────────────────────────────────────────────
print("=== [1] 테이블 탐색 ===")
table = soup.find("table", class_="type_1")
print(f"type_1 테이블 존재: {table is not None}")

# type_1 이 없을 경우 다른 테이블 확인
all_tables = soup.find_all("table")
print(f"페이지 내 전체 테이블 수: {len(all_tables)}")
for i, t in enumerate(all_tables):
    cls = t.get("class", [])
    rows = t.find_all("tr")
    print(f"  테이블[{i}] class={cls}, 행 수={len(rows)}")

if not table:
    print("\n[!] type_1 테이블 없음 → 첫 번째 테이블로 대체 시도")
    if all_tables:
        table = all_tables[0]

# ─── 2) 데이터 행 분석 ───────────────────────────────────────────
if table:
    rows = table.find_all("tr")
    print(f"\n=== [2] 데이터 행 분석 (총 행: {len(rows)}) ===")
    data_count = 0
    for row in rows:
        tds = row.find_all("td")
        if len(tds) < 6:
            continue
        data_count += 1
        if data_count <= 3:
            print(f"\n--- 데이터 행 {data_count} ---")
            for i, td in enumerate(tds):
                print(f"  tds[{i}]: '{td.text.strip()[:60]}'")
            title_a = tds[1].find("a")
            if title_a:
                href = title_a.get("href")
                detail_url = urljoin(base_url, href)
                print(f"  href 원본: {href}")
                print(f"  detail_url: {detail_url}")

                # ─── 3) 상세 페이지 진입 ─────────────────────────────
                print(f"\n  ==> [3] 상세 페이지 요청: {detail_url}")
                detail_res = requests.get(detail_url, headers=headers)
                detail_res.encoding = "euc-kr"
                detail_soup = BeautifulSoup(detail_res.text, "html.parser")

                # PDF 링크 탐색
                pdf_a = detail_soup.find("a", href=re.compile(r".*\.pdf", re.IGNORECASE))
                print(f"  PDF 링크 존재(*.pdf): {pdf_a is not None}")

                # 혹시 모를 다른 다운로드 링크 탐색
                all_links = detail_soup.find_all("a", href=True)
                download_links = [a["href"] for a in all_links if any(ext in a["href"].lower() for ext in [".pdf", "download", "file"])]
                print(f"  Download 관련 링크들: {download_links[:5]}")

                # 실제 PDF 링크가 없으면 모든 링크 출력
                if not pdf_a and not download_links:
                    print("  [!] 다운로드 링크 없음 → 전체 링크 목록:")
                    for a in all_links[:20]:
                        print(f"       {a['href']}")
            else:
                print("  [!] title_a (a 태그) 없음")

    print(f"\n총 데이터 행: {data_count}")
