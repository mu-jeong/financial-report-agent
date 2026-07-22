# Finance LLM V2 마이그레이션과 검색 아키텍처

이 문서는 Finance LLM의 검색 저장소가 V1에서 V2로 바뀐 이유와 구조를 설명합니다.
일반적인 소프트웨어·데이터베이스·운영체제 과목을 수강한 공학 전공 학부생이 이해할
수 있는 수준을 기준으로 하며, 실제 구현을 확인하려는 독자를 위해 뒤쪽에 심화 내용을
덧붙였습니다.

실제로 마이그레이션을 실행하려는 사용자는
[일반 사용자용 V2 마이그레이션](V2_MIGRATION_USER.md)을 먼저 읽으세요. 릴리스
검증 담당자는 [V2 릴리스 인증 실행서](V2_RELEASE_CERTIFICATION.md)를 사용합니다.
이 문서는 두 절차를 대신하는 명령어 모음이 아니라, 설계 의도와 동작 원리를 설명하는
기술 문서입니다.

## 1. 먼저 알아둘 핵심

V1과 V2의 가장 큰 차이는 “데이터가 어디에 있는가”보다 “무엇을 정답으로 믿는가”에
있습니다.

- V1은 SQLite, FAISS, LangChain pickle 파일이 검색 상태를 나누어 가졌습니다.
- V2는 SQLite catalog 하나를 검색 상태의 SSOT(Single Source of Truth)로 삼습니다.
- FAISS는 V2에서도 사용하지만, 벡터와 숫자 ID만 가진 불변(immutable) 검색
  artifact입니다. 문서 내용이나 활성 버전 결정 권한은 없습니다.
- 업데이트는 사용 중인 파일을 직접 수정하지 않습니다. 새 catalog 후보와 FAISS
  snapshot을 별도 위치에서 완성하고 검증한 뒤 활성 포인터만 전환합니다.
- 마이그레이션은 기존 V1 벡터를 재사용합니다. 전체 PDF를 다시 파싱하거나 전체
  corpus를 재임베딩하지 않습니다.
- V2가 한 번 활성화된 뒤 native 상태가 손상되면 임의로 V1을 다시 선택하지 않습니다.
  잘못된 오래된 데이터로 조용히 서비스하는 것보다 명시적으로 중단하는 fail-closed
  정책을 사용합니다.

여기서 `catalog`는 단순한 문서 목록이 아닙니다. 어떤 리포트와 청크가 존재하는지,
어느 FAISS snapshot이 활성인지, 어떤 embedding profile로 만들었는지, 게시가 어디까지
완료되었는지까지 기록하는 검색 시스템의 제어 장치입니다.

## 2. V1과 V2 비교

### 2.1 설계 관점에서 무엇이 달라졌는가

V1은 “문서를 읽어 벡터 저장소에 추가한다”는 단일 작업을 비교적 단순하게 수행하는
구조입니다. 소규모 데이터나 한 명의 운영자가 수동으로 갱신하는 환경에서는 이해하기
쉽고 빠르게 시작할 수 있습니다. 그러나 데이터베이스, FAISS 파일, pickle 매핑이 서로
다른 시점에 저장될 수 있어, 업데이트 도중 실패하면 어느 파일 조합이 최신인지 판단하기
어려울 수 있습니다.

V2는 검색 데이터를 하나의 **버전이 있는 배포 단위(snapshot)** 로 다룹니다. 기존
상태를 바로 수정하지 않고 다음 후보 상태를 별도 경로에서 완성한 뒤, 검증을 통과한
후에만 활성 snapshot 포인터를 전환합니다. 소프트웨어 배포로 비유하면 V1은 실행 중인
서버의 파일을 직접 고치는 방식에 가깝고, V2는 새 릴리스를 만들어 테스트한 뒤 트래픽을
전환하는 방식에 가깝습니다.

| 관점 | V1 | V2 | 변화의 의미 |
| --- | --- | --- | --- |
| 상태 모델 | 여러 파일의 현재 상태를 함께 해석 | catalog가 활성 build와 snapshot을 명시 | “현재 검색되는 버전”을 한 곳에서 확인 |
| 갱신 단위 | 신규 row와 벡터를 기존 저장소에 추가 | 전체 corpus를 대표하는 새 revision 생성 | 신규·변경·삭제를 같은 규칙으로 처리 |
| 게시 방식 | 라이브 파일을 가변적으로 수정 | off-path 후보 생성 후 원자적으로 전환 | 실패한 후보가 사용자 검색에 노출되지 않음 |
| 식별자 | DB ID, UUID, FAISS 순번이 혼재 | 내용 기반 UID와 snapshot-local FAISS ID 분리 | 재실행·재사용·감사가 쉬워짐 |
| 검색 일관성 | 요청 중 파일이 바뀔 가능성을 별도 제어 | 요청이 하나의 revision lease를 사용 | 한 요청이 서로 다른 버전을 섞지 않음 |
| 복구 방식 | 운영자가 파일 조합을 추론 | journal과 predecessor를 이용해 규칙적으로 판단 | 자동 복구와 fail-closed 정책 적용 가능 |

### 2.2 저장 구조와 데이터 연결 방식

| 구분 | V1 | V2 |
| --- | --- | --- |
| 논리 데이터 | `data/reports.db`의 리포트·parent chunk | `catalog.sqlite3`의 report·parent·child·profile·build |
| 벡터 데이터 | `vector_db/index.faiss` | `retrieval/v2/snapshots/<snapshot_id>.faiss` |
| 벡터와 문서 연결 | LangChain `index.pkl`의 ordinal/docstore 매핑 | catalog의 `snapshot_membership(snapshot_id, chunk_uid, faiss_id)` |
| 활성 상태 판단 | DB의 `is_embedded`와 현재 파일 상태를 함께 해석 | catalog의 단일 `retrieval_runtime` row가 active build/snapshot을 지정 |
| ID | DB 정수 ID, 임의 UUID, FAISS 순번 등 목적별 ID가 혼재 | 입력 내용에서 계산한 `report_uid`, `parent_uid`, `chunk_uid`와 snapshot-local 양의 정수 ID |
| 변경 감지 | 주로 미임베딩 row 처리 | 전체 PDF inventory를 비교해 신규·변경·삭제·무변경을 구분 |
| 재사용 | 이미 처리한 문서는 건너뛰지만 새 전체 상태를 명시적으로 증명하지 않음 | 동일 `chunk_uid`의 parent·chunk·vector를 다음 complete snapshot에서 재사용 |
| 역직렬화 | native 검색에 `index.pkl` pickle 필요 | FAISS에는 벡터와 숫자 ID만 저장하고 본문·메타데이터는 SQLite에서 조회 |

V1의 `index.pkl`은 “FAISS의 n번째 벡터가 어느 문서인가”를 알려 주는 역할을 합니다.
V2에서는 이 관계를 SQLite의 membership row로 옮겼습니다. 관계형 데이터베이스의
외래키와 고유성 제약을 활용할 수 있고, pickle을 역직렬화하지 않아도 되므로 손상된
매핑이나 신뢰할 수 없는 입력을 다루는 경계도 더 명확해집니다.

### 2.3 업데이트 동작의 차이

예를 들어 A, B, C 세 개의 PDF가 있고 B가 수정되었으며 C가 삭제되었다고 가정합니다.

- V1은 보통 아직 임베딩되지 않은 row를 찾아 추가합니다. B의 이전 벡터를 어떻게
  대체하고 C의 벡터를 어떻게 제거할지는 별도 정리 로직과 운영 절차에 의존합니다.
