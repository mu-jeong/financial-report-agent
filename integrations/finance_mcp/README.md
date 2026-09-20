# Local Finance MCP

로컬 Finance Report Agent 데이터를 LLM이 검색하고 읽을 수 있게 하는 선택적 stdio MCP 서버입니다. 데이터와 서버는 로컬에 있고, 의미 검색의 query embedding은 기존 OpenRouter를 사용합니다. 응답 본문은 연결한 LLM 클라이언트에 전달됩니다.

## 기존 앱과의 분리

- 기존 `src/`, `apps/`, 설정, 실행 배치 파일을 수정하지 않습니다.
- MCP 코드·테스트·문서·추가 의존성은 이 디렉터리에 있습니다.
- 기존 기능 호출과 catalog SQL은 `upstream_adapter.py`에만 있습니다.
- MCP 전용 가상환경을 사용하므로 기존 앱 `.venv`를 변경하지 않습니다.
- 크롤링, 재색인, 복구, 완성형 답변 생성은 이 서버가 수행하지 않습니다. 기존 앱에서 데이터를 준비하면 MCP가 같은 Native V2 데이터를 읽습니다.

## 설치

Python 3.10 이상, 기존 프로젝트의 다운로드·색인이 준비된 데이터가 필요합니다. 프로젝트 루트에서 실행합니다.

```powershell
python -m venv integrations\finance_mcp\.venv
integrations\finance_mcp\.venv\Scripts\python.exe -m pip install -r requirements.txt -r integrations\finance_mcp\requirements.txt
integrations\finance_mcp\.venv\Scripts\python.exe -m pip check
```

기존 `.env`의 OpenRouter 및 embedding 설정을 사용합니다. 검색 모델은 기존 색인의 embedding profile과 일치해야 합니다. API 키는 도구 인자로 전달하지 않습니다. 상태·목록·통계·본문 조회에는 OpenRouter 호출이 없습니다.

`get_status`의 `ready`는 catalog 조회가 준비됐다는 뜻입니다. 빠른 metadata 조회를 위해 전체 FAISS 파일 검증은 하지 않으며 `snapshot_validated: false`로 표시합니다. 실제 의미 검색은 Native reader가 인덱스를 검증한 뒤 실행합니다.

## 실행과 클라이언트 연결

프로젝트 루트에서:

```powershell
integrations\finance_mcp\.venv\Scripts\python.exe -m integrations.finance_mcp
```

stdio 서버이므로 터미널에서 실행하면 MCP 입력을 기다립니다. HTTP 포트나 웹 페이지는 열지 않습니다. MCP 클라이언트가 서버 프로세스를 실행하도록 command/args를 등록하는 것이 일반적인 사용법입니다.

클라이언트의 작업 디렉터리가 달라도 실행할 수 있도록 Python과 `__main__.py`의 절대 경로를 지정합니다. 다음은 command/args를 받는 클라이언트에 입력할 값입니다. 클라이언트별 설정 파일의 최상위 구조는 해당 클라이언트 형식을 따릅니다.

```json
{
  "command": "C:/path/to/finance_llm/integrations/finance_mcp/.venv/Scripts/python.exe",
  "args": [
    "C:/path/to/finance_llm/integrations/finance_mcp/__main__.py",
    "--repo-root", "C:/path/to/finance_llm",
    "--data-root", "C:/path/to/finance_llm/data",
    "--embedding-timeout", "30"
  ]
}
```

실제 저장 위치로 경로를 바꾸세요. `--data-root`를 생략하면 시작 설정의 데이터 경로를 사용합니다. 로그는 stderr, MCP 메시지는 stdout으로 분리됩니다. 서버 하나당 한 요청을 실행하며 실행 중 추가 요청은 `BUSY`를 반환합니다. 클라이언트별 프로세스는 각각 독립적으로 실행됩니다.

## 도구

| 도구 | 용도 | 주요 인자 |
|---|---|---|
| `get_status` | 준비 상태와 보유 자료 확인 | 없음 |
| `list_reports` | 정확한 metadata 조건으로 문서 목록 조회 | `filters`, `limit`, `cursor` |
| `get_report_stats` | 문서 수와 그룹별 집계 | `filters`, `group_by`, `limit`, `cursor` |
| `search_reports` | query embedding으로 근거 발췌 검색 | `query`, `filters`, `top_k` |
| `read_report` | 문서 ID로 저장된 추출 본문 읽기 | `report_uid`, `expected_revision`, `max_chars`, `cursor` |

필터는 `target_names`, `report_types`, `brokers`, `report_date_start`, `report_date_end`입니다. 날짜는 `YYYY-MM-DD` 형식이며 양끝을 포함합니다. 기업·증권사 이름은 정확히 일치해야 합니다. 필터 생략은 제한 없음, 빈 배열 `[]`은 결과 없음입니다. `report_types`는 `company`, `industry`, `economy`를 지원합니다.

