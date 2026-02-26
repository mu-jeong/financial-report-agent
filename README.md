# 📈 Finance LLM — 증권사 리포트 RAG 파이프라인

폴더에 저장된 증권사 종목/산업/경제 분석 리포트(PDF)를 정제하여 FAISS 벡터 DB에 저장하고, 자연어 질의로 금융 데이터를 검색하는 RAG(Retrieval-Augmented Generation) 파이프라인입니다.

## 🎯 프로젝트 목적

매일 수많은 종목 분석 보고서가 쏟아져 나오지만, 이를 모두 읽기에는 시간이 부족하고 읽더라도 나중에 필요한 내용을 다시 찾아보는 것은 매우 어렵습니다.

이 프로젝트는 **관심 있는 리포트를 로컬 환경에 체계적으로 저장하고, 나중에 필요할 때 자연어 검색을 통해 쉽고 빠르게 찾아볼 수 있도록 돕는 개인용 금융 지식 베이스 파이프라인**을 구축하는 것을 목적으로 합니다.

## ⚠️ 주의사항 (Disclaimer)

본 프로젝트는 금융 분야의 LLM 활용 및 데이터 파이프라인 학습을 목적으로 작성된 프로젝트입니다. 제공되는 스크립트의 사용으로 인해 발생할 수 있는 모든 이슈에 대한 책임은 사용자 본인에게 있습니다. 특히, 데이터 소스의 이용 약관 및 저작권 정책을 반드시 확인하고 준수하여 사용하시기 바랍니다.

---

## 🗂️ 프로젝트 구조

```
finance_llm/
├── cli/                 # 터미널 기반 사용자 인터페이스 (app.py)
├── configs/             # 설정, 필터링 규칙 및 프롬프트 (config.py, filter_configs.py, prompts.py 등)
├── graphs/              # LangGraph 흐름 조립 및 상태 정의 (main_graph.py, state.py)
├── nodes/               # LangGraph의 개별 비즈니스 로직(라우팅, 검색, SQL 생성 등) 모듈
├── utils/               # 공통 유틸리티 (text_filters.py, ranker.py 등)
├── docs/                # 시스템 설계 철학 및 연동 가이드문서
├── tools/               # 개발 및 디버깅 도구
├── report_crawler.py    # (Optional) 네이버 금융 리포트 수집 도구
├── db_manager.py        # SQLite RDB 관리 (메타데이터 및 임베딩 상태 추적)
├── embed_pipeline.py    # PDF 파싱 → 텍스트 정제 → 임베딩 → FAISS 저장
├── search.py            # 벡터 DB 검색 엔트리포인트 (CLI 실행 진입점)
├── downloaded/          # 분석 대상 PDF 저장 폴더
├── faiss_db/            # FAISS 인덱스 저장 폴더 (자동 생성)
├── reports.db           # SQLite DB (자동 생성)
├── requirements.txt     # 필요 패키지 목록
└── .env.example         # 환경 변수 템플릿
```

---

## ⚙️ 환경 설정

> ⚠️ **필수 요구사항:** 본 프로젝트는 최신 타입 힌트(`|` 문법 및 `TypedDict` 등)를 사용하므로 **`Python 3.10 이상`**의 환경이 권장됩니다. 하위 버전에서는 문법 에러가 발생할 수 있습니다.

### 1. 가상환경 생성 및 활성화 (권장)

프로젝트 의존성을 독립적으로 관리하기 위해 파이썬 가상환경(venv)을 사용하는 것을 강력히 권장합니다.

```bash
# 가상환경 생성 (.venv) - Python 3.10 이상 지정
# (Windows의 경우 환경 변수에 등록된 python 버전에 따라 python 또는 py -3.10 등을 사용하세요)
python -m venv .venv 
# 또는 Mac/Linux에서 특정 버전 지정 시: python3.10 -m venv .venv

# 가상환경 활성화 (Windows)
.venv\Scripts\activate

# 가상환경 활성화 (macOS/Linux)
source .venv/bin/activate
```