- V2는 현재 전체 inventory와 이전 manifest를 비교합니다. A는 재사용하고, B는 새로
  추출·분할·임베딩하며, C는 후보 manifest와 membership에서 제외합니다.
- 후보 snapshot이 완전성과 무결성 검사를 통과하면 활성 포인터를 한 번 전환합니다.
  검사에 실패하면 기존 snapshot이 계속 서비스됩니다.

이 방식에서 중요한 것은 “변경된 것만 계산한다”와 “결과는 전체 상태를 표현한다”를
동시에 만족한다는 점입니다. 계산 비용은 증분 처리로 줄이면서도, 최종 snapshot은 특정
시점의 검색 corpus 전체를 설명합니다.

### 2.4 검색 동작의 차이

V1에서는 FAISS 검색 결과의 순번을 pickle/docstore에 대입해 문서를 찾습니다. 검색
필터와 문서 메타데이터가 여러 객체에 나뉘면, 서로 같은 버전인지 추가로 확인해야 합니다.

V2에서는 먼저 catalog가 허용된 검색 범위(scope)를 결정하고, 활성 snapshot의 FAISS
ID를 검색한 다음, membership을 통해 `chunk_uid`를 확인합니다. 마지막으로 같은
catalog에서 본문과 리포트 메타데이터를 읽습니다. 따라서 벡터 검색은 빠른 후보 탐색을,
SQLite는 의미·관계·정합성 검증을 담당합니다.

### 2.5 운영 안정성과 비용의 차이

| 항목 | V1의 특성 | V2의 특성 |
| --- | --- | --- |
| 동시성 | 실행 진입점별 조정에 의존 | 공용 update lock, writer lock, request lease 사용 |
| 장애 복구 | 파일 조합을 운영자가 해석할 수 있음 | publication journal, checkpoint, predecessor로 복구 판단 |
| 손상 대응 | 오래된 V1 파일로 우회할 여지가 있음 | native 흔적이 있으면 catalog를 권위로 보고 불일치 시 중단 |
| 디스크 사용량 | 현재 파일 중심이라 비교적 작음 | 후보·현재·이전 snapshot을 보관해 일시적으로 증가 |
| 구현 복잡도 | 구성요소와 상태 전이가 적음 | build, validation, publication, recovery 상태가 추가됨 |
| 추적 가능성 | 생성 시점과 입력 조합을 별도 기록해야 함 | build와 profile, manifest, checksum을 revision에 연결 |

V2가 무조건 더 단순하거나 저렴한 것은 아닙니다. 파일 수, 메타데이터, 상태 전이가
늘고 게시 전 검증 시간도 필요합니다. 대신 그 복잡성을 코드와 제약조건 안으로 옮겨,
“현재 어떤 데이터가 왜 검색되는가?”라는 질문에 재현 가능한 하나의 답을 제공합니다.
데이터가 커지거나 자동 업데이트, 동시 검색, 장애 복구가 중요해질수록 이 장점이 커집니다.

## 3. V2 전체 구조

### 3.1 세 개의 plane과 하나의 요청 경계

V2는 역할에 따라 Build plane, Control plane, Read plane으로 나뉩니다. 여기서 plane은
반드시 별도 서버를 뜻하지 않습니다. 같은 프로그램 안에 있더라도 책임과 변경 시점을
분리한 논리적 경계입니다.

```text
원본 PDF corpus
      │  경로·PDF SHA-256·메타데이터 SHA-256 비교
      ▼
┌──────────────────────── Build plane ────────────────────────┐
│ source inventory → diff → extract → chunk → embed          │
│   ├─ unchanged: 기존 parent/chunk/vector 재사용             │
│   ├─ new/changed: 새 결과 계산                              │
│   └─ deleted: 후보 전체 상태에서 제외                       │
│                                                            │
│ candidate catalog + raw FAISS snapshot + 검증 결과          │
└───────────────────────────┬────────────────────────────────┘
                            │ publication journal + writer lock
                            ▼
┌──────────────────────── Control plane ──────────────────────┐
│ SQLite catalog.sqlite3                                     │
│ reports / parents / chunks / embedding_profiles            │
│ builds / snapshots / snapshot_membership                   │
│ retrieval_runtime / publication_runs                       │
└───────────────────────────┬────────────────────────────────┘
                            │ active revision lease
                            ▼
┌───────────────────────── Read plane ────────────────────────┐
│ query + scope → query embedding → FAISS 후보 검색           │
│ → membership 검증 → SQLite 본문·메타데이터 hydrate         │
│ → 상위 parent 문맥을 포함한 검색 결과 반환                  │
└────────────────────────────────────────────────────────────┘
```

1. Build plane은 다음 후보 전체 상태를 만듭니다. 사용자 요청을 직접 처리하지 않습니다.
2. Control plane은 논리 데이터, artifact의 의미, 활성 revision을 결정합니다. catalog가
   이 층의 SSOT입니다.
3. Read plane은 요청이 시작될 때 활성 revision을 빌리고, 요청이 끝날 때까지 같은
   catalog와 FAISS snapshot만 사용합니다.

### 3.2 Build plane: 검색 가능한 후보를 만드는 곳

Build plane의 입력은 PDF 파일 목록과 embedding/chunk 정책이고, 출력은 검증 가능한
candidate revision입니다. 주요 단계는 다음과 같습니다.

1. Inventory 작성: 경로, 파일 크기, PDF 내용 SHA-256, 메타데이터 SHA-256을 수집합니다.
2. 변경 분류: 이전 manifest와 비교해 `unchanged`, `new`, `changed`, `deleted`로 나눕니다.
3. 문서 추출: 신규·변경 PDF에서 페이지 텍스트와 문서 메타데이터를 읽습니다.
4. 계층형 chunk 생성: 넓은 문맥을 보존하는 parent와 검색 정밀도를 높이는 child를 만듭니다.
5. UID 계산: 입력 내용과 처리 정책을 바탕으로 안정적인 report/parent/chunk UID를 만듭니다.
6. Embedding 생성 또는 재사용: 새 child는 모델로 계산하고, 동일한 `chunk_uid`와 호환
   profile을 가진 벡터는 이전 snapshot에서 재사용합니다.
7. 후보 조립: 전체 manifest, catalog row, membership, raw FAISS 파일을 off-path에 만듭니다.
8. 검증: row 수, UID 관계, vector 차원·metric·개수, 파일 checksum을 서로 대조합니다.

후보는 검증이 끝날 때까지 활성 검색 경로 밖에 있습니다. 따라서 4단계에서 PDF 추출이
실패하거나 8단계에서 벡터 수가 맞지 않아도 현재 사용자는 이전 정상 snapshot으로
검색할 수 있습니다.

#### Parent와 child를 나누는 이유

긴 문단 전체를 하나의 벡터로 만들면 세부 주제의 의미가 희석될 수 있고, 아주 짧게만
나누면 검색 결과만 읽었을 때 문맥이 부족합니다. V2는 작은 child chunk로 유사도를
계산한 뒤 연결된 parent chunk를 함께 반환합니다. 즉, **작은 단위로 정확히 찾고 큰
단위로 이해하도록 제공**하는 구조입니다.

### 3.3 Control plane: 상태와 규칙을 결정하는 곳

Control plane의 핵심은 `catalog.sqlite3`입니다. 테이블은 크게 네 종류의 질문에 답합니다.