`group_by`는 `report_type`, `broker`, `target_name`, `report_date` 중 하나입니다. 생략하면 전체 건수를 반환합니다. 통계는 검색 청크 수가 아니라 문서 수입니다.

호출 예:

```json
{
  "query": "삼성전자 실적 전망의 주요 근거",
  "filters": {
    "target_names": ["삼성전자"],
    "report_date_start": "2026-09-01",
    "report_date_end": "2026-09-19"
  },
  "top_k": 8
}
```

검색/목록의 `report_uid`와 응답 `meta.revision`을 다음 `read_report`의 `report_uid`/`expected_revision`에 전달합니다. 본문은 색인에 저장된 추출본입니다. PDF의 모든 페이지·표·이미지가 포함됐다고 간주하면 안 됩니다. 서버는 없는 페이지 번호나 원문 URL을 생성하지 않습니다.

## 출력·페이지·오류

성공 출력은 `schema_version`, `data`, `meta`를 가지며 MCP `structuredContent`와 text content로 제공됩니다. `meta`에는 `request_id`, `applied_filters`, `revision`, `elapsed_ms`, `warnings`, `truncated`, `next_cursor`가 있습니다.

- 목록 기본 20/최대 100건, 검색 기본 8/최대 30개, query 최대 4,000자입니다.
- 본문은 기본 12,000/최대 30,000자를 반환합니다. `next_cursor`가 있으면 같은 도구·문서·필터로 이어 읽습니다.
- 커서는 서버 인스턴스에 묶여 있습니다. 서버 재시작 시 첫 페이지부터 다시 요청합니다.
- 데이터 갱신으로 revision이 달라지면 `STALE_REVISION`이 발생합니다. 새 검색/목록 결과를 얻고 이어 읽습니다.
- 결과 없음은 정상 성공입니다. 오류는 MCP `isError`와 JSON `code`, `message`, `retryable`로 구분합니다.
- `INVALID_ARGUMENT`: 입력/커서 수정, `NOT_READY`: 기존 앱에서 데이터 준비, `REPORT_NOT_FOUND`: 문서 재조회, `UPSTREAM_INCOMPATIBLE`: main 호환성 확인, `PROVIDER_UNAVAILABLE`/`TIMEOUT`: provider 설정·연결 확인, `BUSY`: 현재 요청 종료 후 재시도.

의미 검색은 내부 답변 생성, query rewrite, rerank, 최신성 가중치를 추가하지 않는 retrieval-only 기능입니다. GUI와 순위가 완전히 같다는 보장은 없습니다. 검색 결과의 score는 확률이 아닙니다. provider의 자동 재시도는 하지 않으며 timeout은 개별 HTTP 요청 제한입니다. MCP 취소가 이미 실행 중인 동기 요청을 즉시 중단한다고 보장하지 않습니다.

## 검증 및 rebase

실제 로컬 corpus와 OpenRouter를 함께 검증하려면 다음 명령을 별도로 실행합니다. 실제 embedding 요청 **2회**가 발생하며, 삼성전자 보고서가 색인되어 있어야 합니다. 상태·통계·목록·의미 검색·본문 이어 읽기·오류 응답과 종료를 검사하고 JSON 증거를 저장합니다.

```powershell
integrations\finance_mcp\.venv\Scripts\python.exe -X utf8 integrations\finance_mcp\live_smoke.py --output .omx\logs\finance-mcp-live-smoke.json
```

실제 Codex 연결을 포함한 검증 결과는 [LIVE_TEST.md](LIVE_TEST.md)에 기록했습니다.

프로젝트 루트에서:

```powershell
# 기존 앱 회귀 테스트: 기존 환경
.venv\Scripts\python.exe scripts\run_fast_tests.py

# 확장 테스트: MCP 전용 환경, 명시적 경로
integrations\finance_mcp\.venv\Scripts\python.exe -m pytest -q integrations\finance_mcp\tests

# 의존성 검증
integrations\finance_mcp\.venv\Scripts\python.exe -m pip check

# rebase 이후 변경 범위 확인
git diff --name-status main...HEAD
git status --short
```

확장 테스트는 임시 Native V2 fixture와 가짜 embedding 응답을 사용합니다. 실제 OpenRouter 요금이나 사용자 corpus 변경 없이 프로토콜과 조회 계약을 검사합니다. 기존 pytest의 기본 탐색 경로를 변경하지 않았으므로 확장 테스트는 별도로 실행해야 합니다.

rebase 후 텍스트 충돌이 없어도 API·DB 스키마 호환성 테스트는 다시 실행해야 합니다. 기존 파일 변경이 생겼다면 이유를 기록하고 확장과 분리하세요. 의존 경계와 검증 기준 main 커밋은 [COMPATIBILITY.md](COMPATIBILITY.md)에 기록합니다.
