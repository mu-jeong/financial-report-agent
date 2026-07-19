# PDF 추출 엔진 비교

Finance LLM은 PDF에서 텍스트 또는 Markdown을 추출하기 위해 여러 엔진을 지원합니다. 기본 엔진은 `pymupdf`입니다. 선택형 엔진은 로컬 런타임 요구사항이 다르므로 비교 도구로 먼저 샘플 품질과 속도를 확인한 뒤 production 설정에 반영하세요.

## 지원 엔진

| 엔진 | 특징 | 주의사항 |
| --- | --- | --- |
| `pymupdf` | 빠르고 설치 부담이 낮은 기본 추출 엔진 | PyMuPDF `find_tables()`로 표 영역을 찾아 겹치는 텍스트 block을 제외한 뒤 일반 텍스트를 추출합니다. |
| `opendataloader` | LangChain OpenDataLoader PDF 연동. JSON 출력에서 table node를 제거한 뒤 텍스트화합니다. | Java 11+와 `PATH`, `JAVA_HOME`, `JDK_HOME`, `JRE_HOME` 환경 설정이 필요할 수 있습니다. |
| `marker` | 시각 구조 기반 Markdown 추출을 목표로 하는 Marker 연동 | CPU 환경에서는 무겁고 느릴 수 있습니다. table 관련 Marker processor는 제외하고 실행합니다. |
| `docling` | Docling PDF pipeline 기반 Markdown 추출 | 기본 requirements에는 포함하지 않는 선택형 엔진입니다. 사용 전 `pip install docling`이 필요하고, 코드에서는 `do_table_structure=False`로 표 구조 인식을 끕니다. |
| `pdf-to-markdown` | Nutrient/PSPDFKit `pdf-to-markdown` CLI stdout을 사용 | Python 패키지가 아니라 CLI가 `PATH`에 있어야 합니다. 예: `npm install -g @pspdfkit/pdf-to-markdown`. |

지원 alias: `datalab-marker`, `marker-pdf` → `marker`; `pspdfkit`, `nutrient`, `nutrient-pdf-to-markdown` → `pdf-to-markdown`.

## production 엔진 설정

`.env`에서 추출 엔진을 지정합니다. Streamlit/CLI 프로세스 재시작 후 반영됩니다.

```env
PDF_EXTRACTION_ENGINE=pymupdf

# 미임베딩/재시도 문서만 다른 엔진으로 처리하고 싶을 때 사용합니다.
# 빈 값이면 PDF_EXTRACTION_ENGINE을 그대로 씁니다.
UNEMBEDDED_PDF_EXTRACTION_ENGINE=opendataloader
```

기본값과 설명은 `src/configs/settings.py`에 정의되어 있고, 실제 실행값은 `src/configs/config.py`를 통해 로드됩니다. 기존 `.env`의 `EXTRACTION_ENGINE`, `UNEMBEDDED_EXTRACTION_ENGINE`도 alias로 계속 동작합니다. `marker`, `opendataloader`, `docling`, `pdf-to-markdown`는 선택형 엔진이므로 로컬 런타임 요구사항을 확인한 뒤 사용하세요.

## 표 제거 계약

현재 색인 파이프라인은 모든 PDF 추출 옵션에서 표가 downstream으로 들어가지 않도록 처리합니다.

1. `pymupdf`: `page.find_tables()`의 기본 line 기반 전략과 `strategy="text"`를 모두 시도해 table bbox를 수집하고, bbox와 겹치는 텍스트 block을 제외합니다.
2. `marker`: Marker의 processor override hook을 사용해 `TableProcessor`, `LLMTableProcessor`, `LLMTableMergeProcessor`를 제외합니다.
3. `opendataloader`: `format="json"`으로 받은 구조에서 `table`, `table row`, `table cell` 및 연결된 table caption을 건너뜁니다.
4. `docling`: `PdfPipelineOptions.do_table_structure=False`로 표 구조 인식을 비활성화합니다.
5. `pdf-to-markdown`: CLI 자체에 table-off 옵션이 없으므로 공통 후처리에서 Markdown/HTML/plain-text table block을 제거합니다.

그 후 모든 엔진 출력은 `drop_markdown_tables()`와 `clean_extracted_text()`를 통과합니다. `--raw` 비교 모드에서도 table 제거는 유지되고, 금융 리포트 cleanup filter만 생략됩니다.

