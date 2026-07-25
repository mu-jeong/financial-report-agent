# 일반 사용자용 V2 마이그레이션

기존 V1 데이터가 있는 사용자는 프로젝트 폴더의 `MIGRATE_V2.bat`을 더블클릭하면 됩니다. SHA-256 파일이나 프로필 JSON을 직접 만들 필요가 없습니다. 성공하면 읽기 전용 중간 상태가 아니라 질문·검색·데이터 업데이트가 모두 가능한 V2가 바로 활성화됩니다.

V1과 V2의 차이, SSOT 원칙, snapshot과 복구 구조를 먼저 이해하고 싶다면
[V2 마이그레이션과 검색 아키텍처](V2_MIGRATION.md)를 참고하세요.
전환 후 상태를 한국어로 기록하려면
[V2 전환 자체 검증 확인서](V2_RELEASE_CERTIFICATION.md)를 사용하세요.

## 실행 전 확인

- `RUN_QUICKSTART.bat`을 한 번 이상 실행해 `.venv`와 `.env`가 준비되어 있어야 합니다.
- 실행 중인 Finance LLM/Streamlit 창은 닫는 것이 안전합니다.
- 기본 폴더 구조인 `data/reports.db`, `data/vector_db/`, `data/downloaded/`를 사용해야 합니다. 경로를 분리한 개발자용 설치는 자동 전환하지 않습니다.
- 현재 원클릭 도구는 `USE_PARENT_CHILD=true`로 만든 V1만 지원합니다. `false`인 설치는 V1을 건드리기 전에 명확한 오류로 중단합니다.
- 현재 `.env`의 OpenRouter API 키가 유효해야 합니다. 기존 벡터와 현재 임베딩 모델이 같은 공간인지 실제 canary 호출로 확인합니다.
- 새 리포트는 필요하지 않습니다. V1에서 실제 검색에 사용하던 청크와 벡터를 V2 snapshot으로 변환한 뒤, 임베딩 공간 호환성을 확인해 그 snapshot 자체를 쓰기 가능 상태로 승격합니다.

## 실행 방법

1. 프로젝트 폴더에서 `MIGRATE_V2.bat`을 더블클릭합니다.
2. 창에 표시되는 8단계 검사가 끝날 때까지 기다립니다. PDF 전체 재파싱이나 전체 재임베딩은 하지 않지만, 기존 벡터와 현재 임베딩 모델의 호환성을 확인하는 소량의 canary API 호출은 수행합니다.
3. `[완료]`가 표시되면 창을 닫고 `RUN_APP.bat`으로 앱을 실행합니다.

같은 파일을 다시 실행해도 이미 정상적인 V2가 활성화되어 있으면 실행 검사만 하고 새 백업이나 변환본을 만들지 않습니다.

전환 도중 창이 강제로 닫히거나 PC가 재시작되어도 다음 실행이 미완료
`cutover-journal.json`을 먼저 확인합니다. 정확히 같은 snapshot·build·predecessor·
publication generation·write epoch와 도구 소유 표식뿐 아니라 V1 DB/FAISS와 전체
PDF 기준 상태가 그대로인 경우에만 실행 검사를 이어서 완료합니다. 검사가 실패하면
같은 신원인 V2만 격리하고 V1으로 돌아갑니다. 성공 영수증을 기록한 직후 중단된
실행이 나중에 롤백되면 해당 영수증은 `rolled-back-receipt.json`으로 격리합니다.
Native 신원, V1/PDF 기준 상태 또는 보존 경로가 달라졌다면 자동 이동하지 않고
지원이 필요한 상태로 중단합니다.

## 자동으로 수행하는 검사

마이그레이션 도구는 다음 순서로 작동합니다.

1. SQLite 온라인 백업과 FAISS/pickle 바이트 복사로 V1 백업을 만듭니다.
2. DB·벡터·문서 매핑 수와 원본 PDF SHA-256을 확인합니다.
3. 라이브 `data` 밖의 격리된 폴더에서 V1 청크와 벡터를 재사용하는 V2 호환 seed를 변환합니다.
4. 결정적으로 선택한 최대 64개 기존 청크만 현재 임베딩 모델로 다시 계산해 같은 벡터 공간인지 확인합니다.
5. 검증된 변환 seed의 snapshot과 build는 바꾸지 않고, write epoch를 올리고 V1 fallback을 닫아 쓰기 가능한 V2로 승격합니다.
6. 격리된 V2에서 읽기와 쓰기 진입점, GUI 실행 검사를 수행합니다.
7. 크롤러·임베딩 작업과 공유하는 전환 잠금을 잡고 V1 DB·벡터·전체 PDF 목록과 바이트가 바뀌지 않았는지 다시 확인합니다.
8. 검증된 `retrieval` 폴더만 단일 rename으로 활성화한 뒤 라이브 GUI 실행 검사를 한 번 더 수행합니다.