### 2. 패키지 설치

활성화된 가상환경 내에서 `requirements.txt`에 명시된 필수 패키지들을 설치합니다.

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 3. API 키 설정

프로젝트 루트의 `.env.example` 파일을 복사하여 `.env` 파일을 생성하고, 본인의 Gemini API 키를 입력합니다.

```bash
cp .env.example .env  # Linux/macOS
copy .env.example .env # Windows (cmd)
```

`.env` 파일 내용:

```env
GEMINI_API_KEY=your_gemini_api_key_here
```

---

## 🚀 사용 방법

### Step 1. 리포트 파일 준비 (⚠️ 파일명 규칙 엄수)

분석하고자 하는 PDF 파일을 `downloaded/` 폴더에 넣습니다. 본 파이프라인은 파일명을 기준으로 메타데이터를 파싱하므로 아래 **규칙을 반드시 지켜야 합니다.**

- **파일명 규칙:** `[유형]_[YYYY-MM-DD]_[대상]_[증권사]_[제목].pdf`
- **예시:** `company_2024-02-21_삼성전자_미래에셋증권_HBM 공급 확대 전망.pdf`
- **유형:** `company` (종목), `industry` (산업), `economy` (경제)
- 경제 리포트처럼 특정 대상이 없는 경우 대상 부분에 `null` 등을 기재합니다.

> **Note:** `report_crawler.py`를 사용하여 네이버 금융에서 자동으로 수집할 수도 있으나 이는 선택 사항(Optional)입니다. 다른 경로로 수집한 파일이라도 위 규칙대로 이름만 지정되어 있으면 정상적으로 처리됩니다.
> 
> 💡 **(현재 설정) 크롤러 수집 제한 알림:**
> `report_crawler.py` 실행 시 빠르고 가벼운 테스트 환경을 위해 **"가장 최근 평일 단 하루치 리포트"**만 다운로드하도록 기본 설정되어 있습니다.
> 더 많은 기간의 데이터를 수집하고 싶다면 `report_crawler.py` 최하단 실행부의 주석을 참고하여 `target_date_str = "YYYY-MM-DD"` 형태의 특정 과거 날짜를 지정하거나 로직을 수정하세요.

---

### Step 2. 임베딩 파이프라인 실행

준비된 PDF들을 텍스트로 변환하고 벡터 DB에 저장합니다.

```bash
python embed_pipeline.py
```

- 파이프라인은 `downloaded/` 폴더를 스캔하여 새로운 파일을 발견하면 DB에 등록하고 처리를 시작합니다.
- 표 영역 제외, 재무 레이블 제거 등 금융 리포트에 특화된 전처리가 자동으로 수행됩니다.

> 💡 **(현재 설정) 임베딩 10건 제한 (테스트 모드) 안내:**
> 다운로드된 PDF가 수십 건 이상일 경우 단기간 내 API 허용량(Rate Limit)을 초과할 수 있어, 현재 `configs/config.py` 파일 내에 `TEST_LIMIT = 10` (최대 10개만 임베딩)으로 안전 설정이 걸려있습니다.
> **제한 해제 방법:** 토큰을 사용하는데 금전적인 제약이 적다면, `configs/config.py`에서 `TEST_LIMIT = 0`으로 변경하면 폴더 내의 **모든 리포트**를 개수 제한 없이 한 번에 임베딩할 수 있습니다.

---

### Step 3. 복합 검색 (RDB + 벡터 DB 대화형 챗봇)

저장된 내용을 바탕으로 자연어 검색 및 AI 답변을 받아볼 수 있습니다. **LangGraph**를 활용하여 질문의 의도에 따라 아래와 같이 자동으로 탐색 경로(Router)가 나뉩니다.

```bash
python search.py
```

