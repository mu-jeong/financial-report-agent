# V1 → Native V2 사용자 마이그레이션

기존 Finance LLM V1 데이터가 있는 사용자는 업데이트된 앱을 처음 실행하기 전에 프로젝트 루트의 `MIGRATE_V2.bat`을 실행합니다.

## 실행 전 확인

- 기존 Finance LLM GUI, CLI, 데이터 업데이트 창을 모두 닫습니다.
- `.venv`와 `.env`가 프로젝트 루트에 있어야 합니다.
- `DATA_ROOT`는 다음 표준 V1 구조를 사용해야 합니다.

```text
DATA_ROOT/
├─ reports.db
├─ vector_db/
│  ├─ index.faiss
│  └─ index.pkl
└─ downloaded/
   └─ *.pdf
```

## 실행

프로젝트 루트에서 `MIGRATE_V2.bat`을 더블클릭하거나 다음 명령을 실행합니다.

```bat
MIGRATE_V2.bat
```

다른 데이터 루트를 명시하려면 다음과 같이 실행합니다.

```bat
MIGRATE_V2.bat --data-root D:\path\to\data
```

## 수행되는 작업

1. V1 `reports.db`, `index.faiss`, `index.pkl`의 구조와 개수가 서로 일치하는지 확인합니다.
2. V1 parent/child 청크와 기존 FAISS 벡터를 읽습니다.
3. PDF를 다시 파싱하거나 전체 임베딩 API를 호출하지 않고 Native V2 candidate를 만듭니다.
4. 현재 Native V2 게시 절차를 통해 쓰기 가능한 snapshot을 활성화합니다.
5. 활성 snapshot과 기존 벡터값이 일치하는 것을 확인한 뒤 V1 `reports.db`와 `vector_db`를 삭제합니다.
6. `downloaded` PDF는 이후 데이터 업데이트와 전체 재구축을 위해 유지합니다.

## 실패 및 재실행

- Native V2 활성화 전에 실패하면 V1 파일을 삭제하지 않습니다.
- Native V2 활성화 후 V1 파일 삭제가 일부 실패하면 `MIGRATE_V2.bat`을 다시 실행하면 남은 V1 파일만 정리합니다.
- 이미 Native V2가 정상 활성화된 상태에서 다시 실행해도 snapshot을 다시 만들지 않습니다.
- `reports.db-wal`, `reports.db-shm` 또는 `reports.db-journal`이 남아 있으면 실행 중인 V1 프로세스가 있을 수 있으므로 마이그레이션을 중단합니다.

## 삭제되는 항목

- `DATA_ROOT/reports.db`
- `DATA_ROOT/vector_db/index.faiss`
- `DATA_ROOT/vector_db/index.pkl`
- 위 파일을 제거한 뒤 비어 있는 `DATA_ROOT/vector_db`

`DATA_ROOT/downloaded`와 `DATA_ROOT/retrieval/v2`는 삭제하지 않습니다.

## 전체 재구축과의 차이

`MIGRATE_V2.bat`은 기존 V1 벡터를 재사용하는 일회성 전환 도구입니다. `tools\recovery\REBUILD_V2.bat`은 이미 활성화된 Native V2를 현재 추출·임베딩 설정으로 다시 생성하는 복구 도구이며, 전체 PDF 처리와 임베딩 비용이 발생할 수 있습니다.
