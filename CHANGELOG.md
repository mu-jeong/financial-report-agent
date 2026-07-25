# 변경 이력 (Changelog)

이 파일은 프로젝트의 주요 변경 사항을 간단히 기록합니다.

## v0.5.1 - 2026-07-25

### Added

- V1 원본을 유지한 채 기존 청크와 벡터를 재사용해 쓰기 가능한 native V2로 전환하는 `MIGRATE_V2.bat` 사용자 마이그레이션을 추가했습니다.
- 과거 OpenDataLoader-first V2를 PyMuPDF primary·OpenDataLoader fallback 정책으로 전체 재생성하는 `REBUILD_V2.bat` 안전 복구 경로를 추가했습니다.
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
