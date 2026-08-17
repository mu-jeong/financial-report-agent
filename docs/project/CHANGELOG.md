# 변경 이력 (Changelog)

이 문서는 프로젝트의 주요 변경 사항을 간단히 기록합니다.

## Unreleased

## v0.6.0 - 2026-08-17

### Added

- Native V2 업데이트가 성공한 문서를 작은 불변 단위로 즉시 검색에 반영하고, 재시작 후에도 이미 반영된 진행 상태를 이어받도록 했습니다.
- Base snapshot과 진행 중 업데이트를 한 요청 revision으로 고정해 검색하는 composite reader와 delta-aware SQL/status projection을 추가했습니다.
- 검색에서 제외된 임시 vector 파일의 정리 대기 수·용량·최장 보존 시간을 CLI 상태와 Monitoring 화면에 추가했습니다.
- 사용자 동의가 있을 때 제한된 다중 turn 질문·검색 범위를 이슈 재현 정보로 전송하고, durable outbox 기록 이후에만 접수 완료를 표시하도록 했습니다.

### Changed

- Retrieval runtime은 Native V2로 단일화하되 기존 사용자가 청크와 벡터를 재사용해 전환할 수 있도록 `MIGRATE_V2.bat`의 마이그레이션 전용 V1 판독 경계를 유지했습니다. 전환 성공 후 V1 `reports.db`와 `vector_db`는 삭제하고 `downloaded` PDF만 보존합니다.
- 런타임 V1 fallback, release attestation, benchmark gate와 고정 evaluation snapshot 도구는 현재 앱 실행 경로에서 제거했습니다.
- 전체 Monitoring의 단일 dropdown을 `운영 모니터링`과 `성능 개선 실험` 상단 내비게이션으로 나누고, 각 그룹의 마지막 선택을 기억하면서 선택한 세부 화면만 렌더링하도록 변경했습니다.
- `현재 문제`가 경고의 실제 집계값과 다음 확인 경로를 표시하도록 보강하고, Native V2 상태가 복합 active view를 반복 계산하지 않도록 고정 build manifest와 현재 delta head 기준 집계 및 PDF 수 스캔을 최적화했습니다.
- 개별 Chat Monitoring은 정확도와 trace 진단을 제외하고 최근·평균 응답시간, RDB 평균 조회시간, Vector DB 평균 검색시간과 응답별 시간 표만 표시하도록 단순화했습니다.
- Monitoring 기본 화면을 응답 속도(P95)와 correctness-only 답변 정확도로 축소하고, 응답 trace·검색 자료·평가·parsing·신고 도구는 단일 `문제 상황 자세히 보기` 영역으로 이동했습니다. 활성 discovery와 무결성 집계는 Native V2 계약만 사용하며 과거 고정 DB/vector 실행 경로는 UI에서 제거했습니다.
- Monitoring의 속도 표본과 정확도 run에 실제 Native V2 runtime provenance를 요구하고, 유효한 Native V2 상태가 아니면 평가와 상세 지표를 fail closed하도록 변경했습니다.
- V2 업데이트는 batch마다 전체 snapshot을 다시 만들지 않고, 작업 종료 시 기존 vector를 재사용한 완전한 snapshot을 한 번만 게시합니다.
- 변경 문서 파싱이 실패하면 이전 검색 가능 버전을 유지하며, GUI는 업데이트 중에도 검색 사용 가능 여부와 처리 완료 문서의 순차 반영을 안내합니다.
- 임시 vector 파일 정리를 공용 snapshot GC에 통합해 소유 base GC 직후, 모든 snapshot 게시 후, 정상 시작 시 자동 재시도합니다.
- GC는 immutable hash/size를 확인한 quarantine 파일만 삭제하며, fast startup에서도 정리를 재조정합니다. 게시 후 정리 오류는 게시 실패 대신 `cleanup_pending`으로 반환합니다.
- 업종 질의와 순번 기반 후속 질문이 의도한 리포트 범위를 유지하고, 유효하지 않은 순번은 범위를 넓히지 않도록 보강했습니다.
- 복수 기업 답변의 실행 범위·근거·성능 정보를 더 일관되게 기록하도록 개선했습니다.
- 기본 chat model 생성 temperature를 `0.1`로 조정했습니다.

## v0.5.1 - 2026-07-25

### Added

