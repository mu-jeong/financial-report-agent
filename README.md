# Finance Report Agent

> Version: `0.6.3`

Finance Report Agent는 여러 증권사의 기업·산업·경제 리포트를 한곳에 모아 자연어로 검색하고 분석하는 로컬 리서치 도구입니다. 원하는 기간과 기업, 산업, 증권사를 말로 지정하면 관련 리포트의 목록과 통계, 핵심 내용을 대화형 답변으로 확인할 수 있습니다.

> 이 프로젝트는 투자 조언이나 매수/매도 추천을 제공하지 않습니다. 답변은 수집·색인된 리포트와 공개 데이터 기반의 참고 정보로만 사용하세요.

---

## 화면 예시

### Chat

![현재 Chat 화면](examples/example1.png)

## Quick Start: 간편하게 실행하기

Windows에서 처음 실행할 때는 `RUN_QUICKSTART.bat`을 더블클릭하면 설치, OpenRouter API 키 설정, 실행일 포함 이전 7일 범위(총 최대 8일)의 리포트 수집, 임베딩 생성, 웹 화면 실행까지 자동으로 진행됩니다.

1. [OpenRouter API 키 발급 방법](docs/getting-started/OPENROUTER_API_KEY.md)을 따라 API 키와 크레딧을 준비합니다.
2. 프로젝트 폴더에서 `RUN_QUICKSTART.bat`을 더블클릭합니다.
3. 처음 실행 시 API 키를 붙여넣고 Enter를 누릅니다.
4. 브라우저가 열리면 바로 질문을 입력합니다.

초기 준비가 끝난 뒤 앱만 다시 열 때는 `RUN_APP.bat`을 사용하세요. 이 파일은 `.venv`와 `.env`를 확인하고 retrieval runtime의 catalog와 active snapshot을 검증한 뒤 Streamlit GUI를 실행합니다. 패키지 설치·리포트 수집·임베딩은 반복하지 않습니다.

자세한 실행 방법과 자주 생기는 문제는 [Quick Start](docs/getting-started/QUICK_START.md)를 참고하세요.

## 주요 기능

현재 앱에 수집되어 검색 가능한 리포트를 바탕으로 다음과 같은 질문에 답할 수 있습니다.

| 하고 싶은 일 | 질문 예시 | 얻을 수 있는 답 |
| --- | --- | --- |
| 기간·종류별 리포트 찾기 | “지난주에 발간된 기업, 산업, 경제 리포트를 각각 알려줘.” | 기간과 카테고리별 리포트 목록, 발간 건수, 증권사와 대상 기업 |
| 특정 기업 분석하기 | “삼성전자 최근 리포트에서 실적 전망과 목표주가의 근거를 정리해줘.” | 여러 리포트에 나온 실적 전망, 주요 근거, 성장 요인과 위험 요인 |
| 산업·경제 흐름 파악하기 | “최근 반도체 산업 리포트의 공통 전망과 핵심 리스크는 무엇이야?” | 여러 증권사의 공통 관점, 주요 이슈, 전망이 갈리는 지점 |
| 리포트 비교하기 | “SK하이닉스에 대한 증권사별 전망 차이를 비교해줘.” | 리포트별 핵심 주장과 근거, 공통점과 차이점 |
| 발간 현황 집계하기 | “7월에 하나증권이 발간한 기업 리포트는 몇 건이야?” | 날짜·월·분기·연도, 리포트 종류, 기업, 증권사별 건수와 목록 |
| 섹터 관련 기업 찾기 | “반도체 섹터에 속한 기업 중 최근 리포트가 있는 회사를 알려줘.” | 해당 업종의 국내 상장기업과 현재 검색 가능한 관련 리포트 |
| 답변을 이어서 탐색하기 | “그중 산업 리포트만 자세히 설명해줘.” | 직전 답변의 기간과 범위를 이어받은 후속 분석 |
| 최근 주가와 함께 보기 | “삼성전자 리포트 내용과 최근 주가를 함께 알려줘.” | 리포트 분석과 필요한 경우 국내 상장사의 최근 주가 정보 |

질문의 답변 범위는 현재 앱에 수집되고 색인된 리포트에 따라 달라집니다.

## 문서 지도

전체 문서 목록과 읽는 순서는 [docs/README.md](docs/README.md)를 기준으로 합니다. 자주 찾는 항목은 다음과 같습니다.

| 찾는 것 | 문서 |
| --- | --- |
| 실행·설치·자주 생기는 문제 | [Quick Start](docs/getting-started/QUICK_START.md) |
| API 키 발급 | [OpenRouter API 키](docs/getting-started/OPENROUTER_API_KEY.md) |
| `.env`·모델·검색 설정 | [API 설정 가이드](docs/getting-started/API_SETUP.md) |
| 구현 구조·검색 흐름 | [Architecture](docs/architecture/ARCHITECTURE.md) |
| Monitoring Mode 화면·지표 | [Monitoring](docs/operations/MONITORING.md) |
| 신고 기반 개선 루프(현재 구현 권위) | [개선 루프](docs/operations/IMPROVEMENT_LOOP.md) |
| PDF 추출 엔진 비교 | [PDF 추출 엔진 비교](docs/reference/PDF_EXTRACTION_COMPARISON.md) |
| 테스트 실행 | [TESTING](docs/reference/TESTING.md) |
| 기능 변경 내역 | [Changelog](docs/reference/CHANGELOG.md) |

