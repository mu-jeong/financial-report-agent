# Native V2 연속 검색 업데이트 결정 기록

- 상태: 채택
- 결정일: 2026-08-02

## 결정

Native V2 데이터 업데이트의 서비스 단위를 일시적으로 `base snapshot + 작은 불변
segment chain`으로 확장합니다. 신규·변경 문서의 추출과 임베딩이 성공한 batch는
catalog transaction으로 즉시 활성화합니다. 모든 batch가 끝나면 보이는 base/segment
vector를 재사용해 완전한 immutable snapshot을 정확히 한 번 게시합니다.

이 결정은 초기 V2 계획의 “중간 segment 없이 매번 완전한 successor만 게시” 결정을
연속 업데이트 경로에 한해 대체합니다. 완전한 snapshot은 제거하지 않으며, 작업 종료의
내구성·checkpoint·rollback 단위로 유지합니다.

## 사용자 계약

- 업데이트 중에도 기존 검색을 계속 사용할 수 있습니다.
- 성공적으로 처리된 문서는 batch commit 직후 새 검색 요청에 반영됩니다.
- 변경 문서가 실패하면 새 버전은 노출하지 않고 이전 검색 가능 버전을 유지합니다.
- 삭제는 새 검색 요청부터 제외되며, 이미 열린 요청은 시작 시 고정한 revision을 끝까지
  사용합니다.
- 작업이 중단되어도 commit된 batch는 검색 가능 상태로 남습니다. 다음 native 업데이트는
  새 다운로드가 없어도 남은 chain을 완전한 snapshot으로 정리합니다.
- UI에는 segment/delta/compaction 같은 저장소 용어 대신 문서 처리, 검색 반영, 검색 데이터
  정리 상태만 표시합니다.

## 쓰기와 가시성 경계

1. 한 update job이 계획부터 최종 게시까지 native writer lock을 보유합니다.
2. Source inventory를 한 번 hash하고 active composite report와 비교합니다.
3. 각 batch는 새/변경 문서만 추출·chunk·embedding합니다.
4. FAISS segment를 off-path에 쓰고 hash/shape를 검증한 뒤, report action과 membership을
   하나의 `BEGIN IMMEDIATE` transaction에서 `ready`로 전환합니다.
5. Reader는 한 SQLite read transaction에서 base와 현재 ready chain을 고정하고 모든
   artifact lease를 함께 보유합니다. Composite request는 고정 시점에 base와 segment
   index를 검증·적재하므로, 다른 process/cache에서 시작한 요청도 이후 GC의 path 제거에
   의존하지 않습니다. SQL의 `active_reports`도 동일한 latest-head 규칙을 사용합니다.
6. 마지막 compaction은 `active_vector_membership`의 vector를 재구성해 dense ID를 다시
   부여하고 기존 full-snapshot publication coordinator로 게시합니다. Provider embedding은
   호출하지 않습니다.
7. 새 base가 활성화되면 이전 base에 속한 segment는 논리적으로 보이지 않지만 즉시
   삭제하지 않습니다. 소유 base snapshot이 기존 GC 절차에서 `garbage_collected` 경계에
   도달한 뒤에만 artifact 제거를 시도합니다. 이 정리는 snapshot GC의 하위 단계로
   실행되며 모든 snapshot 게시 후와 정상 시작 시에도 재조정합니다. 사용 중이거나 잠긴
   파일은 안전하게 남겨 다음 재조정에서 다시 시도합니다.

## 실패와 재시작

- Artifact 게시 전 실패: catalog 가시성 변화가 없습니다.
- Artifact 게시 후 catalog commit 전 실패: orphan artifact만 남을 수 있으며 같은 결정적
  segment ID로 재시도할 때 검증 후 재사용할 수 있습니다.
- `ready` commit 후 실패: 새 요청은 해당 문서를 볼 수 있고 재시작은 이미 처리한 source를
  다시 embed하지 않습니다.
- 최종 snapshot 게시 후 cleanup 실패: publication 결과는 새 base의 성공과
  `cleanup_pending`을 함께 반환합니다. 새 base는 이미 권위가 있으며 obsolete segment
  cleanup만 다음 snapshot 게시 또는 정상 시작의 GC 재조정으로 미룹니다.
- 추출 실패 action은 visibility head가 아니므로 이전 성공 버전을 가리지 않습니다. 같은
  source hash는 명시적 재시도 전까지 다시 파싱하지 않습니다.

## 제한과 후속 작업

- Segment chain은 update job 안의 일시적 서비스 계층입니다. 장기간 무제한 chain을
  운영하지 않습니다.
- Composite request는 GC와 독립적으로 완료될 수 있도록 고정 시점에 관련 index를 모두
  적재하므로, chain이 큰 동안에는 요청 시작 시 메모리·I/O 비용이 늘 수 있습니다.
- Catastrophic catalog restore의 committed floor는 여전히 최종 complete snapshot 단위입니다.
  중간 진행은 source PDF로 재생성할 수 있습니다.
- Compacted artifact는 소유 base snapshot의 GC 완료 전까지 의도적으로 보존하므로 짧은
  기간 디스크 사용량이 늘어날 수 있습니다. 상태 정보와 Monitoring 화면은 정리 대기 파일
  수·용량·최장 보존 시간을 표시하며, GC 경계 이후에도 잠긴 파일은 모든 snapshot 게시 후와
  정상 시작 시 다시 수거합니다.
- 시간/용량 기반 별도 auto-compaction과 cross-request overlay metadata cache는 현재 범위에
  포함하지 않습니다.
