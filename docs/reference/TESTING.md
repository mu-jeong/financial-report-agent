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

기업 리포트 종합의 고정 corpus, 시간순 종합, 부분 결과, 영속 작업, UI, 기록 다시 열기, 후속 대화와 공유 표시 helper를 함께 확인하려면 다음 범위를 실행합니다.

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