## downstream 처리 계약

추출·표 제거·chunking 계약은 공통이지만 저장 단계는 backend별로 다릅니다.

1. PDF에서 텍스트 또는 Markdown 추출
2. 표 제거 및 금융 리포트 cleanup filter 적용
3. Markdown header 기준 1차 분할
4. `USE_PARENT_CHILD=true`이면 parent-child chunking 적용
5. `USE_PARENT_CHILD=false`이면 recursive chunking 사용
6. OpenRouter 임베딩 모델로 벡터 생성
7. V1은 SQLite parent와 mutable `data/vector_db`를 갱신하고, V2는 신규·변경 문서만 처리해 unchanged vector를 재사용한 완전한 immutable snapshot/catalog successor를 게시

`extract_pdf_text()` API 기본은 fallback 허용이지만 production embedding은 선택된 pending extractor가 primary extractor와 같을 때만 fallback을 허용합니다. 서로 다른 `UNEMBEDDED_PDF_EXTRACTION_ENGINE` override와 비교 CLI는 fallback하지 않습니다. `pymupdf` 자체가 실패하거나 `allow_fallback=False`인 경우에는 오류로 기록됩니다.

## 엔진 비교 실행

가벼운 비교(기본 비교 엔진은 `pymupdf`, `opendataloader`):

```bash
python -m src.core.compare_pdf_extractors --limit 10
```

폴더를 명시해 비교:

```bash
python -m src.core.compare_pdf_extractors data/downloaded --limit 20
```

설치 부담이 낮은 엔진 위주 비교:

```bash
python -m src.core.compare_pdf_extractors --engines pymupdf opendataloader marker --limit 5
```

선택형 엔진까지 모두 비교하려면 `docling` 패키지와 `pdf-to-markdown` CLI를 먼저 준비한 뒤 실행합니다.

```bash
python -m src.core.compare_pdf_extractors --engines pymupdf opendataloader marker docling pdf-to-markdown --limit 5
```

cleanup filter 적용 전 raw 출력 비교:

```bash
python -m src.core.compare_pdf_extractors --raw --limit 5
```

수동 검토용 추출 샘플 저장:

```bash
python -m src.core.compare_pdf_extractors --limit 1 --sample-dir reports/pdf_samples
```

전체 추출 텍스트를 저장하려면 `--sample-chars 0`을 사용합니다.

## 출력 파일

CLI 비교 결과는 기본적으로 다음 파일에 저장됩니다.

- `reports/pdf_extraction_compare.csv`
- `reports/pdf_extraction_compare.json`

Streamlit Monitoring Mode의 Parsing engine evaluation은 `run_id`별 파일을 `reports/pdf_extraction/` 아래에 저장합니다.

- `reports/pdf_extraction/<run_id>.csv`
- `reports/pdf_extraction/<run_id>.json`
- `reports/pdf_extraction/<run_id>_samples/` (sample 저장을 켠 경우)

## 주요 지표

비교 프로세스는 다음 정보를 기록합니다.

- 추출 성공/실패 상태
- 실제 사용 엔진과 fallback 여부
- 소요 시간
- 문자 수
- token 추정치
- 줄 수와 block 수
- Markdown header 줄 수
- Markdown table-like 줄 수
- 숫자 줄 비율
- 한글 줄 비율

JSON summary로 엔진별 경향을 먼저 보고, CSV row로 특정 PDF의 이상치를 확인하세요.

## Profile 변경과 재구축

활성 V2에서는 아래 V1 수동 reset 절차를 사용하지 않습니다. Extraction engine, embedding model, chunk policy가 active profile과 다르면 incremental update는 fail closed합니다. 변경된 profile은 별도로 완전한 native successor를 만들고 검증·게시해야 하며 `data/vector_db` 삭제나 legacy `reports.db` 수정으로 V2가 재구축되지는 않습니다.

아래 절차는 canonical authority가 legacy V1이고 V2 migration/rollback artifact가 없는 설치에만 적용됩니다.

```powershell
Remove-Item -Recurse -Force data\vector_db
@'
from src.core.db_manager import get_connection

conn = get_connection()
conn.execute("UPDATE reports SET is_embedded = 0")
conn.execute("DELETE FROM parent_chunks")
conn.commit()
conn.close()
'@ | python -
python -m src.core.embed_pipeline --all
```
