# PDF 추출 엔진 비교

Finance LLM은 PDF에서 텍스트 또는 Markdown을 추출하기 위해 여러 엔진을 지원합니다. 기본 엔진은 `pymupdf`이며, 필요에 따라 `opendataloader` 또는 `marker`를 비교해 사용할 수 있습니다.

## 지원 엔진

| 엔진 | 특징 | 주의사항 |
| --- | --- | --- |
| `pymupdf` | 빠르고 설치 부담이 낮은 기본 추출 엔진 | 표/레이아웃이 복잡한 일부 PDF에서는 구조 보존이 약할 수 있음 |
| `opendataloader` | Markdown 형태 추출을 목표로 하는 LangChain OpenDataLoader 연동 | Java 11+와 `PATH`, `JAVA_HOME`, `JDK_HOME`, `JRE_HOME` 환경 설정이 필요할 수 있음 |
| `marker` | 품질 높은 Markdown 추출을 목표로 함 | CPU 환경에서는 무겁고 느릴 수 있으며 의존성이 큼 |

## production 엔진 설정

`.env`에서 추출 엔진을 지정합니다.

```env
EXTRACTION_ENGINE=pymupdf
```

기본값은 `src/configs/settings.py`에 정의되어 있고, `src/configs/config.py`를 통해 기존 코드와 호환됩니다. `opendataloader`나 `marker`는 로컬 런타임 요구사항을 확인한 뒤 사용하세요.

## downstream 처리 계약

추출 엔진이 달라도 embedding pipeline의 downstream 흐름은 동일합니다.

1. PDF에서 텍스트 또는 Markdown 추출
2. 금융 리포트 cleanup filter 적용
3. Markdown header 기준 1차 분할
4. `USE_PARENT_CHILD=true`이면 parent-child chunking 적용
5. `USE_PARENT_CHILD=false`이면 recursive chunking 사용
6. OpenRouter 임베딩 모델로 벡터 생성
7. FAISS와 SQLite에 저장

`marker` 또는 `opendataloader`가 production 추출 중 실패하면 pipeline은 PyMuPDF로 fallback합니다.

## 엔진 비교 실행

가벼운 비교:

```bash
python -m src.core.compare_pdf_extractors --limit 10
```

폴더를 명시해 비교:

```bash
python -m src.core.compare_pdf_extractors data/downloaded --limit 20
```

Marker까지 포함:

```bash
python -m src.core.compare_pdf_extractors --engines pymupdf opendataloader marker --limit 5
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

비교 결과는 기본적으로 `reports/` 아래에 저장됩니다.

- `reports/pdf_extraction_compare.csv`
- `reports/pdf_extraction_compare.json`

## 주요 지표

비교 프로세스는 다음 정보를 기록합니다.

- 추출 성공/실패 상태
- 소요 시간
- 문자 수
- token 추정치
- 줄 수와 block 수
- Markdown header 줄 수
- Markdown table-like 줄 수
- 숫자 줄 비율
- 한글 줄 비율

JSON summary로 엔진별 경향을 먼저 보고, CSV row로 특정 PDF의 이상치를 확인하세요.

## 인덱스 재생성

추출 엔진, 임베딩 모델, chunk 전략을 바꾸면 chunk 텍스트가 달라질 수 있습니다. production 인덱스를 깨끗하게 다시 만들려면 기존 FAISS와 임베딩 상태를 초기화한 뒤 재실행하세요.

```powershell
Remove-Item -Recurse -Force data\vector_db
python - <<'PY'
from src.core.db_manager import get_connection

conn = get_connection()
conn.execute("UPDATE reports SET is_embedded = 0")
conn.execute("DELETE FROM parent_chunks")
conn.commit()
conn.close()
PY
python -m src.core.embed_pipeline --all
```
