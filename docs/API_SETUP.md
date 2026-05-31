# 🔑 API 연동 가이드 (API Setup Guide)

이 문서는 Finance LLM 실행에 필요한 API 키와 `.env` 설정을 안내합니다.
현재 기본 구성은 **OpenRouter**를 통해 생성 모델과 임베딩 모델을 모두 사용합니다.

- 생성 LLM 기본값: `deepseek/deepseek-v4-flash`
- 임베딩 기본값: `baai/bge-m3`
- Gemini는 `LLM_PROVIDER=gemini` 또는 `EMBEDDING_PROVIDER=gemini`로 되돌릴 때만 선택적으로 필요합니다.

---

## 1. OpenRouter API 키 발급

1. [OpenRouter Keys](https://openrouter.ai/settings/keys)에 접속합니다.
2. 로그인 후 **Create Key**를 눌러 API 키를 생성합니다.
3. 생성된 키는 한 번만 볼 수 있으므로 안전한 곳에 보관합니다.
4. 프로젝트 루트의 `.env` 파일에 `OPENROUTER_API_KEY`로 저장합니다.

> OpenRouter 계정에 크레딧/결제 설정이 없으면 유료 모델 호출이 실패할 수 있습니다.

---

## 2. `.env` 파일 만들기

프로젝트 루트에서 `.env.example`을 복사합니다.

```bash
# Linux/macOS
cp .env.example .env

# Windows (cmd)
copy .env.example .env

# Windows PowerShell
Copy-Item .env.example .env
```

`.env`의 기본 예시는 아래와 같습니다.

```env
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=your_openrouter_api_key_here
GENERATION_MODEL=deepseek/deepseek-v4-flash
OPENROUTER_APP_TITLE=finance_llm
OPENROUTER_DATA_COLLECTION=deny

EMBEDDING_PROVIDER=openrouter
EMBEDDING_MODEL=baai/bge-m3

# Optional fallback only when LLM_PROVIDER=gemini or EMBEDDING_PROVIDER=gemini
GEMINI_API_KEY=your_gemini_api_key_here
```

---

## 3. 설정 의미

| 변수 | 기본값 | 설명 |
|---|---|---|
| `LLM_PROVIDER` | `openrouter` | 생성 LLM 공급자. `openrouter` 또는 `gemini` |
| `GENERATION_MODEL` | `deepseek/deepseek-v4-flash` | 답변 생성, 라우팅, SQL 생성에 사용할 모델 |
| `EMBEDDING_PROVIDER` | `openrouter` | 임베딩 공급자. `openrouter` 또는 `gemini` |
| `EMBEDDING_MODEL` | `baai/bge-m3` | FAISS 인덱스 생성/검색에 사용할 임베딩 모델 |
| `OPENROUTER_DATA_COLLECTION` | `deny` | 데이터 수집을 하지 않는 provider로 라우팅하도록 제한 |
| `GEMINI_API_KEY` | 없음 | Gemini fallback을 사용할 때만 필요 |

---

## 4. 임베딩 모델 변경 시 주의

임베딩 모델을 바꾸면 기존 `data/vector_db` FAISS 인덱스는 재사용하면 안 됩니다.
벡터 차원과 분포가 달라져 검색 품질이 깨질 수 있습니다.

재빌드 절차는 README의 **DB 초기화 방법**을 따르세요. 핵심 명령은 아래와 같습니다.

```powershell
if (Test-Path data/vector_db) { Remove-Item -Recurse -Force data/vector_db }
python -c "import sqlite3; con=sqlite3.connect('data/reports.db'); con.execute('UPDATE reports SET is_embedded=0'); con.execute('DELETE FROM parent_chunks'); con.commit(); con.close()"
python -m src.core.embed_pipeline --all
```

---

## 5. 보안 원칙

- `.env`에는 실제 API 키가 들어가므로 git에 커밋하지 않습니다.
- `.env.example`에는 placeholder만 둡니다.
- OpenRouter API 키가 노출되면 즉시 OpenRouter 콘솔에서 폐기하고 새 키를 발급하세요.