| 영역 | 대표 데이터 | 답하는 질문 |
| --- | --- | --- |
| 문서 계층 | reports, parents, chunks | 어떤 문서와 본문 조각이 존재하며 서로 어떻게 연결되는가? |
| 처리 규격 | embedding_profiles | 어떤 모델, 차원, metric, 추출·chunk 정책을 사용했는가? |
| revision | builds, snapshots, membership | 어느 build가 어느 벡터 파일과 chunk 집합을 만들었는가? |
| 운영 상태 | retrieval_runtime, publication_runs | 지금 활성인 revision은 무엇이며 게시가 어디까지 진행됐는가? |

Control plane은 FAISS보다 느린 검색 엔진 역할을 하려는 것이 아닙니다. 변경이 적지만
정확해야 하는 관계와 상태를 트랜잭션, 고유키, 외래키로 보호하는 것이 목적입니다.
예를 들어 하나의 snapshot에서 같은 `faiss_id`가 두 chunk를 가리키거나, 존재하지 않는
chunk가 membership에 들어가면 검증 단계에서 거부할 수 있습니다.

### 3.4 Publication: 후보를 현재 버전으로 바꾸는 경계

Publication은 Build plane과 Read plane 사이의 전환 절차입니다. writer lock을 얻은 뒤
후보 artifact를 내구성 있게 저장하고, journal에 진행 단계를 기록하며, 마지막에
`retrieval_runtime`의 활성 포인터를 새 build/snapshot으로 바꿉니다.

핵심은 큰 FAISS 파일과 여러 catalog row를 한 번의 파일시스템 연산으로 원자화하려 하지
않는다는 점입니다. 대신 각 단계를 journal에 남겨 프로세스가 중간에 종료되어도 다음
시작 시 완료 가능한지, 이전 revision으로 돌아가야 하는지 판단합니다. 활성 포인터가
바뀌기 전에는 구버전이 계속 유효하고, 바뀐 뒤에는 새 catalog와 새 snapshot이 하나의
revision으로 취급됩니다.

### 3.5 Read plane: 한 revision 안에서 검색하는 곳

사용자 질의가 들어오면 Read plane은 다음 순서로 동작합니다.

1. 활성 revision lease를 획득해 요청이 사용할 catalog generation과 snapshot을 고정합니다.
2. 종목, 리포트 종류, 날짜 같은 검색 범위를 SQL predicate로 compile합니다.
3. 질문을 활성 embedding profile과 같은 모델·차원의 query vector로 변환합니다.
4. immutable FAISS snapshot에서 가까운 child vector 후보를 찾습니다.
5. `snapshot_membership`으로 `faiss_id`와 `chunk_uid`의 관계를 검증합니다.
6. SQLite에서 child 본문, parent 문맥, 리포트 메타데이터를 hydrate합니다.
7. scope 조건과 결과 개수 정책을 적용해 상위 결과를 반환합니다.

여기서 hydrate는 숫자 ID 중심의 검색 결과에 사람이 읽을 본문과 메타데이터를 다시
붙이는 과정입니다. FAISS가 문서 원문을 직접 저장하지 않으므로, 검색 속도에 적합한
벡터 구조와 데이터 무결성에 적합한 관계형 구조를 각각 활용할 수 있습니다.

### 3.6 요청과 업데이트가 동시에 일어날 때

Read plane은 요청 시작 시 빌린 revision을 끝까지 사용합니다. 그 사이 새 snapshot이
게시되어도 진행 중인 요청은 구 snapshot을 계속 읽고, 다음 요청부터 새 snapshot을
사용합니다. 이를 도서관에 비유하면 독자가 빌린 책은 개정판이 입고되어도 반납할 때까지
같은 판본이고, 다음 독자부터 개정판을 받는 것과 같습니다.

이 lease 경계 덕분에 query vector는 이전 profile로 만들었는데 검색은 새 차원의 FAISS를
사용하거나, FAISS 후보는 구버전인데 본문은 새 catalog에서 읽는 혼합 상태를 피할 수
있습니다. 오래된 snapshot 정리도 활성 요청의 lease가 끝난 뒤 수행해야 합니다.

### 3.7 전체 구조의 핵심 특징

- 불변 snapshot: 게시된 FAISS를 직접 수정하지 않고 새 파일을 만듭니다.
- 명시적 revision: build, catalog 상태, snapshot, membership을 하나의 버전으로 묶습니다.
- 계산과 게시의 분리: 무거운 추출·임베딩 실패가 현재 검색 서비스에 영향을 주지 않습니다.
- 계층형 검색: child로 정밀하게 찾고 parent로 충분한 문맥을 제공합니다.
- 내용 기반 식별: 같은 입력과 정책은 같은 UID를 만들어 재사용과 비교가 가능합니다.
- 검증 우선: checksum, dimension, metric, `ntotal`, membership 수가 맞아야 활성화됩니다.
- 일관된 요청: revision lease로 검색 한 번에 서로 다른 세대가 섞이지 않게 합니다.
- 복구 가능한 게시: journal과 predecessor가 중간 실패의 처리 방향을 제공합니다.

## 4. SSOT 원칙을 어떻게 적용했는가

### 4.1 무엇이 SSOT인가

V2에서 SQLite catalog는 “현재 검색 서비스가 인정하는 논리 상태”의 유일한 권위입니다.
다음 질문은 모두 catalog로 답합니다.

- 현재 활성 snapshot과 build는 무엇인가?
- 그 snapshot은 어떤 embedding model·차원·거리 metric·추출기·chunk 정책을 사용하는가?
- 어떤 리포트와 청크가 snapshot에 포함되는가?
- 각 `chunk_uid`는 FAISS의 어느 `faiss_id`와 연결되는가?
- 이전 snapshot은 무엇이며, 현재 상태는 쓰기 가능한가?
- publication이 완전히 끝났는가, 복구가 필요한가?

반면 원본 PDF는 build의 입력 원본이고, FAISS 파일은 catalog가 지시하는 파생
artifact입니다. FAISS 파일이 디스크에 존재한다는 사실만으로 활성 상태가 되지 않습니다.
파일 크기, SHA-256, vector dimension, metric, `ntotal`, membership 수가 catalog와 모두
맞고 build가 완료 상태여야 읽을 수 있습니다.

### 4.2 왜 SSOT가 필요한가

두 저장소가 같은 사실을 각각 “정답”으로 관리하면 부분 실패가 생겼을 때 판단이
모호해집니다. 예를 들어 DB는 새 문서가 임베딩되었다고 표시하지만 FAISS 저장이
끝나지 않았거나, 새 FAISS는 존재하지만 문서 매핑은 이전 버전일 수 있습니다.

V2의 SSOT 적용은 다음 장점을 줍니다.

- 일관성: 활성 snapshot, 논리 청크, 물리 벡터 ID가 하나의 revision으로 묶입니다.
- 추적 가능성: 어떤 profile과 source manifest로 build했는지 나중에 확인할 수 있습니다.
- 원자적 전환: 후보를 미리 완성한 뒤 runtime pointer를 바꾸므로 사용자가 반쪽짜리
  build를 볼 가능성이 줄어듭니다.
- 명확한 복구: journal과 generation을 보고 완료·전진 복구·rollback·중단 중 하나를
  결정할 수 있습니다.
- 안전한 최적화: FAISS를 빠른 검색용 data plane으로 유지하면서도 문서 의미와
  membership은 SQLite 제약조건으로 검증할 수 있습니다.