## 기존 데이터 마이그레이션과 재구축

### 기존 V1 사용자는 먼저 마이그레이션하세요

V1의 `reports.db`와 `vector_db`를 사용 중인 기존 사용자는 업데이트된 앱을 실행하기 전에 프로젝트 루트의 `MIGRATE_V2.bat`을 실행하세요. 이 작업은 기존 청크와 FAISS 벡터를 그대로 재사용하므로 전체 PDF 재처리나 전체 재임베딩 비용이 발생하지 않습니다. 자세한 내용은 [V1 → Native V2 사용자 마이그레이션](docs/reference/migrations/v2/V2_MIGRATION_USER.md)을 참고하세요.

### `tools\recovery\REBUILD_V2.bat`은 언제 필요한가

> **대부분의 사용자는 `tools\recovery\REBUILD_V2.bat`을 실행할 필요가 없습니다.** 활성 Native V2 snapshot을 현재 추출·임베딩 설정으로 전체 재구축해야 할 때만 사용하세요.

| 하려는 작업 | 실행할 항목 |
| --- | --- |
| 처음 설치 | `RUN_QUICKSTART.bat` |
| 기존 V1 데이터를 그대로 전환 | 앱을 모두 닫고 `MIGRATE_V2.bat` |
| 리포트 추가·변경 | 앱의 일반 데이터 업데이트 — 재구축 불필요 |
| 파싱 실패·미임베딩 문서 재처리 | Monitoring Mode의 `운영 모니터링 → 검색 자료 준비 → DB에는 있지만 임베딩되지 않은 문서 → 모든 파싱 실패/미임베딩 문서 다시 처리` |
| 추출 정책을 바꿔 전체 재구축 | 먼저 `tools\recovery\REBUILD_V2.bat --check` |
| 모델·chunk 설정만 바꿔 전체 재구축 | `tools\recovery\REBUILD_V2.bat --force` |

자세한 내용은 [Native V2 전체 재구축](docs/reference/migrations/v2/V2_REBUILD.md)을 참고하세요.

## 설치