- V1 원본을 유지한 채 기존 청크와 벡터를 재사용해 쓰기 가능한 native V2로 전환하는 `MIGRATE_V2.bat` 사용자 마이그레이션을 추가했습니다.
- 과거 OpenDataLoader-first V2를 PyMuPDF primary·OpenDataLoader fallback 정책으로 전체 재생성하는 `tools\recovery\REBUILD_V2.bat` 안전 복구 경로를 추가했습니다.
- 두 PDF 추출기가 모두 실패한 V2 문서를 active manifest에 기록하고 Monitoring Mode에서 명시적으로 재시도하는 관리 경로를 추가했습니다.
- SQLite native catalog, immutable FAISS snapshot, crash-safe publication과 startup recovery, writer/update lock, launcher runtime guard를 추가했습니다.
- 마이그레이션 canary, GUI/runtime smoke, 자동 rollback과 재시작 journal 검증을 추가했습니다.

### Changed

- V2 데이터 업데이트는 전체 source inventory를 확인하되 새 문서와 변경된 문서만 파싱·임베딩하고, 변경되지 않은 청크와 벡터를 재사용합니다.
- 개별 PDF 추출 실패가 전체 V2 build를 중단하지 않도록 실패 문서만 제외하고 나머지 문서의 파싱·임베딩·successor 게시를 계속합니다.
- OpenDataLoader가 한 PDF에서 5분 넘게 반환하지 않으면 해당 변환을 종료하고 추출 실패로 기록해 V2 복구 작업이 무기한 멈추지 않도록 했습니다.
- `RUN_APP.bat`, Quick Start, CLI/GUI 진입점은 retrieval runtime을 검증한 뒤 실행하도록 변경했습니다.
- Monitoring과 평가 문서를 native V2 backend, 고정 snapshot 기준선, 현재 화면 구조에 맞게 갱신했습니다.

## v0.5.0 - 2026-07-03

### Added
- OpenRouter 기반 생성 모델, 임베딩, 선택형 rerank 설정을 정리했습니다.
- `baai/bge-m3` 임베딩 모델을 기본값으로 사용합니다.
- `cohere/rerank-v3.5` rerank 설정을 추가했지만 기본값은 비용 절감을 위해 비활성화했습니다.
- CLI/GUI 대화 내용을 SQLite `data/conversations.db`에 저장하고 재시작 후 복원할 수 있게 했습니다.
- 리포트 수집 시 카테고리, 목표 개수, lookback 범위를 설정할 수 있게 했습니다.
- 날짜/월/분기/연도 기반 metadata filter와 최신성 가중치를 강화했습니다.
- GUI 참고 문서를 기본 접힘 상태의 expander로 표시합니다.

### Changed
- 이전 LLM provider 분기와 관련 의존성을 제거하고 OpenRouter 중심으로 단순화했습니다.
- 문서의 API 설정, 아키텍처, 비용 관련 설명을 최신 구조에 맞게 갱신했습니다.
- PDF 임베딩 파이프라인의 깨진 한글 로그와 도움말 문자열을 정리했습니다.

## v0.4 - 2026-04-25

### Added
- 데이터 상태 자가 진단 기능을 추가했습니다.
- VectorDB 메타데이터 필터를 추가했습니다.
- CLI와 GUI에서 로컬 데이터 상태를 확인할 수 있게 했습니다.
- 파일명 파싱, SQL guardrail, 상태 요약, tool calling 흐름을 검증하는 테스트를 추가했습니다.

### Changed
- 임베딩 CLI에 `--limit`와 `--all` 옵션을 추가했습니다.
- LangGraph 메시지 누적 흐름을 정리했습니다.
- 주요 의존성 버전을 고정했습니다.
- README와 Architecture 문서를 당시 구조에 맞게 갱신했습니다.

## v0.3 - 2026-04-05

### Changed
- Tool calling 문맥 보존을 강화했습니다.
- 불필요한 중복 LLM 호출을 줄였습니다.
- 사용하지 않는 stock price 분기와 상태 필드를 정리했습니다.

## v0.2 - 2026-03-15

### Added
- Parent-Child Chunking을 도입했습니다.
- Marker/PyMuPDF 기반 PDF 추출 옵션을 추가했습니다.
- Parent chunk를 SQLite에 저장해 검색 문맥을 확장했습니다.

## v0.1 - 2026-03-01

### Added
- 초기 RAG 파이프라인을 구축했습니다.
- FAISS 기반 벡터 검색을 추가했습니다.
- 기본 PDF 텍스트 추출과 Streamlit UI를 제공했습니다.
