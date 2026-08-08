# API 연동 및 설정 가이드

Finance LLM은 OpenRouter API를 중심으로 생성 모델, 임베딩 모델, 선택형 rerank 모델을 호출합니다. 실행값은 루트의 `.env`에 저장하고, 기본값과 설명은 `src/configs/settings.py`에서 한 번만 관리합니다.

## 설정 파일 역할

| 파일 | 역할 |
| --- | --- |
| `src/configs/settings.py` | 설정 이름, 기본값, 타입 파서, 설명의 단일 원본 |
| `.env.example` | `settings.py`에서 생성되는 환경 변수 템플릿 |
| `.env` | 실제 실행 환경의 비밀값과 override 값 |
| `src/configs/config.py` | 기존 코드 호환을 위한 설정 export 레이어 |

`.env.example`을 다시 생성해야 하면 아래 명령을 실행합니다.

```bash
python -m src.configs.generate_env_example
```

## `.env` 준비

루트의 `.env.example`을 복사해 `.env`를 만들고 API 키를 입력합니다.

```powershell
Copy-Item .env.example .env
```

```env
OPENROUTER_API_KEY=sk-or-...
```

Quick Start를 사용하면 `RUN_QUICKSTART.bat` 실행 중 입력한 API 키가 `.env`에 자동 저장됩니다.

## OpenRouter 설정

| 설정 | 설명 |
| --- | --- |
| `OPENROUTER_API_KEY` | OpenRouter 모델 및 임베딩 호출에 필요한 API 키 |
| `OPENROUTER_APP_URL` | OpenRouter rankings metadata에 전달할 선택형 referer URL |
| `OPENROUTER_APP_TITLE` | OpenRouter rankings metadata에 전달할 선택형 앱 이름 |
| `OPENROUTER_DATA_COLLECTION` | provider 데이터 수집 정책. 기본값은 `deny` |

## 모델 및 rerank 설정

| 설정 | 기본값 | 설명 |
| --- | --- | --- |
| `GENERATION_MODEL` | `deepseek/deepseek-v4-flash` | 답변 생성, 질문 재작성, SQL 생성 모델 |
| `EMBEDDING_MODEL` | `baai/bge-m3` | FAISS 색인과 검색 쿼리에 사용하는 임베딩 모델 |
| `USE_RERANKER` | `false` | 추가 rerank 사용 여부. 비용 절감을 위해 기본 비활성화 |
| `RERANK_PROVIDER` | `openrouter` | `openrouter` 또는 명시적 로컬 `flashrank` adapter |
| `RERANK_MODEL` | `cohere/rerank-v3.5` | OpenRouter rerank 모델 |
| `RERANK_TIMEOUT` | `60.0` | rerank 요청 timeout(초) |

`RERANK_PROVIDER=flashrank`는 자동 fallback이 아니라 명시적 로컬 adapter입니다. 이 경우 `RERANK_MODEL`도 FlashRank가 지원하는 로컬 model name으로 바꿔야 합니다(예: `ms-marco-TinyBERT-L-2-v2`). 기본값 `cohere/rerank-v3.5`는 OpenRouter rerank 전용입니다. Quick Start와 기본 실행은 비용을 줄이기 위해 `USE_RERANKER=false`를 권장합니다.

## 검색 및 임베딩 설정

| 설정 | 설명 |
| --- | --- |
| `SEARCH_TOP_K` | 답변 파이프라인으로 넘길 vector search 결과 수 |
| `SEARCH_CANDIDATE_MULTIPLIER` | retrieval과 rerank 전에 가져올 후보 수의 배수. 기본값은 `1`이며 후보 수는 `SEARCH_TOP_K × SEARCH_CANDIDATE_MULTIPLIER`로 계산 |
| `RECENCY_WEIGHT` | 최신 리포트에 부여하는 검색 점수 가중치 |
| `USE_PARENT_CHILD` | parent-child chunking 사용 여부 |
| `PARENT_CHUNK_SIZE` | parent chunk 크기 |
| `CHILD_CHUNK_SIZE` | child chunk 크기 |
| `CHUNK_SIZE` | parent-child 미사용 시 fallback/general chunk 크기 |
| `CHUNK_OVERLAP` | fallback/general chunk overlap |
| `PDF_EXTRACTION_ENGINE` | 일반 임베딩 run의 PDF 파싱/추출 엔진. `pymupdf`, `marker`, `opendataloader`, `docling`, `pdf-to-markdown` 중 선택. 기존 `EXTRACTION_ENGINE`도 alias로 동작 |
| `PDF_EXTRACTION_FALLBACK_ENGINE` | primary 엔진이 실패했을 때 한 번 재시도할 엔진. 배포 템플릿은 `opendataloader`를 명시하며, 키가 없거나 빈 값이면 비활성화. 기존 `EXTRACTION_FALLBACK_ENGINE`도 alias로 동작 |
| `UNEMBEDDED_PDF_EXTRACTION_ENGINE` | 미임베딩 문서에 사용할 PDF 파싱 엔진. 배포 템플릿은 `pymupdf`를 사용하며, 빈 값이면 `PDF_EXTRACTION_ENGINE`을 사용. 기존 `UNEMBEDDED_EXTRACTION_ENGINE`도 alias로 동작 |