- 운영 단순화: `reports.db`, `index.faiss`, `index.pkl` 중 무엇이 최신인지 사람이
  추측할 필요가 없습니다.

### 4.3 SSOT가 의미하지 않는 것

SSOT라고 해서 모든 데이터를 SQLite 한 파일에 넣는 것은 아닙니다. 큰 벡터 배열은
FAISS가 더 효율적으로 처리합니다. 핵심은 물리 저장 위치를 하나로 만드는 것이 아니라,
각 artifact의 의미와 활성 여부를 결정하는 권위를 하나로 만드는 것입니다.

## 5. 일반 사용자 마이그레이션 흐름

표준 V1 설치는 `MIGRATE_V2.bat`으로 전환합니다. 내부적으로 다음 8단계를 수행합니다.

1. V1 복사본 생성
   - SQLite online backup과 FAISS/pickle 바이트 복사를 사용합니다.
   - 라이브 `reports.db`와 `vector_db`를 직접 덮어쓰지 않습니다.
2. 입력 일관성 검사
   - DB report 수, parent 수, FAISS `ntotal`, docstore 매핑, PDF SHA-256을 확인합니다.
3. V2 seed 변환
   - 라이브 `data` 밖의 격리 폴더에서 V1 청크와 기존 벡터를 V2 catalog/snapshot으로
     변환합니다.
4. same-space canary
   - 결정적으로 고른 기존 청크를 현재 embedding model로 다시 계산합니다. 기존 벡터와
     같은 공간인지 최대 64개 표본으로 검사합니다.
5. 쓰기 가능한 seed로 승격
   - 검증된 snapshot/build 자체는 바꾸지 않고 `write_epoch`을 올리고 V1 fallback을
     닫습니다.
6. 격리 환경 smoke test
   - 읽기·쓰기 진입점과 GUI가 새 V2를 정상적으로 여는지 검사합니다.
7. 최종 동시성·원본 재검사
   - 공용 update lock을 잡은 뒤 V1과 PDF corpus가 작업 중 바뀌지 않았는지 다시
     확인합니다.
8. 활성화와 라이브 smoke test
   - 검증된 `retrieval` 디렉터리를 단일 rename으로 활성 위치에 놓고 실제 실행을 다시
     검사합니다.

성공한 원클릭 경로의 최종 상태는 다음과 같습니다.

- 변환한 seed snapshot이 그대로 active입니다.
- `publication_generation=2`, `write_epoch=1`입니다.
- `v1_fallback_open=false`, `write_enabled=true`입니다.
- 아직 별도 V2 successor가 없으므로 `predecessor_snapshot_id=NULL`입니다.
- 이후 업데이트는 신규·변경 문서만 파싱·임베딩하고 무변경 vector를 재사용합니다.

중요한 점은 마이그레이션과 “새로운 embedding profile로 전체 corpus를 다시 만드는
작업”이 서로 다르다는 것입니다. 원클릭 마이그레이션의 목적은 저장 구조를 안전하게
바꾸는 것이며, 모델·추출기·chunk 정책까지 동시에 바꾸지 않습니다.

## 6. 마이그레이션 중 발생한 주요 기술 이슈와 해결

### 6.1 DB와 FAISS가 서로 다른 시점을 가리키는 문제

V1의 DB 갱신과 FAISS 파일 저장은 하나의 데이터베이스 트랜잭션으로 묶을 수 없습니다.
중간에 프로세스가 종료되면 한쪽만 새 상태일 수 있습니다.

V2는 사용 중인 snapshot을 수정하지 않습니다. 후보 catalog와 snapshot을 별도 경로에
작성하고, hash·크기·차원·개수·membership을 검증한 후에만 게시합니다. publication
journal은 파일 작성과 DB 전환 사이의 진행 단계를 기록합니다.

### 6.2 V1 vector와 문서의 정확한 대응을 보존하는 문제

V1에서 FAISS vector는 0부터 시작하는 저장 순번, `index.pkl`의 docstore ID, 문서
metadata를 함께 해석해야 의미를 알 수 있습니다. 순번 하나가 어긋나면 검색은 실행되지만
엉뚱한 문서를 인용하는 위험한 오류가 생깁니다.

변환기는 legacy mapping 수와 FAISS `ntotal`이 같은지 확인하고 vector를 재구성합니다.
V2에서는 content 기반 `chunk_uid`를 정렬해 1부터 시작하는 snapshot-local physical ID를
결정하고, 그 대응을 `snapshot_membership`에 명시적으로 저장합니다. 반복되는 동일 본문도
report/parent 문맥이 UID 계산에 들어가므로 서로 다른 논리 청크로 유지됩니다.

### 6.3 V1 생성 당시 embedding 설정을 완전히 알 수 없는 문제

예전 artifact에는 model revision, normalization 등 provenance가 충분히 남지 않았을 수
있습니다. 차원 수가 같다고 같은 vector space라고 단정할 수는 없습니다.

그래서 알 수 없는 정보는 추측해서 채우지 않고 uncertainty로 남깁니다. 이후 same-space
canary가 기존 청크 일부를 현재 provider로 다시 임베딩해 실제 호환성을 확인합니다.
canary가 실패하면 V1을 유지한 채 전환을 중단합니다.

### 6.4 전체 corpus의 완전성을 증명하는 문제

“새 문서만 처리했다”는 사실만으로 새 검색 상태가 전체 corpus를 정확히 대표한다고 할 수
없습니다. 삭제된 PDF, 이름은 같지만 내용이 바뀐 PDF, 처리 제외된 문서를 모두 설명해야
합니다.

V2는 매 build마다 발견한 전체 source를 `included` 또는 버전이 있는 명시적 제외 사유로
분할한 manifest를 만듭니다. 누락된 결정이나 허용되지 않은 제외 사유가 있으면 build가
실패합니다. 변화가 전혀 없으면 새 publication을 만들지 않습니다.

### 6.5 업데이트 비용을 줄이면서 완전한 snapshot을 만드는 문제

완전한 snapshot을 매번 만든다고 모든 PDF를 다시 임베딩하면 비용과 시간이 큽니다.

V2는 경로, source PDF hash, retrieval metadata hash, embedding profile을 비교합니다.
`chunk_uid`가 동일한 항목은 기존 parent·chunk·vector를 재사용하고 신규·변경 항목만
추출·임베딩합니다. 삭제 항목은 provider 호출 없이 다음 complete snapshot에서
제외합니다. 결과는 증분 계산이지만 게시되는 것은 언제나 완전한 corpus revision입니다.

### 6.6 reader와 writer가 동시에 움직이는 문제

검색 도중 active pointer가 바뀌면 FAISS 결과의 숫자 ID를 새 catalog membership으로
해석하는 cross-snapshot 오류가 생길 수 있습니다.

V2 reader는 요청 시작 시 SQLite read transaction과 `(publication_generation,
snapshot_id)` revision lease를 함께 잡습니다. eligibility 계산, FAISS 검색, 문서 hydrate가
모두 같은 revision에서 수행됩니다. writer는 별도의 single-writer lock을 build 계획부터
publication 종료까지 유지합니다.

### 6.7 Windows 파일 잠금과 안전한 삭제 문제

Windows에서는 열려 있는 FAISS 파일의 rename/delete가 `PermissionError`로 실패할 수
있습니다. 따라서 “이전 snapshot이니 바로 삭제”하는 방식은 안전하지 않습니다.

