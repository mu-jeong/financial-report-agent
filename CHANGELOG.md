# 변경 이력 (Changelog)

이 프로젝트의 모든 주요 변경 사항은 이 파일에 기록됩니다.

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
