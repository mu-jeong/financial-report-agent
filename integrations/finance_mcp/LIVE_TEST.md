# 실환경 MCP 검증 — 2026-09-19

실제 로컬 corpus와 OpenRouter를 사용한 SDK 검사, 등록된 MCP를 사용하는 새 Codex CLI 세션 모두 성공했습니다. 모의 embedding을 사용하지 않았습니다.

## 적용한 연결

- Codex 사용자 설정의 `finance-research` stdio 서버를 활성화했습니다.
- 전용 `.venv` Python과 `__main__.py`, `--repo-root`를 절대 경로로 지정했습니다.
- `PYTHONUTF8=1`, Python `-X utf8`, 시작 제한 30초, 도구 호출 제한 120초를 설정했습니다.
- 프로젝트의 기존 `.env`를 사용합니다. API 키를 MCP 설정에 복사하지 않았습니다.
- 변경 전 사용자 설정은 `~/.codex/config.toml.before-finance-mcp-20260919`에 백업했습니다. 다른 서버의 영구 설정은 유지했습니다.
- 연결 설정 방식: [공식 Codex MCP 문서](https://developers.openai.com/codex/mcp/).

## 실제 데이터 검사

`live_smoke.py`가 공식 SDK 클라이언트로 서버 프로세스를 실행하고 13개 도구 호출을 검증했습니다.

| 검사 | 결과 |
|---|---|
| 초기화·도구 발견 | 5개 도구 발견, 정상 종료 |
| 상태·통계 | 7,095건, 2026-01-02~2026-09-18 |
| 유형별 통계 | 기업 6,462 / 산업 584 / 경제 49 |
| 목록 페이지 | 다음 페이지와 문서 ID 중복 없음 |
| 기업 필터 | 삼성전자 보고서 95건 |
| 실제 OpenRouter 검색 | 기업 필터 검색·전체 검색 각 3개 결과 |
| 본문 페이지 | 300자 + 다음 300자가 처음부터 읽은 600자와 일치 |
| 잘못된 날짜·없는 문서 | `INVALID_ARGUMENT`, `REPORT_NOT_FOUND` |
| 데이터 일관성 | 시작·종료 revision 동일 |

검색 첫 호출은 약 9.1초, 같은 프로세스의 후속 검색은 약 1.0초, 본문 호출은 약 10.8~11.8초였습니다. 단일 로컬 실행의 관측치이며 성능 보장은 아닙니다.

## 실제 LLM 클라이언트 검사

새 `codex exec --ephemeral --json` 실행에서 등록된 `finance-research`를 필수 서버로 지정했습니다. 다른 MCP는 해당 실행에서만 비활성화했습니다. LLM이 다음 인자를 직접 전달하고 반환값을 연결했습니다.

1. `get_status`: `ready`, 보고서 7,095건.
2. `search_reports`: `삼성전자 메모리 실적 전망`, 삼성전자·기업 필터, `top_k=1`.
3. `read_report`: 검색에서 받은 실제 `report_uid`와 `meta.revision`, `max_chars=500`.

검색 결과는 **「판도가 바뀌었다!」 / 2026-04-08 / IBK투자증권**입니다. 해당 문서의 색인 본문 1,126자 중 첫 500자와 다음 페이지 커서가 반환되었습니다. 세 도구 호출이 완료되고 Codex가 결과를 한국어로 보고했으며 프로세스는 종료 코드 0으로 끝났습니다. 이 실행에서 본문 읽기는 약 21.5초였습니다.

초기 클라이언트 검증 스크립트는 PowerShell 파이프의 인코딩 때문에 한글 필터가 `????`로 전달되어 결과가 없었습니다. 입력 스크립트를 ASCII/Unicode escape 기반으로 수정한 뒤 위 검증을 통과했습니다. MCP 서버 코드의 수정은 필요하지 않았습니다.

## 증거와 재실행

로컬 증거 파일은 Git에서 제외되는 `.omx/logs/`에 있습니다.

- `finance-mcp-live-smoke-2026-09-19.json`: 13개 호출, 응답 시간, 출처, 페이지 검증.
- `finance-mcp-codex-live-events.jsonl`: 실제 Codex MCP 호출 인자와 결과.
- `finance-mcp-codex-live-result.txt`: Codex 최종 응답.

재실행 명령은 [README.md](README.md)의 검증 절에 있습니다. 실제 OpenRouter embedding 요청 2회가 발생합니다. 위 Codex 검사는 추가 검색 1회를 수행했습니다.

확장 회귀 테스트 **56 passed**(기존 SWIG 경고 3개), Python 파일 13개 구문 검사, `pip check`도 통과했습니다. 기존 tracked 앱 파일에 변경이 없음을 `git diff --exit-code`로 확인했습니다.

이번 추가 파일은 `live_smoke.py`, 이 검증 기록이며 README와 호환성 기록을 갱신했습니다. 기존 서비스 구조를 그대로 사용해 별도 테스트 프레임워크나 의존성을 추가하지 않았습니다.

범위: 현재 corpus의 조회·검색·색인 본문 읽기를 검증했습니다. 보고서 다운로드·재색인은 이 MCP의 도구 범위에 없으며 이번 검사에도 포함하지 않았습니다. 데스크톱 UI는 별도로 조작하지 않았고 실제 호스트 검증은 Codex CLI에서 수행했습니다. PDF 원문과 추출 텍스트의 수치 일치 여부는 검증하지 않았습니다.