V2 garbage collector는 active/predecessor이거나 request lease가 남은 snapshot을 삭제하지
않습니다. 먼저 `garbage_pending` 상태를 기록하고 cache/lease가 해제된 뒤 파일 삭제를
시도합니다. OS가 거부하면 상태를 유지해 다음 reconciliation에서 재시도합니다.

### 6.8 전환 도중 종료되었을 때 누구의 데이터를 롤백할지 판단하는 문제

마이그레이션 직후 다른 writer가 새 snapshot을 게시했는데 이전 마이그레이션 도구가
무조건 폴더를 되돌리면 정상 데이터까지 잃을 수 있습니다.

원클릭 도구는 `cutover-journal.json`, owner marker, snapshot/build ID, generation,
write epoch, V1/PDF 기준 hash를 함께 확인합니다. 자기 작업과 정확히 같은 native 신원을
증명할 때만 자동 rollback합니다. 다른 writer의 개입이 보이면 자동 이동을 멈추고
`manual support` 상태로 남깁니다.

### 6.9 fallback이 오히려 장애를 숨기는 문제

새 catalog가 있는데 손상되었다는 이유로 오래된 `reports.db/vector_db`를 자동 선택하면
서비스는 실행되지만 최신 문서가 사라질 수 있습니다.

V2 footprint가 한 번 생기면 native catalog가 유일한 권위입니다. epoch 0 전환 구간에서만
hash로 봉인한 compatibility bundle을 제한적으로 사용할 수 있고, `write_epoch > 0`이 되면
V1 fallback은 단조롭게 닫혀 다시 열리지 않습니다.

### 6.10 설정 변경으로 vector space가 섞이는 문제

embedding model, dimension, distance metric, normalization, prefix, extractor, parent/child
chunk 정책이 바뀌면 기존 vector를 그대로 재사용할 수 없을 수 있습니다.

V2는 이 값을 embedding profile로 fingerprint합니다. 실행 설정이 active profile과 다르면
증분 updater는 새 snapshot 게시 전에 중단합니다. profile 변경은 별도로 검증한 full-corpus
successor 전환으로 수행해야 합니다.

### 6.11 반복되는 짧은 child의 원문 위치가 모호한 문제

실제 V1 마이그레이션에서 다음 문서의 span 복원이 중단된 사례가 있었습니다.

- 대상: 한화에어로스페이스 / 키움증권
- 제목: 하반기 실적과 수주 동반 개선 기대
- legacy parent ID: `97edba16-2df0-4dbe-9dba-12b52045f806`
- 문제 child: parent 안의 네 번째 child이며 본문은 마침표 `.` 한 글자

이 parent에서 앞 child의 시작 위치는 519, 다음 child의 시작 위치는 1020이었습니다.
그 사이에서 `.`은 576, 608, 653, 719, 774, 812, 870, 945, 1016의 9곳에서 발견됩니다.
V1은 child 본문과 순서는 저장했지만 정확한 문자 offset은 저장하지 않았으므로, 단순한
문자열 검색만으로는 9곳 중 과거의 위치를 하나로 증명할 수 없습니다.

현재 importer는 모든 child 본문을 parent에서 찾고, 시작 위치가 증가하는 전역 배치가
정확히 하나일 때만 변환합니다. 가능한 배치가 여러 개면
`legacy children have multiple valid global span assignments`로 의도적으로 중단합니다.
실패가 activation이나 rollback보다 앞선 import 단계에서 발생하므로 V1 원본, 활성
snapshot, FAISS vector가 손상된 것은 아닙니다. 같은 입력과 같은 코드로 재실행하면 같은
지점에서 다시 실패합니다.

이 문제는 세 조건이 결합해 발생합니다.

1. V1 splitter가 구두점 하나만으로 된 child를 만들 수 있습니다.
2. V1 docstore에는 `child_index`는 있지만 `span_start`가 없습니다.
3. V2의 `chunk_uid`와 hydrate 검증에는 유효하고 결정적인 span이 필요합니다.

fail-closed 자체는 올바른 보호 장치입니다. 다만 현재 규칙은 “과거의 물리 위치가
유일해야 한다”를 요구합니다. 반복되는 동일 문자열에서는 과거 위치를 알 수 없더라도
본문, embedding text, vector, child 순서 등 V1에서 관찰 가능한 결과를 모두 보존하는
결정적 표현을 만들 수 있으므로 복원 계약을 더 정교하게 정의할 필요가 있습니다.

### 6.12 최종 제안: splitter replay 기반 canonical span 복원

#### 6.12.1 제안의 핵심

최종 제안은 기존 fail-closed 정책을 없애는 것이 아니라 span resolver를 두 단계로
확장하는 것입니다.

1. Unique occurrence proof
   - 현재 방식처럼 모든 occurrence를 전역적으로 검사합니다.
   - 가능한 시작 위치 배치가 정확히 하나라면 그 span을 사용합니다.
2. Exact splitter replay canonicalization
   - 가능한 배치가 여러 개일 때만 V1과 같은 splitter 정책을 parent 전체에 재실행합니다.
   - replay 결과 전체가 보관된 V1 child 목록과 정확히 같을 때만 replay가 계산한
     `start_index`를 canonical span으로 채택합니다.
3. Fail closed
   - 배치가 없거나, replay 결과가 하나라도 다르거나, 필요한 정책을 확인할 수 없으면
     현재와 동일하게 마이그레이션을 중단합니다.

여기서 canonical은 “V1이 실제로 어느 마침표를 사용했는지 역사적으로 증명했다”는 뜻이
아닙니다. V1 artifact만으로 그 사실은 복구할 수 없습니다. 대신 동일한 입력과 정책에서
항상 같은 위치를 선택하며 V1의 관찰 가능한 동작을 바꾸지 않는 표준 표현을 정의한다는
뜻입니다.

#### 6.12.2 replay 입력과 처리 순서

replay는 PDF 재추출, embedding API 호출, FAISS 재학습을 사용하지 않습니다. 복사해
hash로 봉인한 V1 artifact에서 다음 값만 읽습니다.

- parent 원문
- `child_index`로 정렬한 legacy embedding text
- profile의 embedding prefix template
- child chunk size, overlap, separators
- 보관된 embedding text SHA-256과 FAISS ordinal
- 가능한 경우 V1 splitter library version provenance

처리 순서는 다음과 같습니다.

```text
legacy embedding texts
      │ 정확한 prefix 제거
      ▼
ordered legacy child bodies
      │
parent content ── V1 child policy + add_start_index=True ──► replay children
      │                                                      │
      └──────── count/order/body/hash/span 전체 비교 ◄───────┘
                               │
                     모두 동일하면 canonical span
                     하나라도 다르면 fail closed
```

V1의 알려진 기본 child 정책은 500자 크기, 50자 overlap,
`["\n\n", "\n", ". ", " ", ""]` separator 순서입니다. replay splitter에는
`add_start_index=True`를 켭니다. LangChain은 이전 chunk의 시작·길이·overlap으로 다음
검색 시작점을 좁힌 뒤 그 이후의 일치 항목을 선택합니다. 현재 사례에서는 앞뒤 chunk의
경계 때문에 마지막 후보인 1016이 canonical 위치로 선택될 가능성이 높습니다. 이 값은
실제 failure fixture 회귀 테스트에서 확정해야 합니다.

#### 6.12.3 반드시 모두 통과해야 하는 승인 조건

replay가 다음 조건을 모두 증명할 때만 모호한 span을 허용합니다.

1. Prefix fidelity: legacy embedding text가 기대한 prefix로 시작하고, 제거 후 다시 붙이면
   원문과 byte-for-byte 동일해야 합니다.