- **⏳ 초기 실행 대기시간:** LangGraph 상태 객체 컴파일 및 메모리 로딩 과정으로 인해 프로그램을 처음 실행하여 터미널 프롬프트가 나타날 때까지 **약 10~20초 정도의 대기 시간**이 발생할 수 있습니다.
- **메타데이터 질문 (RDB 처리):** *"저장된 산업 리포트는 모두 몇 개야?"*, *"미래에셋증권에서 나온 가장 최근 리포트는 언제 발간됐어?"* 와 같은 질문은 벡터 DB를 거치지 않고 직접 SQLite DB에 SQL 변환하여 빠르게 답변합니다.
- **문서 본문 질문 (Vector DB 처리):** *"삼성전자의 반도체 실적 전망 알려줘"* 와 같은 질문은 FAISS 벡터 DB를 검색하고 FlashRank를 통해 문서를 재평가(Reranking) 한 뒤, 참조 문헌(source)과 함께 심층적인 답변을 제공합니다.
- **다중 턴(Multi-turn) 대화 메모리 지원:** LangGraph의 `MemorySaver`와 쿼리 재작성(Query Rewrite) 노드가 탑재되어 있어, "그 첫 번째 보고서에 대해 조금 더 요약해 줘" 처럼 대명사를 포함한 연속적인 질문을 해도 과거 맥락(Chat History)을 기억하고 답변합니다.
- **애플리케이션 가드레일 (Guardrail):** 데코레이터(`@sql_guardrail`)와 `sqlglot` 라이브러리를 통해 LLM이 생성한 위험한 SQL 명령어를 추상 구문 트리(AST) 레벨에서 사전 차단하며, Pydantic Validator로 라우팅 응답 포맷을 검증합니다.
- 스크립트를 실행하면 터미널에 대화형 프롬프트가 나타납니다.
- 종료하려면 `q` 또는 `quit`를 입력하세요.
- 메모리를 초기화하려면 `c` 또는 `clear`를 입력하세요.

> **💡 Reranking (문서 재평가) 기능 활성화 방법**
> 기본적으로 빠른 응답 속도를 위해 Reranker 모델이 비활성화 되어 있습니다. 더 정확하고 문맥에 맞는 문서를 찾고 싶다면 `search.py` 파일 상단의 `USE_RERANKER = False` 를 `True` 로 변경하세요. (최초 1회 실행 시 모델 다운로드로 인해 1~2분 정도 소요될 수 있습니다.)

---

## 🛡️ 백엔드 성능 및 보안 최적화 (Advanced Engineering)

본 프로젝트는 단순한 데모를 넘어 **실제 프로덕션 환경의 안정성**을 고려하여 설계되었습니다.

1. **AST 기반 SQL 인젝션 완벽 방어 (`sqlglot`)**
   - 기존의 단순한 정규식(Regex) 필터링 대신, `sqlglot` 모듈을 도입해 LLM이 작성한 쿼리를 **추상 구문 트리(AST)로 완벽 파싱**하여 검증합니다.
   - 허락되지 않은 내부 테이블(`sqlite_master` 등) 접근을 차단하고 오직 `SELECT` 명령만 통과시키므로 어떤 난독화(Obfuscation)된 악의적 SQL 공격도 막아냅니다.
2. **배치(Batch) 처리를 통한 디스크 I/O 병목 제거**
   - 수백 개의 리포트를 DB에 동기화할 때 반복되는 `sqlite3.connect()` 열고 닫기로 인한 병목을 해소했습니다. (`db_manager.py`)
   - 파일을 순회하며 메모리(List)에서 메타데이터만 미리 파싱한 후, **단일 트랜잭션의 `.executemany()`**를 활용해 DB 쓰기(Write) 작업을 한 번에 처리합니다.
