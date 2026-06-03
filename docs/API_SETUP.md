# API 연동 및 설정 가이드

Finance LLM은 OpenRouter API를 중심으로 동작합니다. 현재 생성 모델, 임베딩 모델, 선택형 rerank 모델 모두 OpenRouter 경로로 설정할 수 있습니다.

- 생성 모델 기본값: `deepseek/deepseek-v4-flash`
- 임베딩 모델 기본값: `baai/bge-m3`
- Rerank 모델 기본값: `cohere/rerank-v3.5`
- Rerank는 비용을 고려해 기본적으로 비활성화되어 있습니다.

## 1. OpenRouter API 키와 크레딧

OpenRouter API 키 발급과 크레딧 충전 방법은 아래 문서를 참고하세요.

- [OpenRouter API 키 발급 방법](OPENROUTER_API_KEY.md)

> OpenRouter의 모델별 가격과 제공 여부는 변경될 수 있으므로 실제 사용 전 OpenRouter 콘솔에서 확인하세요.

## 2. 설정 관리 원칙

수정 가능한 설정의 단일 원본은 `src/configs/settings.py`입니다.

- `src/configs/settings.py`: 설정 이름, 기본값, 타입, 설명을 정의합니다.
- `src/configs/config.py`: 기존 코드 호환용 상수 export 레이어입니다.
- `.env`: 실제 사용자 실행값을 저장합니다.
- `.env.example`: `settings.py`에서 자동 생성되는 템플릿입니다.

`.env.example`을 다시 생성해야 할 때는 아래 명령을 실행합니다.

```bash
python -m src.configs.generate_env_example
```

## 3. `.env` 준비

루트의 `.env.example`을 복사해 `.env`를 만들고 `OPENROUTER_API_KEY`를 채웁니다.

```powershell
Copy-Item .env.example .env
```

Quick Start를 사용하면 `RUN_QUICKSTART.bat` 실행 중 API 키를 입력받아 `.env`에 자동 저장합니다.

## 4. 주요 설정 위치

모든 기본값은 `src/configs/settings.py`의 `CONFIG_SPECS`를 확인하세요. 대표 설정은 다음과 같습니다.

| 설정 | 설명 |
| --- | --- |
| `OPENROUTER_API_KEY` | OpenRouter API 호출에 사용하는 키 |
| `OPENROUTER_DATA_COLLECTION` | `deny` 또는 `allow`; provider 데이터 수집 정책 |
| `GENERATION_MODEL` | 답변 생성, 질문 재작성, SQL 생성에 사용하는 모델 |
| `EMBEDDING_MODEL` | FAISS 색인과 검색 쿼리에 사용하는 임베딩 모델 |
| `USE_RERANKER` | rerank 사용 여부. 비용 때문에 기본값은 false |
| `RERANK_PROVIDER` | `openrouter` 또는 fallback용 `flashrank` |
| `RERANK_MODEL` | OpenRouter rerank 모델 |
| `RECENCY_WEIGHT` | 최신 문서에 부여하는 검색 점수 가중치 |
| `CRAWLER_LOOKBACK_DAYS` | 기준일 포함 이전 며칠까지 수집할지 |
| `REPORT_PDF_DIR` | GUI의 참고 문서 `열기` 버튼이 PDF를 찾는 폴더 |
| `EXTRACTION_ENGINE` | PDF 추출 엔진: `pymupdf`, `marker`, `opendataloader` |
| `USE_PARENT_CHILD` | parent-child chunking 사용 여부 |
| `TEST_LIMIT` | 기본 임베딩 처리 제한. `0`이면 pending 전체 처리 |

## 5. 임베딩 비용 참고

현재 설정(`baai/bge-m3`) 기준으로 약 2,000건의 리포트를 임베딩 벡터화하는 데 약 **$0.05**가 들었습니다. 비용은 PDF 길이, 추출 품질, chunk 수, OpenRouter 가격 정책에 따라 달라질 수 있습니다.

Rerank는 검색 때마다 추가 비용이 발생할 수 있으므로 기본값을 `USE_RERANKER=false`로 둡니다. 답변 품질이 더 중요한 경우에만 켜는 것을 권장합니다.

## 6. FAISS 인덱스 재생성

임베딩 모델, 추출 엔진, chunk 전략을 바꾼 경우 기존 `data/vector_db`와 SQLite의 임베딩 상태를 초기화한 뒤 다시 색인하세요.

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

## 7. 보안 주의사항

- `.env`의 실제 API 키는 Git에 커밋하지 마세요.
- `.env.example`에는 placeholder만 두세요.
- OpenRouter 사용량과 비용은 OpenRouter 대시보드에서 주기적으로 확인하세요.
- 외부에서 받은 FAISS `index.pkl`은 신뢰할 수 없으면 로드하지 마세요.
