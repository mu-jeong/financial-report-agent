# 일반 사용자용 V2 마이그레이션

기존 V1 데이터가 있는 사용자는 프로젝트 폴더의 `MIGRATE_V2.bat`을 더블클릭하면 됩니다. SHA-256 파일이나 프로필 JSON을 직접 만들 필요가 없습니다. 성공하면 읽기 전용 중간 상태가 아니라 질문·검색·데이터 업데이트가 모두 가능한 V2가 바로 활성화됩니다.

## 실행 전 확인

- `RUN_QUICKSTART.bat`을 한 번 이상 실행해 `.venv`와 `.env`가 준비되어 있어야 합니다.
- 실행 중인 Finance LLM/Streamlit 창은 닫는 것이 안전합니다.
- 기본 폴더 구조인 `data/reports.db`, `data/vector_db/`, `data/downloaded/`를 사용해야 합니다. 경로를 분리한 개발자용 설치는 자동 전환하지 않습니다.
- 현재 원클릭 도구는 `USE_PARENT_CHILD=true`로 만든 V1만 지원합니다. `false`인 설치는 V1을 건드리기 전에 명확한 오류로 중단합니다.
- 현재 `.env`의 OpenRouter API 키가 유효해야 합니다. 기존 벡터와 현재 임베딩 모델이 같은 공간인지 실제 canary 호출로 확인합니다.
- 쓰기 가능한 첫 V2 snapshot에는 기존 V1 검색 snapshot에 없던 리포트가 하나 이상 필요합니다. 미임베딩 PDF가 없으면 도구가 실행일 포함 최대 8일 범위의 리포트를 한 번 수집해 보고, 새 리포트가 여전히 없으면 V1을 유지한 채 중단합니다.

## 실행 방법

1. 프로젝트 폴더에서 `MIGRATE_V2.bat`을 더블클릭합니다.
2. 창에 표시되는 8단계 검사가 끝날 때까지 기다립니다. 전체 PDF를 다시 추출·임베딩하므로 데이터 양과 API 속도에 따라 시간이 걸릴 수 있습니다.
3. `[완료]`가 표시되면 창을 닫고 `RUN_APP.bat`으로 앱을 실행합니다.

같은 파일을 다시 실행해도 이미 정상적인 V2가 활성화되어 있으면 실행 검사만 하고 새 백업이나 변환본을 만들지 않습니다.

전환 도중 창이 강제로 닫히거나 PC가 재시작되어도 다음 실행이 미완료
`cutover-journal.json`을 먼저 확인합니다. 정확히 같은 snapshot·build·predecessor·
publication generation·write epoch와 도구 소유 표식을 모두 확인한 경우에만 실행
검사를 이어서 완료합니다. 검사가 실패하면 같은 신원인 V2만 격리하고 V1으로
돌아갑니다. 성공 영수증을 기록한 직후 중단된 실행이 나중에 롤백되면 해당 영수증은
`rolled-back-receipt.json`으로 격리합니다. 신원이나 보존 경로가 달라졌다면 자동
이동하지 않고 지원이 필요한 상태로 중단합니다.

## 자동으로 수행하는 검사

마이그레이션 도구는 다음 순서로 작동합니다.

1. SQLite 온라인 백업과 FAISS/pickle 바이트 복사로 V1 백업을 만듭니다.
2. DB·벡터·문서 매핑 수와 원본 PDF SHA-256을 확인합니다.
3. 필요하면 실행일 포함 최대 8일 범위의 리포트를 한 번 확인하고, 라이브 `data` 밖의 격리된 폴더에서 V2 호환 seed를 변환합니다.
4. 현재 임베딩 모델로 기존 벡터를 다시 계산해 같은 벡터 공간인지 확인합니다.
5. 새 리포트를 포함한 전체 PDF corpus를 다시 추출·임베딩해 첫 쓰기 가능한 V2 successor를 만듭니다.
6. 격리된 V2에서 읽기와 쓰기 진입점, GUI 실행 검사를 수행합니다.
7. 크롤러·임베딩 작업과 공유하는 전환 잠금을 잡고 V1 DB·벡터·전체 PDF 목록과 바이트가 바뀌지 않았는지 다시 확인합니다.
8. 검증된 `retrieval` 폴더만 단일 rename으로 활성화한 뒤 라이브 GUI 실행 검사를 한 번 더 수행합니다.

전환 전 실패하면 라이브 V1 선택 상태는 바뀌지 않습니다. 전환 직후 실행 검사에 실패하면 도구가 소유 표식뿐 아니라 snapshot·publication generation·write epoch가 정확히 같은지 확인한 뒤에만 실패 폴더로 격리하고 V1으로 자동 복귀합니다. 다른 V2 writer가 이미 새 snapshot을 게시했다면 그 데이터를 임의로 롤백하지 않고 실패 기록을 남깁니다.

최근 리포트 확인 단계에서 새로 다운로드한 PDF는 이후 재시도에 쓸 수 있도록 `data/downloaded`에 남을 수 있습니다. 실패하더라도 기존 V1 DB와 FAISS 파일은 교체하지 않습니다.

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

성공 시에는 첫 후속 snapshot까지 이미 게시된 상태입니다.

- `RUN_APP.bat` 질문/검색: 사용 가능
- V2 네이티브 snapshot: 사용 중 (`write_epoch > 0`)
- V1 복구 브리지: 종료
- 크롤링·임베딩 등 데이터 갱신: 사용 가능

따라서 완료 후에는 평소처럼 `RUN_APP.bat`으로 앱을 열고, 앱의 데이터 업데이트 기능이나 `RUN_QUICKSTART.bat`을 사용할 수 있습니다. epoch 0만 라이브로 남기는 성공 경로는 없습니다.

## 실패 메시지별 확인 사항

- `source PDF is missing`: DB에 기록된 PDF가 `data/downloaded`에 없습니다.
- `same-space canary failed`: `.env`의 임베딩 모델이 V1 생성 당시 모델과 다르거나 API 호출에 실패했습니다.
- `a new report is required`: 미임베딩 PDF도, 최근 리포트 수집에서 새 PDF도 발견되지 않았습니다. 새 리포트가 게시된 뒤 같은 파일을 다시 실행하면 됩니다. V1은 계속 사용 가능합니다.
- `USE_PARENT_CHILD=true`: 현재 V1이 단일 레벨 chunk 설정입니다. 원클릭 마이그레이션 지원 범위가 아니며 아무 데이터도 활성화하지 않았습니다.
- `standard layout`: `DB_PATH`, `FAISS_DIR`, `SAVE_DIR`가 기본 한 폴더 구조가 아닙니다.
- `changed during migration`: 다른 앱이나 업데이트 작업이 동시에 V1 데이터를 변경했습니다. 해당 작업을 닫고 다시 실행하세요.
- `another retrieval update or V2 migration is already running`: 크롤러·임베딩·마이그레이션 중 하나가 이미 같은 데이터 폴더를 사용 중입니다. 실행 중인 작업이 끝난 뒤 다시 시도하세요.
- `another V2 migration is already running`: 중복 실행된 마이그레이션 창 하나를 종료하세요.
- `manual support`: 중단 뒤 발견한 V2의 소유권·전체 native 신원 또는 보존 경로가
  저널과 다릅니다. 도구가 다른 writer의 데이터나 충돌한 경로를 임의로 롤백하지
  않은 상태이므로 `.v2m/<실행 ID>/`를 보존하고 점검해야 합니다.

실패 기록에는 API 키를 저장하지 않습니다.