전환 전 실패하면 라이브 V1 선택 상태는 바뀌지 않습니다. 전환 직후 실행 검사에 실패하면 도구가 소유 표식뿐 아니라 snapshot·publication generation·write epoch가 정확히 같은지 확인한 뒤에만 실패 폴더로 격리하고 V1으로 자동 복귀합니다. 다른 V2 writer가 이미 새 snapshot을 게시했다면 그 데이터를 임의로 롤백하지 않고 실패 기록을 남깁니다.

실패하더라도 기존 V1 DB와 FAISS 파일은 교체하지 않습니다. 마이그레이션 후 새 PDF가 추가되거나 기존 PDF가 변경되면 해당 파일만 파싱·임베딩하고, 변경되지 않은 V2 청크와 벡터는 다음 snapshot에 그대로 재사용합니다. 삭제된 PDF는 provider 호출 없이 다음 complete snapshot에서 제외되며, source 변화가 없으면 새 publication을 만들지 않습니다.

## 데이터 보존 위치

각 실행 기록은 `data` 폴더 옆의 숨김 폴더 `.v2m/<실행 ID>/`에 남습니다.

- `v1/`: 복사된 V1 백업과 manifest
- `cutover-journal.json`: `PREPARED → ACTIVATED → VERIFIED` 또는
  `ROLLED_BACK` 전환을 기록한 재시작 복구 저널
- `migration-receipt.json`: 성공한 snapshot과 canary 결과
- `rolled-back-receipt.json`: 기록 후 롤백되어 더 이상 성공을 뜻하지 않는 영수증
- `failure.json`: 중단 원인
- `failed-retrieval/`: 라이브 실행 검사 실패 후 자동 격리된 V2

도구는 라이브 `reports.db`, `vector_db`, `downloaded`, `conversations.db`를 삭제하거나 교체하지 않습니다.

## 완료 후 상태

성공 시에는 변환한 seed snapshot 자체가 계속 active이며 `publication_generation=2`, `write_epoch=1`, `predecessor=NULL`인 쓰기 가능 상태입니다. 별도의 첫 후속 snapshot을 만들지 않습니다.

- `RUN_APP.bat` 질문/검색: 사용 가능
- V2 네이티브 snapshot: 사용 중 (`write_epoch > 0`)
- V1 fallback 선택: 영구 폐쇄. 봉인된 V1 호환성 bundle은 자동 삭제하지 않고 개인 백업 정책에 따라 보관
- 크롤링·임베딩 등 데이터 갱신: 사용 가능

따라서 완료 후에는 평소처럼 `RUN_APP.bat`으로 앱을 열고, 앱의 데이터 업데이트 기능이나 `RUN_QUICKSTART.bat`을 사용할 수 있습니다. epoch 0만 라이브로 남기는 성공 경로는 없습니다.

## 활성 V2 전체 재구축

`MIGRATE_V2.bat`은 V1을 V2로 처음 전환하는 도구입니다. 이미 활성화된 V2에서 primary와 fallback이 반대로 기록된 추출 profile을 배포 기본값으로 바로잡을 때는 `tools\recovery\REBUILD_V2.bat`을 사용합니다. 이 파일은 내부적으로 `scripts/migrations/v2/rebuild_v2_successor.py`를 실행해 전체 corpus를 새 embedding profile로 재구축합니다.

배포 기본 추출 정책은 다음과 같습니다.

```env
PDF_EXTRACTION_ENGINE=pymupdf
PDF_EXTRACTION_FALLBACK_ENGINE=opendataloader
UNEMBEDDED_PDF_EXTRACTION_ENGINE=pymupdf
```

PyMuPDF가 primary이며, 해당 PDF의 추출이 실패할 때만 OpenDataLoader를 fallback으로 한 번 시도합니다. active V2에 `opendataloader` primary와 `pymupdf` fallback이 기록되어 있다면 역방향 profile입니다. `--check`에서 현재 profile과 목표 profile을 확인한 뒤 전체 재구축으로 바로잡을 수 있습니다.

### 1. 실행 전 점검

Finance LLM, Streamlit, Quick Start, 데이터 업데이트 창을 모두 닫으세요. 먼저 프로젝트 폴더에서 다음 읽기 전용 점검을 실행합니다.

