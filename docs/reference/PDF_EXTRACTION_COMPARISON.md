# PDF 추출 엔진 비교

Finance LLM은 PDF에서 텍스트 또는 Markdown을 추출하기 위해 여러 엔진을 지원합니다. 기본 엔진은 `pymupdf`입니다. 선택형 엔진은 로컬 런타임 요구사항이 다르므로 비교 도구로 먼저 샘플 품질과 속도를 확인한 뒤 production 설정에 반영하세요.

## 지원 엔진

| 엔진 | 특징 | 주의사항 |
| --- | --- | --- |
| `pymupdf` | 빠르고 설치 부담이 낮은 기본 추출 엔진 | 기본 `find_tables()`로 표 영역을 찾고, 면적의 50%를 초과해 겹치는 텍스트 block만 제외합니다. |
| `opendataloader` | LangChain OpenDataLoader PDF 연동. JSON 출력에서 table node를 제거한 뒤 텍스트화합니다. | Java 11+와 `PATH`, `JAVA_HOME`, `JDK_HOME`, `JRE_HOME` 환경 설정이 필요할 수 있습니다. |
| `docling` | Docling PDF pipeline 기반 Markdown 추출 | 기본 requirements에는 포함하지 않는 선택형 엔진입니다. 사용 전 `pip install docling`이 필요하고, 코드에서는 `do_table_structure=False`로 표 구조 인식을 끕니다. |
| `pdf-to-markdown` | Nutrient/PSPDFKit `pdf-to-markdown` CLI stdout을 사용 | Python 패키지가 아니라 CLI가 `PATH`에 있어야 합니다. 예: `npm install -g @pspdfkit/pdf-to-markdown`. |

지원 alias: `pspdfkit`, `nutrient`, `nutrient-pdf-to-markdown` → `pdf-to-markdown`.

## production 엔진 설정

`.env`에서 추출 엔진을 지정합니다. Streamlit/CLI 프로세스 재시작 후 반영됩니다.

```env
PDF_EXTRACTION_ENGINE=pymupdf
PDF_EXTRACTION_FALLBACK_ENGINE=opendataloader

# 미임베딩 문서에 사용할 엔진입니다. 빈 값이면 PDF_EXTRACTION_ENGINE을 그대로 씁니다.
UNEMBEDDED_PDF_EXTRACTION_ENGINE=pymupdf
```

런타임 기본값과 배포 템플릿 값은 `src/configs/settings.py`에 정의되어 있고, 실제 실행값은 `src/configs/config.py`를 통해 로드됩니다. 배포 템플릿은 OpenDataLoader fallback을 명시하지만 새 키가 없는 기존 `.env`는 fallback을 사용하지 않습니다. 기존 `EXTRACTION_ENGINE`, `EXTRACTION_FALLBACK_ENGINE`, `UNEMBEDDED_EXTRACTION_ENGINE`도 alias로 계속 동작합니다. `opendataloader`, `docling`, `pdf-to-markdown`는 선택형 엔진이므로 로컬 런타임 요구사항을 확인한 뒤 사용하세요.

## 표 제거 계약

현재 색인 파이프라인은 모든 PDF 추출 옵션에서 표 데이터를 줄이기 위한 엔진별 처리와 공통 후처리를 적용합니다.

1. `pymupdf`: 페이지마다 기본 line 기반 `page.find_tables()`를 한 번 호출합니다. `strategy="text"`는 사용하지 않으며, 표 BBox와 텍스트 block 면적의 50%를 초과해 겹치는 block만 제외합니다. 그 밖의 식별 가능한 표는 공통 후처리에서 제거합니다.
2. `opendataloader`: `format="json"`으로 받은 구조에서 `table`, `table row`, `table cell` 및 연결된 table caption을 건너뜁니다.
3. `docling`: `PdfPipelineOptions.do_table_structure=False`로 표 구조 인식을 비활성화합니다.
4. `pdf-to-markdown`: CLI 자체에 table-off 옵션이 없으므로 공통 후처리에서 Markdown/HTML/plain-text table block을 제거합니다.

그 후 모든 엔진 출력은 `drop_markdown_tables()`와 `clean_extracted_text()`를 통과합니다. `--raw` 비교 모드에서도 table 제거는 유지되고, 금융 리포트 cleanup filter만 생략됩니다.

## downstream 처리 계약

추출·표 제거·chunking은 다음 순서로 처리합니다.

1. PDF에서 텍스트 또는 Markdown 추출
2. 표 제거 및 금융 리포트 cleanup filter 적용
3. Markdown header 기준 1차 분할
4. `USE_PARENT_CHILD=true`이면 parent-child chunking 적용
5. `USE_PARENT_CHILD=false`이면 recursive chunking 사용
6. OpenRouter 임베딩 모델로 벡터 생성
7. 신규·변경 문서만 처리하고 unchanged vector를 재사용한 완전한 immutable Native V2 snapshot/catalog successor 게시

배포 템플릿의 production embedding은 신규·미임베딩 문서를 먼저 `pymupdf`로 처리하고, 실패하면 명시된 `PDF_EXTRACTION_FALLBACK_ENGINE=opendataloader`로 한 번 재시도합니다. fallback도 실패하면 두 엔진의 오류를 함께 보존해 해당 문서를 실패로 기록합니다. 서로 다른 `UNEMBEDDED_PDF_EXTRACTION_ENGINE` override와 비교 CLI는 fallback하지 않습니다. fallback 설정을 누락·비우거나 `allow_fallback=False`로 호출해도 primary 오류를 그대로 기록합니다. Native V2는 active profile에 기록된 fallback과 설정이 다르면 incremental publication 전에 중단합니다.

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
python -m src.core.compare_pdf_extractors --engines pymupdf opendataloader --limit 5
```

선택형 엔진까지 모두 비교하려면 `docling` 패키지와 `pdf-to-markdown` CLI를 먼저 준비한 뒤 실행합니다.

```bash
python -m src.core.compare_pdf_extractors --engines pymupdf opendataloader docling pdf-to-markdown --limit 5
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

## Profile 변경과 Native V2 재구축

Extraction engine, embedding model, chunk policy가 active profile과 다르면 incremental update는 fail closed합니다. 앱과 데이터 업데이트 창을 닫은 뒤 다음 명령으로 현재 설정과 목표 profile을 확인하고, 필요한 경우 완전한 Native V2 successor를 만들어 검증·게시하세요.

```bat
tools\recovery\REBUILD_V2.bat --check
tools\recovery\REBUILD_V2.bat
```

현재 활성 데이터는 `DATA_ROOT/retrieval/v2` 아래에 유지되며, 새 snapshot은 검증을 통과한 뒤에만 활성화됩니다. 자세한 절차는 [Native V2 전체 재구축](../reference/migrations/v2/V2_REBUILD.md)을 참고하세요.
