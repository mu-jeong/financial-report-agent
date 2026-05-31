# 변경 이력 (Changelog)

이 프로젝트의 모든 주요 변경 사항은 이 파일에 기록됩니다.

## [v0.4] - 2026-04-25
### 추가된 기능 (Added)
- **데이터 상태 점검 유틸 추가**: `src/core/status.py`에서 다운로드 PDF 수, SQLite 리포트/임베딩 상태, FAISS 인덱스 상태, 핵심 설정을 읽기 전용으로 집계합니다.
- **VectorDB 메타데이터 필터 추가**: `src/core/metadata_filters.py`에서 질문에 명시된 종목명, 증권사명, 리포트 유형을 추론하고 FAISS 검색 결과를 후필터링합니다.
- **CLI 상태 출력**: `python apps/cli/app.py --status` 및 대화형 `status`/`s` 명령으로 LLM 호출 없이 로컬 데이터 상태를 확인할 수 있습니다.
- **GUI 상태 표시**: Streamlit 사이드바에서 전체 리포트 수, 임베딩 완료 수, 대기 수, FAISS 크기와 `TEST_LIMIT` 경고를 보여줍니다.
- **회귀 테스트 추가**: 파일명 파싱, SQL 가드레일, 상태 스냅샷, Tool Calling 최종 응답 메시지 흐름을 pytest로 검증합니다.

### 변경 및 개선 사항 (Changed)
- **임베딩 CLI 인자 지원**: `python -m src.core.embed_pipeline --limit N` 및 `--all`로 처리 건수를 실행 시점에 제어할 수 있습니다.
- **LangGraph 메시지 delta 반환**: `final_response_node`가 tool 결과 반영 후 새 AI 메시지만 반환하도록 정리하여 `operator.add` reducer의 중복 누적 가능성을 줄였습니다.
- **의존성 재현성 강화**: 현재 검증 환경 기준으로 `requirements.txt` 주요 패키지 버전을 고정했습니다.
- **문서 현재화**: README와 Architecture 문서를 현재 파일 구조, `TEST_LIMIT=10` 상태, 상태 점검 기능, FAISS 신뢰 경계, 테스트 실행 방법 기준으로 갱신했습니다.
- **임베딩 메타데이터 보강**: 새로 임베딩되는 문서에는 `report_type`도 VectorDB metadata에 저장되도록 했습니다.

## [v0.3] - 2026-04-05
### 변경 및 개선 사항 (Changed)
- **Tool Calling 문맥 보존 강화**: RDB/VectorDB 경로에서 tool 호출이 발생해도 원질문과 DB 조회 결과 문맥이 final 응답 생성 단계까지 유지되도록 흐름을 정리했습니다.
- **Tool Calling 흐름 단순화**: `answer_agent_node`를 제거하고, `rdb_execute_node`와 `vectordb_node`가 직접 답변 생성 또는 tool 호출 여부를 결정하도록 정리했습니다.
- **중복 LLM 호출 제거**: tool이 필요 없는 경우에는 검색 노드에서 생성한 답변을 그대로 사용하고, tool이 실제 호출된 경우에만 `final_response_node`가 후처리를 담당하도록 바꿨습니다.
- **문서 및 다이어그램 동기화**: README와 Architecture 문서를 최신 LangGraph 구조에 맞게 업데이트하고, `docs/langgraph_diagram.png`도 현재 그래프 기준으로 다시 생성했습니다.
- **미사용 라우팅 잔재 제거**: 더 이상 실제 분기로 쓰이지 않던 `stock_price` 라우트 설명과 관련 state 필드를 정리해 현재 구현과 내부 정의를 일치시켰습니다.

## [v0.2] - 2026-03-15
### 추가된 기능 (Added)
- **Parent-Child Chunking (부모-자식 청킹)**: 맥락 이해도 향상을 위한 Small-to-Big Retrieval(작은 조각 검색 후 큰 맥락 확장) 패턴 구현.
- **Marker-PDF 통합**: Marker-PDF 옵션 제공.
- **부모 맥락 병합 (Parent Context Merging)**: 중복된 부모 섹션 제거를 통한 LLM 토큰 사용 최적화.

### 변경 및 개선 사항 (Changed)
- PDF 추출 로직 옵션(marker, pymupdf) 추가 및 안정성 강화.
- 부모-자식 참조 효율화를 위한 데이터베이스 스키마 최적화 (SQLite 참조 구조).
- 중복 부모 추적 로직 추가를 통한 검색 속도 및 컨텍스트 품질 개선.

## [v0.1] - 2026-03-01
### 추가된 기능 (Added)
- 프로젝트 초기 릴리즈.
- Gemini 및 FAISS 기반의 핵심 RAG 파이프라인 구축.
- 기본적인 PDF 텍스트 추출 지원.
- Streamlit 기반의 웹 사용자 인터페이스(UI) 제공.
