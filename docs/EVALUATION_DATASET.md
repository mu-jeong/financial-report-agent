# Evaluation Dataset 선정 기준

`tests/fixtures/evaluation_dataset.json`은 Monitoring Mode에서 성능 개선 전후를 비교하기 위한 로컬 고정 기준 테스트셋입니다. PDF 본문은 포함하지 않고 질문/기대 라우팅/기대 필터/기대 source/RDB 기대 집계만 저장합니다.

이 fixture의 재현 기준은 `tests/fixtures/eval_snapshot/`에 고정된 V1형 `reports.db`와 `vector_db`입니다. `current_data` 실행은 현재 runtime backend(V1 또는 V2)에 같은 기대값을 적용하지만, 고정 baseline의 source of truth는 live `data/reports.db`가 아닙니다.

## 고정 정책

- 테스트셋은 한 번 기준선으로 정하면 변경 사유가 생기기 전까지 그대로 유지합니다.
- `scripts/build_evaluation_dataset.py`는 매번 최신 DB로 자동 갱신하기 위한 도구가 아니라, 기준선 변경이 필요할 때 재생성 근거를 남기고 사용하는 도구입니다.
- 변경 가능한 사유:
  - 기대 source PDF 또는 DB row가 더 이상 로컬 기준 데이터에서 재현되지 않음
  - Monitoring Mode에서 새로 추적할 핵심 지표 축이 추가되어 기존 케이스로는 커버할 수 없음
  - parsing/chunking/retrieval/rerank/model 평가 기준이 바뀌어 acceptance criteria 자체가 바뀜
  - fixture schema 변경이 필요함
- 단순히 live runtime에 더 최신 리포트가 추가되었다는 이유만으로 테스트셋을 바꾸지 않습니다.

## 선정 기준

1. **로컬 재현 가능성**
   - 고정 V1형 snapshot의 `reports.db`에 존재하고 `is_embedded=1`인 리포트만 VectorDB 기대 source로 사용합니다.
   - fixture에는 source 파일명과 메타데이터만 저장하고 본문은 저장하지 않습니다.

2. **라우팅 커버리지**
   - 본문 검색이 필요한 `vectordb_retrieval` 질문과 집계가 필요한 `rdb_aggregate` 질문을 모두 포함합니다.
   - router 회귀를 보기 위해 종목/산업/경제 본문 질문과 count/group-by 질문을 분리합니다.

3. **필터 커버리지**
   - 날짜 단일값, 날짜 범위, `report_type`, `target_name`, `broker`, “가장 최근” 의도를 포함합니다.
   - 짧은 영문 티커(`LS`)와 구두점 포함 종목명(`JYP Ent.`)처럼 필터 오탐이 쉬운 케이스를 포함합니다.

4. **Retrieval/Rerank 난이도**
   - 동일 종목·동일 날짜의 복수 증권사 리포트, 동일 산업 복수 리포트, 경제 리포트 묶음을 포함합니다.
   - Top-K, source coverage, rerank와 recency 변화에 민감한 케이스를 포함합니다.

5. **Monitoring Mode 지표 연관성**
   - Parsing, chunking, retrieval, rerank, generation model 변경 전후에 측정 가능한 `monitoring_dimensions` 태그를 붙입니다.
   - 현재 자동 evaluator가 직접 채점하는 값은 route, filter, source hit, hit@k, citation validity, latency, no-result입니다. Rerank-score delta, answer similarity와 provider cost는 아직 자동 평가하지 않습니다.
   - RDB 케이스에는 데이터 준비 상태, 캘린더 분포, 증권사 분포처럼 대시보드에 바로 쓰일 수 있는 집계를 포함합니다.

6. **프라이버시와 크기**
   - PDF 본문, 대화 원문, API 응답 전문은 fixture에 넣지 않습니다.
   - 안전하게 공유 가능한 메타데이터와 기대 결과만 저장합니다.

## 변경 필요 시 재생성 방법

```bash
python scripts/build_evaluation_dataset.py --db-path <V1-compatible-reports.db>
python -m pytest tests/test_evaluation_dataset.py tests/test_monitoring.py -q
```

`scripts/build_evaluation_dataset.py`는 V1-compatible `reports` schema 전용이며 활성 V2 catalog를 직접 읽는 adapter는 현재 없습니다. 기준선을 바꿀 때는 dataset JSON만 재생성하지 말고 `tests/fixtures/eval_snapshot/`의 DB/index와 `manifest.json`도 같은 source/date/count/version으로 함께 갱신해야 합니다. 재생성은 반드시 위 고정 정책의 변경 사유가 있을 때만 수행합니다.

## Multi-turn 평가 범위

`tests/fixtures/multiturn_evaluation_dataset.json`과 `run_multiturn_evaluation_dataset()` helper는 후속 질문의 scope 유지와 section deep-dive 회귀를 programmatic하게 검증합니다. 이 runner는 현재 전체 Monitoring GUI에는 노출되지 않았으며 테스트·코어 helper 범위로만 사용합니다.
