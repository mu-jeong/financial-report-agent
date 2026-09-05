# Native V2 연속 검색 업데이트 결정 기록

- 상태: 채택
- 결정일: 2026-08-02

## 결정

Native V2 데이터 업데이트의 서비스 단위를 일시적으로 `base snapshot + 작은 불변 segment chain`으로 확장한다. 신규·변경 문서의 추출·임베딩이 성공한 batch는 catalog transaction으로 즉시 활성화하고, 모든 batch가 끝나면 보이는 base/segment vector를 재사용해 완전한 immutable snapshot을 정확히 한 번 게시한다.

초기 V2 계획의 "중간 segment 없이 매번 완전한 successor만 게시" 결정을 연속 업데이트 경로에 한해 대체한 것이다. 완전한 snapshot은 제거하지 않고 작업 종료의 내구성·checkpoint·rollback 단위로 유지한다.

## 사용자 계약

- 업데이트 중에도 기존 검색을 계속 사용할 수 있고, 성공한 문서는 batch commit 직후 새 요청에 반영된다.
- 변경 문서가 실패하면 새 버전을 노출하지 않고 이전 검색 가능 버전을 유지한다.
- 삭제는 새 요청부터 제외되며, 이미 열린 요청은 시작 시 고정한 revision을 끝까지 쓴다.
- 작업이 중단돼도 commit된 batch는 검색 가능 상태로 남고, 다음 native 업데이트가 남은 chain을 완전한 snapshot으로 정리한다.
- UI에는 storage 용어 대신 문서 처리·검색 반영·정리 상태만 표시한다.

## 쓰기와 가시성 경계

1. 한 update job이 계획부터 최종 게시까지 native writer lock을 보유한다.
2. Source inventory를 한 번 hash하고 active composite report와 비교한다.
3. 각 batch는 새·변경 문서만 추출·chunk·embedding해 off-path에 쓰고 hash/shape를 검증한 뒤, report action·membership을 하나의 `BEGIN IMMEDIATE` transaction에서 `ready`로 전환한다.
4. Reader는 한 read transaction에서 base와 현재 ready chain을 고정하고 artifact lease를 함께 보유하므로, 이후 GC의 path 제거에 의존하지 않는다.
5. 마지막 compaction은 기존 vector를 재구성해 dense ID를 다시 부여하고 full-snapshot publication coordinator로 게시한다. Provider embedding은 호출하지 않는다.
6. 새 base가 활성화되면 이전 base의 segment는 논리적으로 보이지 않지만 즉시 삭제하지 않고, 소유 base snapshot이 GC 경계에 도달한 뒤에만 정리한다. 사용 중이거나 잠긴 파일은 다음 재조정에서 다시 시도한다.

## 실패와 재시작

| 시점 | 결과 |
| --- | --- |
| Artifact 게시 전 실패 | catalog 가시성 변화 없음 |
| 게시 후 catalog commit 전 실패 | orphan artifact만 남고 같은 결정적 segment ID로 재시도 시 검증 후 재사용 |
| `ready` commit 후 실패 | 새 요청은 해당 문서를 보고, 재시작은 이미 처리한 source를 다시 embed하지 않음 |
| 최종 게시 후 cleanup 실패 | 새 base가 이미 권위이며 cleanup만 다음 게시·시작 시 GC 재조정으로 미룸 |
| 추출 실패 | visibility head가 아니므로 이전 성공 버전을 가리지 않고, 같은 hash는 명시적 재시도 전까지 재파싱하지 않음 |

## 제한과 후속 작업

- Segment chain은 update job 안의 일시적 서비스 계층이며 장기간 무제한 chain을 운영하지 않는다.
- Composite request는 GC와 독립적으로 완료되도록 고정 시점에 index를 모두 적재하므로, chain이 큰 동안 요청 시작 시 메모리·I/O 비용이 늘 수 있다.
- Catastrophic catalog restore의 committed floor는 최종 complete snapshot 단위이며 중간 진행은 source PDF로 재생성할 수 있다.
- Compacted artifact는 소유 base GC 완료 전까지 의도적으로 보존하므로 짧게 디스크 사용량이 늘 수 있고, 상태·Monitoring 화면은 정리 대기 수·용량·최장 보존 시간을 표시한다.
- 시간·용량 기반 auto-compaction과 cross-request overlay cache는 현재 범위에 포함하지 않는다.