3. **중앙 집중형 로깅 시스템 (Centralized Logging)**
   - 단순한 `print()` 출력을 배제하고, `configs/config.py`에 파이썬 내장 `logging` 모듈을 전역으로 설정했습니다.
   - 터미널(Stream) 진행 상황과 함께, 모든 동작과 구체적인 에러 이력이 `finance_llm.log` 파일에 영구 기록되어 백그라운드 서버 모드로 구동할 때의 관찰성(Observability)을 확보했습니다.

---

## 🔧 DB 초기화 방법

새로운 규칙을 적용하거나 DB를 처음부터 다시 구성하고 싶을 때 사용합니다.

```bash
# 1. FAISS 인덱스 삭제 (PowerShell 기준)
Remove-Item -Recurse -Force faiss_db

# 2. SQLite 임베딩 상태 초기화
python -c "import sqlite3; con=sqlite3.connect('reports.db'); con.execute('UPDATE reports SET is_embedded=0'); con.commit(); print('완료')"
```

---

## 🧹 텍스트 정제 규칙 (Pre-processing)

증권사 리포트의 노이즈를 제거하기 위해 아래와 같은 규칙 기반 필터가 적용됩니다.

- **BBox 기반 표 제거:** PyMuPDF의 표 감지 기능을 사용해 표 영역과 겹치는 텍스트 블록 물리적 배제
- **재무 레이블 제거:** 손익계산서/재무상태표의 행 레이블(예: 지분법이익, 매출채권 등) 감지 및 제거
- **준법고지 제거:** 리포트 하단의 면책 조항 및 애널리스트 준법 확인 문구 섹션 절단
- **기타 노이즈:** 도표 캡션, 주석, 숫자 위주의 데이터 행 등 제거

---

## 📝 TODO (LangChain & LangGraph 통합 계획)

현재의 단방향 검색 구조(FAISS → LLM)를 넘어, LangChain 생태계의 장점을 살린 고도화 로드맵입니다.

- [x] **LangGraph 기반 질문 라우팅:** 사용자의 질의 의도에 따라 RDB(메타데이터)와 Vector DB(문서 본문)를 동적으로 분기(Router)하는 구조 구현
- [x] **대화형 챗봇 메모리 (History):** LangGraph 내장 `MemorySaver` 및 `Query Rewrite` 노드를 활용해 이전 질의응답 맥락 유지
- [x] **대화 메모리 초기화 기능 (CLI):** 특정 키워드(예: 'c' 또는 'clear') 입력 시 `thread_id`를 갱신하여 현재까지의 메모리를 초기화하고 새로운 세션 시작
- [ ] **다중 대화 쓰레드(세션) 관리 기능:** 여러 개의 독립적인 대화 쓰레드(세션)를 동시에 유지 및 저장하고, 과거의 대화 세션을 불러오거나 관리할 수 있는 히스토리 기능 확장 도입
- [ ] **Agent 및 툴 콜링 (Tool Calling):** AI가 스스로 판단하여 리포트 외의 최신·정량적 데이터를 수집하는 외부 API 호출 도입
  - **실시간 주가 조회 API 연결:** 리포트 발행일과 현재 시점 간의 가격 괴리를 보완하기 위한 주가 연동
  - **재무제표 API 연동:** OpenDART 또는 금융 데이터 API를 통해 정형화된 재무 데이터(매출액, 순이익 등)를 직접 끌어와 RAG 결과와 교차 검증
- [ ] **GUI 환경 지원:** 현재의 CLI(터미널) 방식을 넘어, 나중에는 Streamlit이나 Gradio 등을 활용해 누구나 쉽게 접근할 수 있는 사용자 인터페이스(UI) 개발
- [ ] **동시성 처리 (Concurrency):** `report_crawler.py`와 `embed_pipeline.py`에 대해 `asyncio`/`aiohttp` 또는 Celery/RQ 등을 이용한 비동기 백그라운드 워커 큐 도입을 통한 대규모 파일 파싱 속도 향상

---