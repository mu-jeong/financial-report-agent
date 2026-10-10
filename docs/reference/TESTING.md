# 테스트 실행 구간

개발 중 기본 확인은 의도적으로 오래 걸리는 새 프로세스·benchmark·복구·스토리지 수명주기 테스트를 제외한 fast 구간을 사용합니다.

```powershell
.\.venv\Scripts\python.exe scripts\run_fast_tests.py
```

파일이나 테스트 이름을 뒤에 넘겨 범위를 더 좁힐 수 있습니다.

```powershell
.\.venv\Scripts\python.exe scripts\run_fast_tests.py tests\test_metadata_filters.py
.\.venv\Scripts\python.exe scripts\run_fast_tests.py -k report_type_only_followup
```

기업 리포트 종합의 고정 corpus, 시간순 종합, 부분 결과, 영속 작업, UI, 기록 다시 열기·삭제, 검증된 기존 PDF 열기, 후속 대화와 공유 표시 helper를 함께 확인하려면 다음 범위를 실행합니다. 단일 `종합 생성` 제출은 실제 위젯으로 RDB 결과 생성과 후속 대화까지 검증하며, 제출한 조건을 한 번 읽고 이전 확인 자료를 재사용하지 않는지 확인합니다. 입력 편집·화면 갱신 시 작업 미실행, 자료 부족·한도 초과·저장소 오류 안내와 실행 중 작업의 중복 제출 차단도 검증합니다. 기록 삭제는 소유권·실행 중 차단·선택 전환·마지막 기록 정리·캐시 유지와 watcher 재로드를 검증하며, PDF 열기는 기본 뷰어 호출을 대체해 정확한 경로·해시 검사와 실패 안내를 확인합니다. 실제 OS 뷰어 창은 자동 테스트에서 열지 않습니다.

근거 부록은 미인용 문서가 9번과 10번 사이에 끼는 사례와 대상 문서 순서가 뒤집힌 사례를 재현해 저장된 번호순 표시를 검증합니다. 번호 없는 문서가 접힌 `기타 대상 문서`에 한 번만 표시되는지 실제 위젯 구조로 확인하고, 인용 anchor·PDF 버튼·후속 질문 범위·저장된 결과가 유지되는지 검사합니다.

```powershell
.\.venv\Scripts\python.exe scripts\run_fast_tests.py tests\test_report_corpus.py tests\test_report_synthesis.py tests\test_report_synthesis_raw_contract.py tests\test_report_synthesis_store.py tests\test_synthesis_app_flow.py tests\test_synthesis_followup.py tests\test_synthesis_jobs.py tests\test_synthesis_plain_text.py tests\test_synthesis_response_contract.py tests\test_synthesis_temporal_prompt.py tests\test_synthesis_views.py tests\test_sidebar_synthesis_history.py tests\test_chat_ui_helpers.py
```

fast runner는 `slow` 제외를 항상 유지하므로 사용자 `-m` 인자를 받지 않습니다. 또한 현재 테스트에서 사용하지 않는 외부 pytest 플러그인의 자동 로드를 끕니다. coverage 플러그인처럼 명시적인 플러그인이 필요한 실행에는 fast runner를 사용하지 않습니다.

`slow` 표시는 기본 전체 테스트에서 제외한다는 뜻이 아닙니다. 전체 테스트에는 fast와 slow가 모두 포함됩니다.

```powershell
# 느린 통합·benchmark 구간만 실행
.\.venv\Scripts\python.exe -m pytest -q -m slow

# 배포 전 전체 회귀 테스트 실행
.\.venv\Scripts\python.exe -m pytest -q
```

새 테스트에는 실행 방식 자체가 계약인 경우에만 `slow`를 붙입니다. 예시는 독립 Python 프로세스 기동, 실제 GUI entrypoint smoke, 다중 snapshot/GC 복구, 반복 benchmark입니다. 단순히 일시적으로 느린 단위 테스트를 숨기기 위해 사용하지 않습니다.