2. Full sequence equality: replay child 수, 순서, 본문이 parent의 모든 legacy child와
   완전히 같아야 합니다. 문제 child 하나만 비교해서는 안 됩니다.
3. Hash fidelity: 각 replay body에 prefix를 붙인 SHA-256이 legacy embedding text
   SHA-256과 같아야 합니다.
4. Span fidelity: 모든 `parent[span_start:span_end]`가 해당 child body와 같고, 시작 위치가
   strict increasing이어야 합니다.
5. Cardinality parity: child 수, docstore mapping 수, FAISS `ntotal`이 계속 같아야 합니다.
6. Vector parity: 기존 ordinal의 vector를 새 physical ID로 옮긴 뒤 값이 기존 vector와
   동일해야 합니다.
7. Policy binding: chunk size, overlap, separator, prefix, resolver version을 reconstruction
   evidence와 embedding profile에 묶어야 합니다.
8. Determinism: 같은 복사본을 두 번 변환했을 때 span, `chunk_uid`, reconstruction digest,
   snapshot membership이 같아야 합니다.

이 조건은 임의 위치 선택과 exact replay를 구분하는 안전 경계입니다. 특히 전체 child
sequence equality가 중요합니다. 현재 library가 우연히 문제의 `.`만 만들 수 있다는
사실로는 충분하지 않으며, parent에서 생성된 모든 child가 V1 저장 결과와 일치해야 합니다.

#### 6.12.4 library version을 모를 때의 처리

V1에는 splitter package version이 남아 있지 않을 수 있습니다. version이 확인되면 같은
version을 사용해 replay하는 것이 가장 강한 증거입니다. 확인할 수 없다면 version을
추측해서 신뢰하지 않고 `unattested`로 기록해야 합니다.

다만 library version이 미확인이어도 현재 resolver가 만든 **전체 출력 sequence가 보관된
V1 sequence와 완전히 동일**하고 나머지 승인 조건을 만족한다면, 역사적 구현을 복원한
것이 아니라 V2 import를 위한 canonicalization으로 제한해 허용할 수 있습니다. 이후
resolver나 package를 업그레이드해도 이전 migration identity가 바뀌지 않도록 resolver
version과 package version을 evidence에 함께 고정해야 합니다.

#### 6.12.5 감사와 장애 분석을 위해 남길 evidence

모호성이 자동 해소되었다는 사실을 숨기면 안 됩니다. migration receipt 또는 별도
reconstruction evidence에 최소한 다음 내용을 기록합니다.

```json
{
  "resolution_method": "legacy-splitter-replay-canonical-v1",
  "resolver_version": 2,
  "legacy_parent_id": "97edba16-2df0-4dbe-9dba-12b52045f806",
  "child_order": 3,
  "candidate_count": 9,
  "selected_span_start": 1016,
  "full_sequence_replay_matched": true,
  "splitter_package": "langchain-text-splitters",
  "splitter_version": "<version-or-unattested>",
  "child_policy_sha256": "<policy-digest>",
  "embedding_text_sha256": "<legacy-hash>"
}
```

일반적인 unique proof와 canonical replay의 수, 대상 parent, 후보 수를 요약하면 릴리스
검증 담당자가 예외의 범위를 확인할 수 있습니다. 선택된 span만 남기고 모호성 정보를
버리면 나중에 같은 데이터가 다른 결과를 만든 이유를 설명하기 어렵습니다.

#### 6.12.6 구현 경계와 권장 함수 구조

기존 `resolve_ordered_spans()`의 기본 계약은 유지하고, importer가 정책을 알고 있을 때만
replay fallback을 호출하는 구조가 안전합니다.

```text
resolve_ordered_spans(...)                 # 기존 unique proof
replay_legacy_splitter_spans(...)          # 정책 기반 canonical 후보 생성
validate_replayed_legacy_sequence(...)     # 전체 sequence/hash/span 검증
resolve_legacy_spans(...)                  # 두 단계 조정 및 evidence 생성
```

generic resolver 안에 500/50이나 구두점 예외를 하드코딩하지 않습니다. splitter 정책은
embedding profile에서 전달하고, `legacy_import.py`가 legacy child metadata와 결합합니다.
이렇게 하면 resolver는 문자열·span 검증에 집중하고 importer는 V1 provenance와 fail-closed
결정을 책임집니다.

#### 6.12.7 테스트 전략

다음 테스트를 추가해야 합니다.

- 현재 사례와 같은 9개 마침표 fixture가 canonical 위치 하나를 반복해서 선택합니다.
- 전체 6개 replay child가 legacy child와 완전히 같을 때만 성공합니다.
- child 한 글자, 순서, prefix, hash, 정책 중 하나라도 바뀌면 실패합니다.
- 단순 반복 문자열이지만 splitter replay로 증명되지 않은 기존 ambiguous fixture는 계속
  실패합니다.
- overlap이 있는 정상 child의 span과 embedding hash가 기존 테스트와 동일합니다.
- 두 번의 plan 결과가 같은 reconstruction digest, `chunk_uid`, physical ID를 만듭니다.
- import 중 PDF extractor와 embedding provider가 호출되지 않습니다.
- 변환 전후 FAISS vector가 전수 또는 허용 오차 0 기준으로 동일합니다.
- replay evidence가 receipt에 포함되고 release validation이 이를 다시 검증합니다.

#### 6.12.8 단계적 적용과 중단 조건

1. 실제 parent를 비식별 fixture로 고정하고 현재 실패를 회귀 테스트로 재현합니다.
2. replay resolver를 추가하되 기존 unique 경로의 결과가 바뀌지 않는지 전체 테스트합니다.
3. 실제 복사본에 read-only dry run을 수행해 canonical replay 대상이 예상한 한 건인지
   확인합니다.
4. 변환 계획과 evidence를 생성하고 vector·membership parity를 검증합니다.
5. 격리된 canary catalog/snapshot으로 검색 parity를 확인한 뒤에만 게시합니다.
6. 게시 후 receipt와 선택된 span을 보존하고 동일 입력 재실행 결과를 비교합니다.

예상보다 많은 parent가 replay fallback을 사용하거나, 같은 입력에서 digest가 달라지거나,
전체 sequence/hash/vector parity 중 하나라도 실패하면 자동 전환을 중단해야 합니다.

#### 6.12.9 대안과 선택 기준

해당 parent만 원본 PDF에서 V2 방식으로 다시 추출·분할·임베딩하는 방법도 있습니다. 이는
운영 가능한 새 snapshot을 만들 수 있지만 V1 vector 전수 보존과 exact retrieval parity를
포기하게 됩니다. 원본 PDF와 provider를 사용할 수 있고 “완전한 V1 복제”보다 “최신 V2
검색 상태”가 중요할 때만 명시적인 별도 모드로 제공하는 것이 적절합니다.

반대로 첫 번째/마지막 occurrence 선택, 가장 가까운 위치 선택, punctuation-only 예외,
수동 DB 수정은 사용하지 않습니다. 이런 규칙은 전체 생성 과정을 증명하지 못하며 다른
반복 문자열을 조용히 잘못 연결할 수 있습니다.

#### 6.12.10 V2의 parent+span 저장 방식을 유지할 것인가

이번 문제는 V2가 child 본문을 별도 row에 중복 저장하지 않고 parent의 문자 span으로
표현하기 때문에 표면화된 것이 맞습니다. 그러나 직접 원인은 offset 방식 자체보다
**offset이 없는 V1 artifact를 offset이 필요한 V2 계약으로 변환하는 과정**에 있습니다.