배포 템플릿은 일반 문서와 미임베딩 문서를 먼저 `pymupdf`로 추출하고 `opendataloader` fallback을 명시합니다. 이를 실행하려면 Java 11+와 `java` 명령의 `PATH` 등록이 필요합니다. 새 fallback 키가 없는 기존 `.env`는 자동으로 정책을 바꾸지 않으며, 서로 다른 `UNEMBEDDED_PDF_EXTRACTION_ENGINE` override도 fallback 없이 해당 엔진만 사용합니다.

## 크롤러 설정

| 설정 | 설명 |
| --- | --- |
| `CRAWLER_MODE` | `LATEST` 또는 `SPECIFIC_DATE` |
| `CRAWLER_CATEGORIES` | `company`, `industry`, `economy`, comma-separated list, 또는 `all` |
| `CRAWLER_TARGET_DATE` | `SPECIFIC_DATE` 모드에서 사용할 기준일 |
| `CRAWLER_LOOKBACK_DAYS` | 기준일 포함 이전 며칠까지 조회할지 결정. `7`이면 총 8일 범위 |
| `CRAWLER_MAX_LOOKBACK_DAYS` | count 기반 수집의 안전 lookback 범위 |
| `CRAWLER_TARGET_COUNT` | 수집할 리포트 개수 제한. `0`이면 개수 제한 없음 |

`LATEST`는 실행 시점의 KST 현재 날짜를 기준으로 수집합니다. 과거 특정 날짜를 재현하려면 `SPECIFIC_DATE`와 `CRAWLER_TARGET_DATE`를 함께 사용합니다.

## 경로 설정

| 설정 | 설명 |
| --- | --- |
| `REPORT_PDF_DIR` | GUI의 `열기` 버튼이 PDF를 찾는 폴더. 비워두면 `data/downloaded` 사용 |
| `SAVE_DIR` | 다운로드 PDF이자 V2 source inventory의 기본 폴더 |
| `DATA_ROOT` | Native V2 retrieval의 기준 폴더. 비워두면 `data`를 사용하며 catalog와 snapshot은 이 폴더의 `retrieval/v2/` 아래에 저장 |
| `CONVERSATION_DB_PATH` | GUI/CLI 대화 SQLite 경로 |
| `COMPANY_INDUSTRY_DATA_PATH` | 선택형 KRX 업종 CSV 경로 |

임베딩 파이프라인과 다른 위치의 PDF를 열어야 하면 `.env`에서 `REPORT_PDF_DIR`을 해당 폴더로 지정합니다.

## 비용 참고

- 임베딩과 답변 생성은 OpenRouter API를 호출하므로 크레딧이 차감될 수 있습니다.
- rerank는 검색 때마다 추가 호출이 발생할 수 있으므로 기본값은 꺼져 있습니다.
- 비용은 PDF 길이, chunk 수, 추출 품질, OpenRouter 모델 가격에 따라 달라집니다.
- 사용량과 잔액은 OpenRouter Credits 또는 Activity 화면에서 주기적으로 확인하세요.

## Native V2 embedding profile 변경 주의

Native V2가 활성 상태일 때는 `DATA_ROOT/retrieval/v2`를 수동으로 삭제하거나 수정하지 마세요. 일반 updater는 활성 profile과 모델·추출기·chunk 설정이 다르면 fail closed로 중단하며, 같은 profile에서는 전체 source inventory를 비교해 변경된 PDF만 처리합니다.

Profile 전체를 변경해야 하면 앱과 데이터 업데이트 창을 닫고 `tools\recovery\REBUILD_V2.bat --check`로 현재 상태를 확인한 뒤 검증된 full-corpus successor를 만드세요. 자세한 절차는 [Native V2 전체 재구축](migrations/v2/V2_REBUILD.md)을 참고하세요.

## 보안 주의사항

- `.env`의 실제 API 키는 Git에 커밋하지 마세요.
- `.env.example`에는 placeholder만 두세요.
- API 키가 노출되었다면 OpenRouter에서 즉시 삭제하고 새 키를 발급하세요.