```bat
tools\recovery\REBUILD_V2.bat --check
```

점검은 데이터를 수정하거나 successor를 만들지 않습니다. 현재 snapshot·추출 profile·인덱싱 문서 수, 교정 후 재생성할 추출 profile·원본 PDF 수를 출력합니다. legacy `EXTRACTION_ENGINE`과 `PDF_EXTRACTION_ENGINE`이 다르면 두 primary 값도 함께 알립니다.

`[확인] 활성 프로필과 현재 설정이 일치합니다.`가 표시되고 알려진 역방향 설정 충돌도 없다면 일반 실행은 새 successor를 만들지 않고 종료합니다.

새 환경변수와 기존 alias는 다음과 같이 대응합니다.

| 새 키 | 기존 alias |
| --- | --- |
| `PDF_EXTRACTION_ENGINE` | `EXTRACTION_ENGINE` |
| `PDF_EXTRACTION_FALLBACK_ENGINE` | `EXTRACTION_FALLBACK_ENGINE` |
| `UNEMBEDDED_PDF_EXTRACTION_ENGINE` | `UNEMBEDDED_EXTRACTION_ENGINE` |

과거 공식 설정 중 다음 두 조합이면 원클릭 복구 경로가 작동합니다.

1. `EXTRACTION_ENGINE=pymupdf`, `PDF_EXTRACTION_ENGINE=opendataloader`,
   유효한 fallback이 비어 있거나 `pymupdf`
2. 유효한 PDF primary가 `pymupdf`,
   `UNEMBEDDED_PDF_EXTRACTION_ENGINE=opendataloader` 또는
   `UNEMBEDDED_EXTRACTION_ENGINE=opendataloader`, 유효한 fallback이 비어 있거나
   `pymupdf`

여기서 유효한 값은 새 `PDF_*` 키가 있으면 그 값을, 없으면 표의 기존 alias 값을 뜻합니다.

`--check`는 `.env`를 수정하지 않고 교정 후 목표 profile을
`pymupdf|fallback=opendataloader`로 미리 보여줍니다. 전체 재구축을
실행하면 도구가 화면에 안내를 표시하고 다음 세 개의 사용자용 키만 저장소 기본값으로
정리한 뒤 설정을 불러옵니다.

```env
PDF_EXTRACTION_ENGINE=pymupdf
PDF_EXTRACTION_FALLBACK_ENGINE=opendataloader
UNEMBEDDED_PDF_EXTRACTION_ENGINE=pymupdf
```

다른 사용자 지정 정책은 자동으로 덮어쓰지 않습니다. 새 primary 키와 기존 primary alias의 그 밖의 충돌은 알림으로 표시되므로, 값을 검토하고 의도한 정책으로 맞춘 뒤 다시 점검하세요.

active profile이 이미 `pymupdf|fallback=opendataloader`이고 알려진 과거 설정 충돌만 남았다면, 전체 실행은 세 키만 교정하고 successor 재생성이나 API 호출 없이 끝납니다.

### 2. 전체 재구축

점검을 통과하면 다음 명령을 실행하고 화면의 확인 질문에 답합니다.

```bat
tools\recovery\REBUILD_V2.bat
```

전체 corpus의 PDF 추출, chunk 생성, 임베딩과 검증을 다시 수행하므로 OpenRouter API 비용이 발생하고 오래 걸릴 수 있습니다. 비용과 시간은 PDF 수·길이, 청크 수, 선택한 모델과 API 가격에 따라 달라집니다. OpenDataLoader fallback을 사용하려면 Java 11 이상과 `java` 명령의 `PATH` 등록이 필요합니다. 시작 전에 Java와 OpenRouter 크레딧을 확인하고, 완료될 때까지 앱이나 데이터 업데이트를 실행하지 마세요.

실행 창에는 `[PDF 현재/전체] 파일명` 형식으로 추출 진행률이 표시됩니다. PyMuPDF가 빈 텍스트를 반환해 OpenDataLoader fallback으로 넘어간 PDF는 한 파일에도 여러 분이 걸릴 수 있습니다. 다만 한 PDF에서 5분 안에 반환하지 않으면 해당 Java 변환을 종료하고 `source-extraction-failed`로 기록한 뒤 다음 문서를 계속 처리하므로, 진행 번호가 잠시 멈춰 있어도 창을 강제로 닫지 마세요.