V2가 실제로 저장하는 것은 offset 두 개만이 아닙니다. immutable parent 본문,
`child_order`, `span_start`, `span_end`, embedding text SHA-256, profile, snapshot membership을
함께 저장하고 child 본문은 `parent[span_start:span_end]`로 계산합니다. Native V2 build는
분할 순간 `add_start_index=True`로 span을 기록하므로 과거 위치를 역추론할 필요가 없습니다.

parent+span 방식을 유지하면 다음 장점이 있습니다.

- 텍스트 SSOT: parent와 child에 같은 문장을 두 번 저장하지 않아 어느 쪽이 정답인지
  모호해지지 않습니다.
- 불일치 방지: parent가 수정됐는데 child 복사본이 갱신되지 않는 drift를 구조적으로
  피할 수 있습니다.
- 무결성 검증: parent slice, span 범위, embedding hash를 함께 검사할 수 있습니다.
- 계층형 검색: 작은 child로 찾은 뒤 같은 parent를 바로 hydrate하기 쉽습니다.
- 재사용과 identity: immutable parent content hash와 span으로 동일 chunk를 결정적으로
  식별할 수 있습니다.

대신 parent가 반드시 immutable이어야 하고, 분할 시점부터 정확한 span을 기록해야 하며,
offset이 없는 legacy/import 데이터에는 별도 복원 정책이 필요합니다. V2는 parent UID에
content hash를 포함하고 chunk UID에 span을 포함하므로 앞의 두 조건을 이미 전제로 합니다.

child 본문을 다시 catalog의 권위 데이터로 저장하는 방식은 권장하지 않습니다. parent
본문과 child 본문이라는 두 SSOT가 생기고, insert·검증·업데이트·복구 시 두 값의 일치를
계속 보장해야 합니다. 특히 overlap이 있는 child를 모두 저장하면 텍스트 중복도 늘어납니다.

권장하는 절충안은 다음과 같습니다.

1. Runtime catalog는 현재의 parent+span 구조를 유지합니다.
2. Native V2 ingestion은 생성 순간 span과 body hash를 기록하고 즉시 slice equality를
   검증합니다.
3. Legacy migration은 splitter replay로 canonical span을 만들고, 선택 방법·후보 수·hash를
   runtime row가 아닌 migration evidence에 남깁니다.
4. V1 복사 bundle은 전환 승인과 retention 기간이 끝날 때까지 감사 원본으로 보존합니다.
5. 성능상 child 본문 materialization이 필요해지면 권위 데이터가 아닌 재생성 가능한 cache나
   generated column 성격으로만 추가합니다.

여러 종류의 외부 legacy corpus를 계속 가져와야 하고 정확한 offset을 대부분 알 수 없다면
그때는 별도의 import staging schema에 `legacy_body`와 resolution 상태를 두는 방안을 검토할
수 있습니다. 하지만 정상 검색 catalog까지 nullable span 또는 독립 child body를 허용하면
모든 reader와 검증 규칙이 두 표현을 처리해야 하므로 현재 사례의 해결책으로는 범위가
지나치게 큽니다.

따라서 최종 판단은 **V2의 parent+span 모델은 유지하되, legacy 변환 경계에서만 본문
증거와 canonicalization을 보강하는 것**입니다. 이는 V2의 SSOT 장점을 보존하면서 V1에
없던 위치 정보 때문에 전체 마이그레이션이 막히는 문제를 제한된 범위에서 해결합니다.

**최종 권고 원칙:** 과거 offset의 유일성을 복구할 수 없더라도 보관된 V1의 전체 child
sequence와 vector를 바꾸지 않는 결정적 canonical span을 엄격한 replay 검증으로 증명할
수 있으면 변환합니다. 전체 sequence·hash·span·vector 중 하나라도 증명하지 못하면
기존과 같이 fail closed합니다.

## 7. V2 catalog의 주요 데이터 모델

| 테이블 | 역할 |
| --- | --- |
| `reports` | PDF 경로, 원본 hash, 검색 metadata hash와 논리 `report_uid` |
| `embedding_profiles` | model, dimension, metric, normalization, prefix, extractor, chunk 정책 |
| `retrieval_parents` | 검색 결과 hydrate의 기준이 되는 불변 parent 본문 |
| `retrieval_chunks` | parent 내부 child 순서와 문자 span, `chunk_uid` |
| `retrieval_builds` | 전체 source manifest와 build lifecycle |
| `vector_snapshots` | FAISS 파일 경로·hash·크기·차원·metric·`ntotal` |
| `snapshot_membership` | 한 snapshot 안의 `chunk_uid ↔ faiss_id` 대응 |
| `retrieval_runtime` | active/predecessor, generation, epoch, fallback, degraded/write 상태 |
| `publication_runs` | 후보 작성부터 checkpoint 완료까지의 durable journal |

관계를 간단히 나타내면 다음과 같습니다.

```text
embedding_profiles ─┬─ retrieval_parents ── retrieval_chunks
                    └─ retrieval_builds ─── vector_snapshots
                                                │
retrieval_chunks ───── snapshot_membership ─────┘
                                                │
retrieval_runtime ───── active / predecessor ───┘
publication_runs ────── from / to snapshot
```

SQLite foreign key, `CHECK`, unique constraint, trigger가 잘못된 상태 전이를 차단합니다. 예를
들어 ready snapshot의 membership은 수정할 수 없고, `write_epoch > 0`인데 V1 fallback이
열린 상태도 허용되지 않습니다.

## 8. ID와 불변성 설계

V2의 UID는 위치에 따라 우연히 정해지는 번호가 아니라 canonical input의 SHA-256에서
계산합니다.

- `report_uid`: canonical relative path, PDF hash, retrieval metadata hash를 반영합니다.
- `parent_uid`: report, profile, parent 순서와 본문을 반영합니다.
- `chunk_uid`: parent, profile, child 순서와 span을 반영합니다.
- `faiss_id`: 해당 snapshot의 `chunk_uid`를 정렬해 1부터 부여하는 물리 ID입니다.

이 구분의 장점은 논리 동일성과 물리 배치를 분리하는 것입니다. 같은 논리 청크는 다음
snapshot에서도 같은 `chunk_uid`를 유지할 수 있지만, FAISS 내부 ID는 snapshot에만
유효합니다. 따라서 코드가 숫자 ID만 들고 다른 snapshot으로 넘어가는 실수를 revision
검사로 차단할 수 있습니다.

## 9. Build와 publication 상태 머신

일반 build는 다음 방향으로만 진행합니다.

```text
planned → cataloging → vector_building → validating → ready
        → committed_pending_checkpoint → fully_complete
        └────────────────────────────────────────────→ failed
```

FAISS snapshot은 별도의 lifecycle을 가집니다.

```text
staged → validating → ready
failed → garbage_pending → garbage_collected
ready  → garbage_pending → garbage_collected   (더 이상 서빙 계보가 아닐 때만)
```

publication은 journal 생성, catalog 작성, artifact 내구화, 파일 게시, 재검증, commit intent,
checkpoint, durable floor 기록 순으로 진행합니다. 각 단계가 디스크에 남기 때문에 재시작한
프로세스는 “대충 최신처럼 보이는 파일”을 고르지 않고 마지막으로 증명된 단계에서 복구를
결정할 수 있습니다.