```powershell
git clone <repository-url>
cd financial-report-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Python 3.10 이상이 필요합니다. Quick Start는 3.10 미만에서 실행을 중단합니다.

## 환경 변수 설정

일반 실행에 사용하는 공개 설정의 기본값, 타입, 설명은 `src/configs/settings.py`에서 관리합니다. `.env.example`은 이 파일에서 자동 생성되는 템플릿이고, `.env`는 실제 실행값만 저장합니다.

수동으로 환경을 준비할 때는 루트의 `.env.example`을 `.env`로 복사한 뒤 `OPENROUTER_API_KEY`를 채웁니다.

```powershell
Copy-Item .env.example .env
```

전체 설정 항목과 Native V2 embedding profile 변경 주의사항은 [API 설정 가이드](docs/getting-started/API_SETUP.md)를 참고하세요.

## PDF 파일명 규칙

다운로드된 PDF는 기본적으로 `data/downloaded/` 아래에 저장됩니다. 파일명은 DB 메타데이터 파싱에 사용하므로 아래 규칙을 따릅니다.

```text
[카테고리]_[YYYY-MM-DD]_[대상]_[증권사]_[제목].pdf
```

예시:

```text
company_2026-05-29_NAVER_미래에셋증권_기업 분석.pdf
industry_2026-05-20_반도체_신한투자증권_HBM 전망.pdf
economy_2026-05-15_null_한국투자증권_금리 전망.pdf
```

## 상장기업 업종 데이터

섹터나 분야에 속한 기업을 묻는 질문은 레포에 포함된 KRX 상장법인 업종 CSV를 회사 universe lookup에 사용합니다. 원본 데이터는 KRX 상장법인목록 페이지에서 내려받은 Excel을 `company_name,industry,main_products` CSV로 변환한 것입니다.

- 출처: https://kind.krx.co.kr/corpgeneral/corpList.do?method=loadInitPage
- 기본 데이터 파일: `data/listed_company_industries.csv`
- 명시 설정: `.env`의 `COMPANY_INDUSTRY_DATA_PATH`에 CSV 경로를 지정합니다.

## 리포트 다운로드와 임베딩

```bash
python -m src.core.report_crawler   # 리포트 다운로드
python -m src.core.embed_pipeline   # 임베딩 인덱스 생성
```

크롤러 옵션(`CRAWLER_MODE`, `CRAWLER_CATEGORIES`, `CRAWLER_LOOKBACK_DAYS` 등)과 임베딩 파이프라인의 증분 갱신·복구 경계는 [API 설정 가이드](docs/getting-started/API_SETUP.md)와 [연속 업데이트](docs/operations/CONTINUOUS_UPDATES.md)를 참고하세요.

## 실행

### Streamlit GUI

권장 실행 방식입니다.

```bash
streamlit run apps/gui/app.py
```

### CLI (deprecated)

CLI 모드는 유지보수 전용입니다. 신규 기능 개발은 중단하며, 일반 사용은 Quick Start 또는 Streamlit GUI를 권장합니다. 기존 자동화 호환성을 위해 `--status` 등 최소 동작만 유지합니다.

```bash
python apps/cli/app.py --status
```

## 검색 및 답변 흐름

질문 준비(`query_rewrite`·`search_scope_prepare`)부터 `router`가 RDB/VectorDB 경로를 고르고, VectorDB는 metadata scope를 compile해 `DIRECT`/`SELECTOR`/`ADAPTIVE` 전략으로 검색한 뒤 선택적 rerank와 최신성 가중치를 적용하는 전체 흐름은 [Architecture](docs/architecture/ARCHITECTURE.md)를 참고하세요.

## Monitoring Mode

`MONITORING_MODE=true`는 로컬 `개별 Chat Monitoring`과 `개선 실험`을 엽니다. production 운영자 `Monitoring`은 `DEPLOYMENT_ENVIRONMENT=production`과 Supabase URL·publishable key·operator Function URL·`MONITORING_ARTIFACT_ROOT`까지 설정해야 노출됩니다.

현재 구현의 저장 권위, 강제 gate, 권장 운영 순서, 알려진 제한은 [사용자 신고 기반 개선 루프](docs/operations/IMPROVEMENT_LOOP.md), 화면·지표·trace 세부 계약은 [Monitoring](docs/operations/MONITORING.md)을 기준으로 합니다.

## 테스트

```bash
python -m pytest -q
```

테스트 구간과 느린 통합·benchmark 구간 구분은 [TESTING](docs/reference/TESTING.md)을 참고하세요.

## 주의사항

- 이 프로젝트는 투자 조언이나 매수/매도 추천을 제공하지 않습니다.
- 오래된 PDF와 잘못 추출된 표, 누락된 리포트는 답변 품질에 영향을 줄 수 있습니다.
- `.env`에는 실제 API 키가 들어가므로 커밋하지 마세요.
- 로컬 GUI에서 PDF `열기` 기능은 Streamlit 서버가 실행 중인 PC 기준으로 동작합니다. 원격 서버에 배포하면 서버 PC에서 파일을 열려고 시도합니다.

## TODO

- [x] 일반 사용자 UX와 분리된 Monitoring Mode의 진입 방식과 노출 범위를 정합니다.
- [x] 데이터 준비 상태와 검색 가능 여부를 한눈에 파악할 수 있는 대시보드 방향을 잡습니다.
- [x] 질문 처리 흐름을 추적해 검색 실패, 라우팅 오류, 답변 품질 저하 원인을 확인할 수 있게 합니다.
- [x] 기존 active V2 profile에 다른 추출 정책을 자동으로 섞지 않고, 정책 변경을 검증된 full-corpus successor 경계로 제한합니다.
- [x] PyMuPDF가 실패한 문서를 OpenDataLoader로 즉시 한 번 재시도하고 V2 profile에 fallback 정책을 기록합니다.
- [x] 두 추출기가 모두 실패한 V2 문서를 manifest 제외 상태로 기록하고 나머지 문서의 snapshot 게시를 계속합니다.
- [ ] 미래에셋 이미지형 PDF 실패군에서 OpenRouter `mistral-ocr`와 `qwen/qwen3-vl-32b-instruct`를 동일한 페이지 표본으로 비교하고, 한글 CER·숫자 exact-match·표 cell F1·누락/환각률·페이지당 비용·latency를 기준으로 OCR fallback 채택 여부와 임계값을 결정합니다. 이후 실험을 통과한 OCR 경로를 PyMuPDF·OpenDataLoader 이후의 조건부 fallback으로 추가합니다.
- [ ] 상세 parser 오류·시도 횟수와 fallback 사용 추이를 별도 진단 이력으로 관측할 수 있게 합니다.
- [ ] parsing·chunking·retrieval·rerank·모델 변경의 품질, 답변 변화량, 비용/latency를 비교할 수 있는 관측 지표를 정리합니다.
- [x] 신고 릴리스 Baseline과 다른 Candidate 릴리스를 같은 `case_contract_id`로 반복 실행하고 정성 Comparison을 저장하는 release-scoped 비교 흐름을 마련합니다.
- [ ] 여러 이슈를 묶는 versioned evaluation suite, 자동 품질 gate, promotion·canary·rollback 흐름을 마련합니다.
- [x] Monitoring Mode가 일반 실행 경로에 영향을 주지 않는지 회귀 테스트로 보호합니다.
- [x] Native V2를 V1 SQLite·FAISS·pickle 경로에서 분리하고 기본 설치의 `langchain-community` 의존성을 제거한 뒤, 일회성 마이그레이션 완료 시 남은 V1 artifacts를 삭제합니다.
- [x] 복수 기업 질문을 retrieval-only LangGraph `Send` fan-out과 단일 fan-in·rerank·답변·전역 citation으로 처리합니다.
- [ ] 질문별 실행 경로를 효율적으로 구성하기 위한 `Plan Compiler` 도입을 검토합니다.