두 추출기가 모두 실패한 PDF는 원본을 삭제하지 않고 새 build manifest에
`excluded / source-extraction-failed`로 기록합니다. 해당 문서에는 chunk와 vector를
만들지 않지만 다음 PDF의 파싱과 전체 embedding은 계속합니다. 완료 화면의
`원본 PDF`, `인덱싱 성공`, `추출 실패/제외` 수로 부분 제외 결과를 확인할 수
있습니다.

### 3. 전환과 데이터 보존

- 재구축과 검증 중에는 기존 active V2가 계속 질문과 검색을 처리합니다.
- 성공 문서와 명시적 제외 결정을 합친 successor가 manifest·catalog·FAISS 검증을 통과한 경우에만 active 선택을 원자적으로 전환합니다.
- 개별 PDF의 이중 파싱 실패는 문서 단위 제외로 처리합니다. embedding API, profile, source inventory 또는 snapshot 무결성 검증이 실패하거나 실행을 중단하면 기존 active 선택은 바뀌지 않으며 계속 검색할 수 있습니다.
- 사용자 확인 뒤 설정 교정까지 끝난 상태에서 build가 실패했다면 교정된 `PDF_*` 값은 유지됩니다. 오류 원인을 해결한 뒤 `tools\recovery\REBUILD_V2.bat`을 다시 실행하세요. 새 successor가 성공하기 전까지 incremental update는 profile 불일치로 계속 차단될 수 있습니다.
- 원본 `data/reports.db`와 `data/downloaded`의 PDF는 삭제하지 않습니다.
- 전환 직전 active snapshot은 검증된 predecessor로 잠시 보존합니다. 전환 후 검색에는 새 snapshot만 사용하며 predecessor는 제공하지 않습니다.
- `data/retrieval/v2`를 직접 삭제하거나 파일을 옮기지 마세요. 수동 변경은 active 선택, 검증 기록과 복구 경계를 손상할 수 있습니다.

### 4. 파싱 실패 문서 재처리

일반 데이터 업데이트는 active manifest에 같은 SHA-256으로 이미 기록된
`source-extraction-failed` 문서를 자동으로 반복 파싱하지 않습니다. OpenDataLoader
fallback이 오래 걸리는 문서 때문에 매 업데이트가 지연되거나 동일한 partial
snapshot이 반복 게시되는 것을 막기 위한 동작입니다.

나중에 Java 설정이나 PDF 상태를 정비한 뒤 앱의 Monitoring Mode에서
`운영 상태 → 임베딩 누락 문서 → 파싱 실패 문서 재시도`를 누르세요. 이 명시적
재시도에서 성공하면 문서는 다음 successor에 `included`로 다시 들어갑니다.
다시 실패하면 검색 가능한 corpus는 그대로 유지하되, 명시적 재시도 시각을 새
감사 build에 남기고 실패 상태를 계속 표시합니다. 다른 대상 처리는 중단하지
않습니다.

## 실패 메시지별 확인 사항

- `source PDF is missing`: DB에 기록된 PDF가 `data/downloaded`에 없습니다.
- `source-extraction-failed`: PyMuPDF와 OpenDataLoader가 모두 텍스트를 만들지 못한 문서입니다. 원본은 보존되며 관리 화면에서 나중에 다시 시도할 수 있습니다.
- `same-space canary failed`: `.env`의 임베딩 모델이 V1 생성 당시 모델과 다르거나 API 호출에 실패했습니다.
- `USE_PARENT_CHILD=true`: 현재 V1이 단일 레벨 chunk 설정입니다. 원클릭 마이그레이션 지원 범위가 아니며 아무 데이터도 활성화하지 않았습니다.
- `standard layout`: `DB_PATH`, `FAISS_DIR`, `SAVE_DIR`가 기본 한 폴더 구조가 아닙니다.
- `changed during migration`: 다른 앱이나 업데이트 작업이 동시에 V1 데이터를 변경했습니다. 해당 작업을 닫고 다시 실행하세요.
- `another retrieval update or V2 migration is already running`: 크롤러·임베딩·마이그레이션 중 하나가 이미 같은 데이터 폴더를 사용 중입니다. 실행 중인 작업이 끝난 뒤 다시 시도하세요.
- `another V2 migration is already running`: 중복 실행된 마이그레이션 창 하나를 종료하세요.
- `manual support`: 중단 뒤 발견한 V2의 소유권·전체 native 신원 또는 보존 경로가
  저널과 다릅니다. 도구가 다른 writer의 데이터나 충돌한 경로를 임의로 롤백하지
  않은 상태이므로 `.v2m/<실행 ID>/`를 보존하고 점검해야 합니다.

실패 기록에는 API 키를 저장하지 않습니다.