`publication_generation`과 `write_epoch`은 감소하지 않습니다. fallback도 한 번 닫히면
다시 열리지 않습니다. 이 단조성(monotonicity)은 오래된 상태로의 우발적 회귀를 막는
간단하지만 강력한 규칙입니다.

## 10. V2 검색 요청의 동작

검색 요청 하나는 대략 다음 순서로 처리됩니다.

1. runtime과 active snapshot이 완전하고 일치하는지 확인합니다.
2. 날짜·종목·증권사·리포트 유형·파일명 scope를 parameterized SQL predicate로 compile합니다.
3. 같은 SQLite read transaction에서 active revision과 snapshot lease를 얻습니다.
4. eligible chunk 비율에 따라 검색 전략을 선택합니다.
   - `DIRECT`: 필터가 없거나 전체가 eligible이면 FAISS를 바로 검색합니다.
   - `SELECTOR`: eligible ID가 충분히 적으면 FAISS ID selector로 범위를 제한합니다.
   - `ADAPTIVE`: 넓은 필터는 후보 수를 점차 늘리며 필요한 결과 수를 확보합니다.
5. 결과의 physical ID가 현재 leased revision의 membership인지 검증합니다.
6. catalog에서 parent slice와 citation metadata를 hydrate합니다.
7. 요청이 끝나면 lease를 반환합니다.

이 설계는 성능을 위해 FAISS를 사용하면서도 필터, 본문, citation의 의미는 SSOT에서
가져오게 합니다.

## 11. 장애 복구와 fail-closed 원칙

V2는 무조건 자동 복구하지 않습니다. 증거가 충분한 경우에만 다음 중 하나를 선택합니다.

- Forward recovery: commit intent와 artifact가 유효하면 미완료 checkpoint를 앞으로
  완료합니다.
- Predecessor recovery: active snapshot이 손상되었고 검증된 predecessor가 있으면
  degraded read-only 상태로 이전 revision을 선택합니다.
- Exact rollback: 마이그레이션 도구가 자신이 만든 동일 신원을 증명할 때만 V1 선택
  상태로 되돌립니다.
- Fail closed: 여러 journal, hash 불일치, 소유권 충돌처럼 안전한 답을 하나로 결정할 수
  없으면 읽기·쓰기를 중단합니다.

fail closed는 가용성을 조금 희생할 수 있지만, 재무 문서 검색에서 더 위험한 “조용한
오답”과 최신 데이터 유실을 피합니다.

## 12. 운영 시 지켜야 할 불변 조건

- V2 활성 상태에서 `data/retrieval/v2`, `reports.db`, `vector_db`를 수동으로 섞거나
  삭제하지 않습니다.
- catalog가 가리키지 않는 FAISS 파일을 활성 snapshot이라고 간주하지 않습니다.
- snapshot hash·크기·dimension·metric·`ntotal`·membership 중 하나라도 다르면 읽지
  않습니다.
- 한 요청의 FAISS 결과를 다른 publication generation에서 hydrate하지 않습니다.
- active 또는 predecessor snapshot, 사용 중인 lease가 있는 snapshot을 삭제하지 않습니다.
- active embedding profile과 설정이 다르면 증분 업데이트로 억지 전환하지 않습니다.
- V1 fallback을 닫은 뒤 임의로 다시 열지 않습니다.
- 실패한 evidence나 일부 단계만 끝난 실행을 성공으로 표시하지 않습니다.

## 13. 현재 제약과 후속 과제

- 원클릭 마이그레이션은 표준 폴더 구조와 `USE_PARENT_CHILD=true`인 V1을 대상으로 합니다.
- V1 provenance가 완전하지 않아 same-space canary가 필요합니다.
- 기존 active V2 profile에는 다른 추출 정책을 자동으로 섞지 않습니다. 설정 정책이
  active profile과 다르면 incremental update는 fail closed하며, 정책 변경은 검증된
  full-corpus successor로만 수행합니다.
- 새 migration은 명시된 fallback을 `legacy-v1-import|configured=<primary>|fallback=<fallback>|unattested`
  profile에 기록합니다.
- PyMuPDF가 현재 run에서 실패하면 profile에 기록된 OpenDataLoader fallback을 즉시 한 번 시도하지만,
  추출 실패 이력과 fallback 사용 이력을 DB SSOT에 영구 기록하는 관측 기능은 후속 과제입니다.
- epoch-zero compatibility bundle은 fallback이 닫혀도 retention 승인이 끝날 때까지
  보존합니다.
- V2의 복구·publication 구조는 로컬 단일 writer를 전제로 합니다. 여러 장비가 동시에
  같은 원격 filesystem을 쓰는 분산 시스템용 합의 프로토콜은 아닙니다.

## 14. 자주 묻는 질문

### V2로 바꾸면 모든 PDF를 다시 임베딩하나요?

아닙니다. 마이그레이션은 기존 V1 vector를 옮기고 일부 canary만 현재 provider로 다시
계산합니다. 이후에도 변경된 문서만 임베딩합니다.

### SQLite가 SSOT라면 FAISS가 없어도 검색할 수 있나요?

아닙니다. catalog가 논리적 권위이지만 vector 검색에는 catalog가 검증한 active FAISS
artifact가 필요합니다. SSOT는 모든 파일을 대체한다는 뜻이 아닙니다.

### 왜 snapshot을 매번 완전한 형태로 만드나요?

활성 revision 하나만 읽어도 전체 검색 corpus를 설명할 수 있게 하기 위해서입니다.
계산은 증분으로 하지만 결과는 완전하게 만들면 여러 delta를 조합하다 생기는 누락과 순서
문제를 피할 수 있습니다.

### 왜 손상 시 자동으로 V1으로 돌아가지 않나요?

V1이 더 오래된 상태일 수 있기 때문입니다. 자동 fallback은 장애를 숨기고 최신 문서가
없는 답을 정상처럼 제공할 수 있습니다. V2는 제한된 전환 구간 외에는 명시적 실패를
선택합니다.

### snapshot과 backup은 같은 것인가요?

아닙니다. snapshot은 실제 검색에 게시할 수 있는 불변 FAISS revision이고, backup은 장애
복구나 감사 목적으로 원본을 보존한 복사본입니다.

### `predecessor`는 항상 있나요?

아닙니다. 원클릭 전환 직후에는 변환 seed 하나만 있으므로 `NULL`입니다. 이후 서로 다른
정상 successor를 게시하면 직전 healthy snapshot을 predecessor로 유지할 수 있습니다.

## 15. 구현을 더 깊게 읽는 순서

1. `src/retrieval/schema.py`: SSOT schema, 제약조건, 상태 전이
2. `src/retrieval/identity.py`: content-based UID와 physical ID 할당
3. `src/retrieval/build_service.py`: 전체 manifest, 변경 감지, vector 재사용
4. `src/retrieval/publication.py`: journal 기반 snapshot 게시
5. `src/retrieval/bootstrap.py`: V1/V2 선택과 fail-closed 검사
6. `src/retrieval/repository.py`, `reader.py`: revision lease와 검색 전략
7. `src/retrieval/recovery.py`: checkpoint와 재시작 복구
8. `scripts/migrations/v2/migrate_v2_user.py`: 원클릭 8단계 전환
9. `tests/migrations/v2`, `tests/retrieval`: 변환·복구·동시성·검색 parity 검증

문서보다 코드가 최종 동작 기준입니다. 다만 이 설계에서는 코드 역시 catalog의 불변
조건과 테스트를 우회하지 않는 것을 원칙으로 합니다.
