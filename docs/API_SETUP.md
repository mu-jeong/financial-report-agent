# API 연동 가이드

Finance LLM은 OpenRouter API를 중심으로 동작합니다. 현재 생성 모델, 임베딩 모델, 선택형 rerank 모델 모두 OpenRouter 경로로 설정할 수 있습니다.

- 생성 모델 기본값: `deepseek/deepseek-v4-flash`
- 임베딩 모델 기본값: `baai/bge-m3`
- Rerank 모델 기본값: `cohere/rerank-v3.5`
- Rerank는 비용을 고려해 기본적으로 비활성화되어 있습니다.

## 1. OpenRouter API 키 발급

1. [OpenRouter Keys](https://openrouter.ai/settings/keys)에 접속합니다.
2. 로그인 후 새 API 키를 생성합니다.
3. 생성된 키를 안전한 곳에 보관합니다.
4. 프로젝트 루트의 `.env` 파일에 `OPENROUTER_API_KEY`로 저장합니다.

> OpenRouter의 모델별 가격과 제공 여부는 변경될 수 있으므로 실제 사용 전 OpenRouter 콘솔에서 확인하세요.

## 2. `.env` 예시

루트의 `.env.example`을 복사해 `.env`를 만듭니다.

```env
OPENROUTER_API_KEY=sk-or-v1-your-key
OPENROUTER_DATA_COLLECTION=deny

GENERATION_MODEL=deepseek/deepseek-v4-flash
GENERATION_TEMPERATURE=0.1
GENERATION_MAX_TOKENS=4096

EMBEDDING_MODEL=baai/bge-m3
EMBEDDING_DIMENSIONS=1024

USE_RERANKER=false
RERANK_PROVIDER=openrouter
RERANK_MODEL=cohere/rerank-v3.5
RERANK_TIMEOUT=20
RERANK_CANDIDATE_MULTIPLIER=3
RECENCY_WEIGHT=0.15
```

## 3. 주요 설정

| 설정 | 기본값 | 설명 |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | 필수 | OpenRouter API 호출에 사용하는 키 |
| `OPENROUTER_DATA_COLLECTION` | `deny` | OpenRouter 데이터 수집 허용 여부 |
| `GENERATION_MODEL` | `deepseek/deepseek-v4-flash` | 답변 생성, 질문 재작성, SQL 생성에 사용하는 모델 |
| `GENERATION_TEMPERATURE` | `0.1` | 생성 모델 temperature |
| `GENERATION_MAX_TOKENS` | `4096` | 생성 모델 최대 출력 토큰 |
| `EMBEDDING_MODEL` | `baai/bge-m3` | FAISS 색인과 검색 쿼리에 사용하는 임베딩 모델 |
| `EMBEDDING_DIMENSIONS` | `1024` | 임베딩 차원. 모델 변경 시 함께 확인 필요 |
| `REPORT_PDF_DIR` | 자동 생성 | Streamlit GUI의 참고 문서 `열기` 버튼이 파일명과 조합해 PDF를 찾는 폴더. 임베딩 파이프라인이 `.env`에 절대경로로 기록 |
| `USE_RERANKER` | `false` | rerank 사용 여부. 비용 때문에 기본값은 false |
| `RERANK_PROVIDER` | `openrouter` | `openrouter` 또는 fallback용 `flashrank` |
| `RERANK_MODEL` | `cohere/rerank-v3.5` | OpenRouter rerank 모델 |
| `RERANK_TIMEOUT` | `20` | rerank API timeout 초 |
| `RERANK_CANDIDATE_MULTIPLIER` | `3` | rerank 후보를 `SEARCH_TOP_K` 대비 몇 배 가져올지 결정 |
| `RECENCY_WEIGHT` | `0.15` | 최신 문서에 부여하는 검색 점수 가중치 |

## 4. 임베딩 비용 참고

현재 설정(`baai/bge-m3`) 기준으로 약 2,000건의 리포트를 임베딩 벡터화하는 데 약 **$0.05**가 들었습니다. 비용은 PDF 길이, 추출 품질, chunk 수, OpenRouter 가격 정책에 따라 달라질 수 있습니다.

Rerank는 검색 때마다 추가 비용이 발생할 수 있으므로 기본값을 `USE_RERANKER=false`로 둡니다. 답변 품질이 더 중요한 경우에만 켜는 것을 권장합니다.

## 5. FAISS 인덱스 재생성

임베딩 모델, 임베딩 차원, 추출 엔진, chunk 전략을 바꾼 경우 기존 `data/vector_db`와 SQLite의 임베딩 상태를 초기화한 뒤 다시 색인하세요.

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

## 6. 보안 주의사항

- `.env`의 실제 API 키는 Git에 커밋하지 마세요.
- `.env.example`에는 placeholder만 두세요.
- OpenRouter 사용량과 비용은 OpenRouter 대시보드에서 주기적으로 확인하세요.
- 외부에서 받은 FAISS `index.pkl`은 신뢰할 수 없으면 로드하지 마세요.

