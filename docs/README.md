# 문서 지도

이 저장소의 문서는 **현재 구현을 설명하는 문서**로 구성된다. 같은 주제가 두 곳에 흩어지지 않도록 아래 경로를 기준으로 찾는다.

- 현재 구현의 권위: `operations/` (운영·개선 루프), `architecture/`, `getting-started/`, `reference/`

## 읽는 순서

처음 접한다면 다음 순서를 권장한다.

1. [루트 README](../README.md) — 이 앱이 무엇인지, 바로 실행하는 방법
2. [Quick Start](getting-started/QUICK_START.md) — 설치·첫 실행·자주 생기는 문제
3. [Architecture](architecture/ARCHITECTURE.md) — 구성 요소와 데이터 흐름
4. [Monitoring](operations/MONITORING.md) — 운영자 화면과 지표·trace
5. [개선 루프](operations/IMPROVEMENT_LOOP.md) — 신고 기반 재현·비교·이슈 생애주기의 현재 구현 권위

## 디렉터리별 문서

### getting-started — 설치와 설정

| 문서 | 내용 |
| --- | --- |
| [QUICK_START.md](getting-started/QUICK_START.md) | 설치·첫 실행·V1 마이그레이션·자주 생기는 문제 |
| [OPENROUTER_API_KEY.md](getting-started/OPENROUTER_API_KEY.md) | OpenRouter API 키 발급 방법 |
| [API_SETUP.md](getting-started/API_SETUP.md) | `.env` 설정, 모델·검색·크롤러 설정 |

### architecture — 구조

| 문서 | 내용 |
| --- | --- |
| [ARCHITECTURE.md](architecture/ARCHITECTURE.md) | 구성 요소, 데이터 흐름, 검색·메타데이터·rerank·메모리·신고 구조 |

### operations — 운영과 개선 (현재 구현 기준)

| 문서 | 내용 |
| --- | --- |
| [MONITORING.md](operations/MONITORING.md) | Monitoring Mode 화면·지표·trace 세부 계약 |
| [IMPROVEMENT_LOOP.md](operations/IMPROVEMENT_LOOP.md) | 신고 기반 개선 루프의 현재 구현 권위 문서 |
| [CONTINUOUS_UPDATES.md](operations/CONTINUOUS_UPDATES.md) | Native V2 연속 검색 업데이트 결정 기록 |

### reference — 참고 자료와 마이그레이션

| 문서 | 내용 |
| --- | --- |
| [CHANGELOG.md](reference/CHANGELOG.md) | 버전별 변경 이력 |
| [TESTING.md](reference/TESTING.md) | 테스트 실행 구간 |
| [PDF_EXTRACTION_COMPARISON.md](reference/PDF_EXTRACTION_COMPARISON.md) | PDF 추출 엔진 비교 |
| [migrations/v2/V2_MIGRATION_USER.md](reference/migrations/v2/V2_MIGRATION_USER.md) | V1 → Native V2 사용자 마이그레이션 |
| [migrations/v2/V2_REBUILD.md](reference/migrations/v2/V2_REBUILD.md) | Native V2 전체 재구축 |
| [migrations/v2/V2_MIGRATION.md](reference/migrations/v2/V2_MIGRATION.md) | 마이그레이션 아키텍처 |

## 루트 문서

- [README.md](../README.md) — 프로젝트 소개와 Quick Start
- [DESIGN.md](../DESIGN.md) — 디자인 시스템(영문)
