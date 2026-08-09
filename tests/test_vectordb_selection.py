from langchain_core.documents import Document

from src.nodes.vectordb import (
    build_temporal_preflight_plan,
    ensure_document_coverage,
    required_file_names_from_prior_scope,
    select_top_passages,
)


def test_requested_candidate_count_uses_one_configurable_multiplier(monkeypatch):
    import src.nodes.vectordb as vectordb

    monkeypatch.setattr(vectordb, "SEARCH_TOP_K", 40)
    monkeypatch.setattr(vectordb, "SEARCH_CANDIDATE_MULTIPLIER", 1, raising=False)

    assert vectordb._requested_candidate_count() == 40

    monkeypatch.setattr(vectordb, "SEARCH_CANDIDATE_MULTIPLIER", 3)

    assert vectordb._requested_candidate_count() == 120


def test_report_universe_query_uses_in_clause_for_multiple_report_types():
    import src.nodes.vectordb as vectordb

    query, params = vectordb._report_universe_query(
        {
            "report_types": ["company", "industry", "economy"],
            "report_date_start": "2026-07-27",
            "report_date_end": "2026-08-02",
        }
    )

    assert "report_type IN (?, ?, ?)" in query
    assert params == [
        "company",
        "industry",
        "economy",
        "2026-07-27",
        "2026-08-02",
    ]


def test_file_name_filter_accepts_multiple_report_type_prefixes():
    import src.nodes.vectordb as vectordb

    filters = {"report_types": ["company", "industry"]}

    assert vectordb._file_name_matches_filters("company_2026-08-01_A.pdf", filters)
    assert vectordb._file_name_matches_filters("industry_2026-08-01_B.pdf", filters)
    assert not vectordb._file_name_matches_filters("economy_2026-08-01_C.pdf", filters)


def test_ensure_document_coverage_keeps_small_filtered_document_set():
    docs_with_scores = [
        (
            Document(
                page_content="한화 chunk 1",
                metadata={"file_name": "hanwha.pdf", "broker": "한화투자증권"},
            ),
            0.1,
        ),
        (
            Document(
                page_content="한화 chunk 2",
                metadata={"file_name": "hanwha.pdf", "broker": "한화투자증권"},
            ),
            0.2,
        ),
        (
            Document(
                page_content="유안타 chunk 1",
                metadata={"file_name": "yuanta.pdf", "broker": "유안타증권"},
            ),
            0.3,
        ),
    ]
    selected = [
        {
            "text": "한화 chunk 1",
            "score": 0.1,
            "meta": {"file_name": "hanwha.pdf", "broker": "한화투자증권"},
        },
        {
            "text": "한화 chunk 2",
            "score": 0.2,
            "meta": {"file_name": "hanwha.pdf", "broker": "한화투자증권"},
        },
    ]

    covered = ensure_document_coverage(selected, docs_with_scores, max_passages=2)

    assert {item["meta"]["file_name"] for item in covered} == {"hanwha.pdf", "yuanta.pdf"}


def test_ensure_document_coverage_honors_explicit_required_file_scope():
    docs_with_scores = [
        (
            Document(page_content="hanwha recent", metadata={"file_name": "hanwha.pdf"}),
            0.1,
        ),
        (
            Document(page_content="hanwha duplicate", metadata={"file_name": "hanwha.pdf"}),
            0.2,
        ),
        (
            Document(page_content="yuanta selected by prior list", metadata={"file_name": "yuanta.pdf"}),
            0.9,
        ),
        (
            Document(page_content="unrelated", metadata={"file_name": "other.pdf"}),
            0.05,
        ),
    ]
    selected = [
        {"text": "hanwha recent", "score": 0.1, "meta": {"file_name": "hanwha.pdf"}},
        {"text": "hanwha duplicate", "score": 0.2, "meta": {"file_name": "hanwha.pdf"}},
    ]

    covered = ensure_document_coverage(
        selected,
        docs_with_scores,
        max_passages=2,
        required_file_names=["hanwha.pdf", "yuanta.pdf"],
    )

    assert {item["meta"]["file_name"] for item in covered} == {"hanwha.pdf", "yuanta.pdf"}


def test_select_top_passages_skips_document_coverage_for_single_target_deep_dive(monkeypatch):
    import src.nodes.vectordb as vectordb

    monkeypatch.setattr(vectordb, "SEARCH_TOP_K", 2)
    monkeypatch.setattr(vectordb, "USE_RERANKER", False)
    monkeypatch.setattr(vectordb, "RECENCY_WEIGHT", 0)
    docs_with_scores = [
        (
            Document(page_content="삼성전자 HBM 전망 1", metadata={"file_name": "samsung_a.pdf", "target_name": "삼성전자"}),
            0.1,
        ),
        (
            Document(page_content="삼성전자 HBM 전망 2", metadata={"file_name": "samsung_a.pdf", "target_name": "삼성전자"}),
            0.2,
        ),
        (
            Document(page_content="다른 회사 HBM 전망", metadata={"file_name": "other.pdf", "target_name": "다른회사"}),
            0.3,
        ),
    ]

    selected, metrics = select_top_passages(
        "삼성전자 HBM 전망 자세히 알려줘",
        docs_with_scores,
        search_filters={"target_name": "삼성전자"},
    )

    assert [item["meta"]["file_name"] for item in selected] == ["samsung_a.pdf", "samsung_a.pdf"]
    assert metrics == {
        "document_coverage_applied": False,
        "document_coverage_reason": "single_target_deep_dive",
    }


def test_select_top_passages_applies_document_coverage_for_multi_document_intent(monkeypatch):
    import src.nodes.vectordb as vectordb

    monkeypatch.setattr(vectordb, "SEARCH_TOP_K", 2)
    monkeypatch.setattr(vectordb, "USE_RERANKER", False)
    monkeypatch.setattr(vectordb, "RECENCY_WEIGHT", 0)
    docs_with_scores = [
        (
            Document(page_content="삼성전자 리포트 A chunk 1", metadata={"file_name": "samsung_a.pdf", "target_name": "삼성전자"}),
            0.1,
        ),
        (
            Document(page_content="삼성전자 리포트 A chunk 2", metadata={"file_name": "samsung_a.pdf", "target_name": "삼성전자"}),
            0.2,
        ),
        (
            Document(page_content="삼성전자 리포트 B", metadata={"file_name": "samsung_b.pdf", "target_name": "삼성전자"}),
            0.3,
        ),
    ]

    selected, metrics = select_top_passages(
        "이번 주 삼성전자 리포트들 각각 주요 내용 정리해줘",
        docs_with_scores,
        search_filters={"target_name": "삼성전자"},
    )

    assert {item["meta"]["file_name"] for item in selected} == {"samsung_a.pdf", "samsung_b.pdf"}
    assert metrics == {
        "document_coverage_applied": True,
        "document_coverage_reason": "multi_document_intent",
    }


def test_select_top_passages_applies_document_coverage_for_date_bounded_target_summary(monkeypatch):
    import src.nodes.vectordb as vectordb

    monkeypatch.setattr(vectordb, "SEARCH_TOP_K", 3)
    monkeypatch.setattr(vectordb, "USE_RERANKER", False)
    monkeypatch.setattr(vectordb, "RECENCY_WEIGHT", 0)
    docs_with_scores = [
        (
            Document(
                page_content="SK하이닉스 6월 22일 chunk 1",
                metadata={
                    "file_name": "company_2026-06-22_SK하이닉스_iM증권_2Q26 영업이익 전망.pdf",
                    "target_name": "SK하이닉스",
                    "report_date": "2026-06-22",
                    "report_type": "company",
                },
            ),
            0.1,
        ),
        (
            Document(
                page_content="SK하이닉스 6월 22일 chunk 2",
                metadata={
                    "file_name": "company_2026-06-22_SK하이닉스_iM증권_2Q26 영업이익 전망.pdf",
                    "target_name": "SK하이닉스",
                    "report_date": "2026-06-22",
                    "report_type": "company",
                },
            ),
            0.2,
        ),
        (
            Document(
                page_content="SK하이닉스 6월 25일 ADR",
                metadata={
                    "file_name": "company_2026-06-25_SK하이닉스_IBK투자증권_ADR 발행.pdf",
                    "target_name": "SK하이닉스",
                    "report_date": "2026-06-25",
                    "report_type": "company",
                },
            ),
            0.3,
        ),
    ]

    selected, metrics = select_top_passages(
        "해당 기간 내에 발간된 sk하이닉스에 대한 리포트 정리해서 내용을 알려줘",
        docs_with_scores,
        search_filters={
            "report_date_start": "2026-06-22",
            "report_date_end": "2026-06-25",
            "target_name": "SK하이닉스",
            "report_type": "company",
        },
    )

    assert {item["meta"]["file_name"] for item in selected} == {
        "company_2026-06-22_SK하이닉스_iM증권_2Q26 영업이익 전망.pdf",
        "company_2026-06-25_SK하이닉스_IBK투자증권_ADR 발행.pdf",
    }
    assert metrics == {
        "document_coverage_applied": True,
        "document_coverage_reason": "date_bounded_target_report_set",
    }


def test_required_file_names_from_prior_scope_keeps_matching_target_period_files_only():
    prior_scope = {
        "file_names": [
            "company_2026-06-22_SK하이닉스_iM증권_2Q26 영업이익 전망.pdf",
            "company_2026-06-22_SK하이닉스_한화투자증권_PE 10배.pdf",
            "company_2026-06-25_SK하이닉스_IBK투자증권_ADR 발행.pdf",
            "company_2026-06-25_삼성전자_미래에셋증권_생산능력.pdf",
            "industry_2026-06-22_반도체_iM증권_산업 전망.pdf",
        ]
    }

    required = required_file_names_from_prior_scope(
        "해당 기간 내에 발간된 sk하이닉스에 대한 리포트 정리해서 내용을 알려줘",
        prior_scope,
        {
            "report_date_start": "2026-06-22",
            "report_date_end": "2026-06-25",
            "target_name": "SK하이닉스",
            "report_type": "company",
        },
    )

    assert required == [
        "company_2026-06-22_SK하이닉스_iM증권_2Q26 영업이익 전망.pdf",
        "company_2026-06-22_SK하이닉스_한화투자증권_PE 10배.pdf",
        "company_2026-06-25_SK하이닉스_IBK투자증권_ADR 발행.pdf",
    ]


def test_required_file_names_from_prior_scope_keeps_target_files_for_target_with_prior_dates():
    prior_scope = {
        "file_names": [
            "company_2026-06-22_SK하이닉스_iM증권_2Q26 영업이익 전망.pdf",
            "company_2026-06-22_SK하이닉스_한화투자증권_PE 10배.pdf",
            "company_2026-06-25_삼성전자_미래에셋증권_생산능력.pdf",
        ]
    }

    required = required_file_names_from_prior_scope(
        "SK하이닉스 알려줘",
        prior_scope,
        {
            "report_date_start": "2026-06-22",
            "report_date_end": "2026-06-26",
            "target_name": "SK하이닉스",
            "report_type": "company",
        },
    )

    assert required == [
        "company_2026-06-22_SK하이닉스_iM증권_2Q26 영업이익 전망.pdf",
        "company_2026-06-22_SK하이닉스_한화투자증권_PE 10배.pdf",
    ]


def test_select_top_passages_applies_document_coverage_for_section_followup(monkeypatch):
    import src.nodes.vectordb as vectordb

    monkeypatch.setattr(vectordb, "SEARCH_TOP_K", 3)
    monkeypatch.setattr(vectordb, "USE_RERANKER", False)
    monkeypatch.setattr(vectordb, "RECENCY_WEIGHT", 0)
    docs_with_scores = [
        (
            Document(
                page_content="넥스트바이오메디컬 논문 상세 chunk 1",
                metadata={"file_name": "nextbio.pdf", "target_name": "넥스트바이오메디컬", "report_type": "company"},
            ),
            0.1,
        ),
        (
            Document(
                page_content="넥스트바이오메디컬 논문 상세 chunk 2",
                metadata={"file_name": "nextbio.pdf", "target_name": "넥스트바이오메디컬", "report_type": "company"},
            ),
            0.2,
        ),
        (
            Document(
                page_content="넥스트바이오메디컬 논문 상세 chunk 3",
                metadata={"file_name": "nextbio.pdf", "target_name": "넥스트바이오메디컬", "report_type": "company"},
            ),
            0.3,
        ),
        (
            Document(
                page_content="삼성E&A 수주 모멘텀",
                metadata={"file_name": "samsung_ea.pdf", "target_name": "삼성E&A", "report_type": "company"},
            ),
            0.9,
        ),
        (
            Document(
                page_content="리가켐바이오 ADC 이벤트",
                metadata={"file_name": "ligachem.pdf", "target_name": "리가켐바이오", "report_type": "company"},
            ),
            1.0,
        ),
    ]

    selected, metrics = select_top_passages(
        "개별 종목에 대해 좀 더 자세히 작성해줘",
        docs_with_scores,
        search_filters={
            "report_date_start": "2026-06-15",
            "report_date_end": "2026-06-21",
            "report_type": "company",
        },
        scope_decision={"reason": "matched_prior_section_alias", "matched_section_id": "company"},
    )

    assert {item["meta"]["file_name"] for item in selected} == {
        "nextbio.pdf",
        "samsung_ea.pdf",
        "ligachem.pdf",
    }
    assert metrics == {
        "document_coverage_applied": True,
        "document_coverage_reason": "section_followup_scope",
    }


def test_build_temporal_preflight_plan_uses_all_files_when_within_top_k():
    rows = [
        {"report_date": "2026-01-10", "file_name": "samsung-jan.pdf", "is_embedded": 1},
        {"report_date": "2026-02-20", "file_name": "samsung-feb.pdf", "is_embedded": 1},
    ]

    plan = build_temporal_preflight_plan(rows, max_files=3)

    assert plan["file_names"] == ["samsung-jan.pdf", "samsung-feb.pdf"]
    assert plan["metrics"]["preflight_file_count"] == 2
    assert plan["metrics"]["selected_file_count"] == 2
    assert plan["metrics"]["selection_reason"] == "all_files_within_search_top_k"


def test_build_temporal_preflight_plan_selects_month_representatives_when_over_top_k():
    rows = [
        {"report_date": "2026-01-10", "file_name": "samsung-jan-a.pdf", "is_embedded": 1},
        {"report_date": "2026-01-20", "file_name": "samsung-jan-b.pdf", "is_embedded": 1},
        {"report_date": "2026-02-15", "file_name": "samsung-feb.pdf", "is_embedded": 1},
        {"report_date": "2026-03-15", "file_name": "samsung-mar.pdf", "is_embedded": 1},
    ]

    plan = build_temporal_preflight_plan(rows, max_files=3)

    assert plan["file_names"] == ["samsung-jan-a.pdf", "samsung-feb.pdf", "samsung-mar.pdf"]
    assert plan["metrics"]["preflight_file_count"] == 4
    assert plan["metrics"]["selected_file_count"] == 3
    assert plan["metrics"]["bucket_by"] == "month"
    assert plan["metrics"]["bucket_count"] == 3
    assert plan["metrics"]["selection_reason"] == "month_bucket_representatives"


def test_native_dispatch_embeds_once_and_uses_one_scoped_reader_request(
    monkeypatch,
):
    from types import SimpleNamespace

    import src.nodes.vectordb as vectordb

    calls = {"embed": 0, "search": 0, "reader": 0}

    class FakeEmbeddings:
        def embed_query(self, query):
            calls["embed"] += 1
            assert query == "native query"
            return [0.25, 0.75]

    monkeypatch.setattr(vectordb, "build_embeddings_fn", FakeEmbeddings)

    repository = object()
    scope = {
        "file_names": ["prior.pdf"],
        "report_date_start": "2026-07-01",
        "report_date_end": "2026-07-31",
    }
    chunk = SimpleNamespace(
        metadata={
            "parent_uid": "parent-1",
            "chunk_uid": "chunk-1",
            "file_name": "prior.pdf",
            "target_name": "Acme",
            "report_date": "2026-07-15",
            "title": "Outlook",
            "broker": "Broker",
            "report_type": "company",
        },
        child_order=2,
        span_start=5,
        span_end=20,
        physical_id=7,
        snapshot_id="snapshot-1",
        publication_generation=4,
        parent_slice="canonical parent slice",
        score=0.125,
    )
    response = SimpleNamespace(
        results=(chunk,),
        faiss_fetch_k=8,
        candidate_count=1,
        eligible_count=1,
        snapshot_total=10,
        strategy=SimpleNamespace(value="selector"),
        faiss_calls=1,
        hydration_batches=1,
        hydration_rows=1,
        hydration_cache_hits=1,
        hydration_cache_misses=0,
        revision=SimpleNamespace(snapshot_id="snapshot-1", publication_generation=4),
    )

    class FakeReader:
        def __init__(self, received_repository):
            calls["reader"] += 1
            assert received_repository is repository

        def search(self, vector, k, *, scope):
            calls["search"] += 1
            assert vector.tolist() == [0.25, 0.75]
            assert k >= vectordb.SEARCH_TOP_K
            assert scope == {
                "file_names": ["prior.pdf"],
                "report_date_start": "2026-07-01",
                "report_date_end": "2026-07-31",
            }
            return response

    reader = FakeReader(repository)
    monkeypatch.setattr(
        vectordb,
        "resolve_retrieval_dispatch",
        lambda _path: SimpleNamespace(
            mode="native",
            native=SimpleNamespace(reader=reader),
            selection=None,
        ),
    )
    docs_with_scores, metrics = vectordb._retrieve_docs_with_scores(
        "native query",
        scope,
    )
    second_docs_with_scores, second_metrics = vectordb._retrieve_docs_with_scores(
        "native query",
        scope,
    )

    assert calls == {"embed": 2, "search": 2, "reader": 1}
    assert docs_with_scores[0][0].page_content == "canonical parent slice"
    assert docs_with_scores[0][0].metadata["parent_uid"] == "parent-1"
    assert docs_with_scores[0][0].metadata["child_index"] == 2
    assert docs_with_scores[0][1] == 0.125
    assert metrics["runtime_mode"] == "native"
    assert metrics["snapshot_id"] == "snapshot-1"
    assert metrics["publication_generation"] == 4
    assert metrics["native_hydration_rows"] == 1
    assert metrics["native_hydration_cache_hits"] == 1
    assert metrics["native_hydration_cache_misses"] == 0
    assert second_docs_with_scores == docs_with_scores
    assert second_metrics == metrics


def test_native_repository_failure_returns_no_results(
    monkeypatch,
):
    from types import SimpleNamespace

    import src.nodes.vectordb as vectordb

    monkeypatch.setattr(
        vectordb,
        "build_embeddings_fn",
        lambda: SimpleNamespace(embed_query=lambda query: [0.0, 1.0]),
    )

    class FailingReader:
        def search(self, *args, **kwargs):
            raise vectordb.RepositoryError("catalog lease failed")

    monkeypatch.setattr(
        vectordb,
        "resolve_retrieval_dispatch",
        lambda _path: SimpleNamespace(
            mode="native",
            native=SimpleNamespace(reader=FailingReader()),
            selection=None,
        ),
    )
    result = vectordb.vectordb_node(
        {"question": "query", "search_filters": {"report_type": "company"}}
    )

    assert result["no_vector_results"] is True
    assert result["monitoring_metrics"]["retrieval"]["runtime_mode"] == "unavailable"
    assert result["monitoring_metrics"]["retrieval"]["error_code"] == "RetrievalDispatchError"


def test_bootstrap_failure_returns_no_results_before_embedding(monkeypatch):
    import src.nodes.vectordb as vectordb

    def fail_bootstrap(path):
        raise vectordb.RetrievalBootstrapError("native catalog is invalid")

    monkeypatch.setattr(vectordb, "resolve_retrieval_dispatch", fail_bootstrap)
    monkeypatch.setattr(
        vectordb,
        "build_embeddings_fn",
        lambda: (_ for _ in ()).throw(
            AssertionError("bootstrap must complete before embedding")
        ),
    )
    result = vectordb.vectordb_node(
        {"question": "query", "search_filters": {"report_type": "company"}}
    )

    assert result["no_vector_results"] is True
    assert result["monitoring_metrics"]["retrieval"]["error_code"] == "RetrievalBootstrapError"


def test_native_parent_slice_deduplicates_by_parent_uid():
    import src.nodes.vectordb as vectordb

    docs_with_scores = [
        (
            Document(
                page_content="canonical parent slice",
                metadata={"parent_uid": "parent-1", "file_name": "native.pdf"},
            ),
            0.1,
        ),
        (
            Document(
                page_content="same parent from another child",
                metadata={"parent_uid": "parent-1", "file_name": "native.pdf"},
            ),
            0.2,
        ),
    ]

    passages = vectordb._build_passages(docs_with_scores)

    assert len(passages) == 1
    assert passages[0]["text"] == "canonical parent slice"


def test_vectordb_node_records_prompt_chunk_and_document_identifiers(monkeypatch):
    from langchain_core.messages import AIMessage

    import src.nodes.vectordb as vectordb

    metadata = {
        "report_uid": "report-1",
        "chunk_uid": "chunk-1",
        "parent_uid": "parent-1",
        "profile_id": "profile-1",
        "child_index": 3,
        "span_start": 20,
        "span_end": 120,
        "physical_id": 9,
        "snapshot_id": "snapshot-1",
        "publication_generation": 4,
        "file_name": "company.pdf",
        "target_name": "Acme",
        "report_date": "2026-07-15",
        "title": "Outlook",
        "broker": "Broker",
        "report_type": "company",
    }
    document = Document(page_content="prompt context", metadata=metadata)
    monkeypatch.setattr(
        vectordb,
        "_retrieve_docs_with_scores",
        lambda *_args, **_kwargs: (
            [(document, 0.25)],
            {
                "runtime_mode": "native",
                "requested_k": 160,
                "fetch_k": 8,
            },
        ),
    )
    monkeypatch.setattr(
        vectordb,
        "filter_docs_with_scores",
        lambda docs, _filters: docs,
    )
    monkeypatch.setattr(
        vectordb,
        "select_top_passages",
        lambda *_args, **_kwargs: (
            [
                {
                    "text": "prompt context",
                    "score": 0.25,
                    "meta": metadata,
                }
            ],
            {
                "document_coverage_applied": False,
                "document_coverage_reason": "single_target_default",
            },
        ),
    )

    class FakeChatModel:
        def bind_tools(self, _tools):
            return self

        def invoke(self, _messages):
            return AIMessage(content="answer [1]")

    monkeypatch.setattr(vectordb, "build_chat_model", lambda **_kwargs: FakeChatModel())

    result = vectordb.vectordb_node(
        {
            "question": "Acme outlook",
            "rewritten_query": "Acme outlook",
            "search_filters": {"target_name": "Acme"},
        }
    )

    source = result["rerank_info"][0]
    assert source["report_uid"] == "report-1"
    assert source["chunk_uid"] == "chunk-1"
    assert source["parent_uid"] == "parent-1"
    assert source["child_index"] == 3
    assert source["span_start"] == 20
    assert source["span_end"] == 120
    assert source["physical_id"] == 9
    assert source["snapshot_id"] == "snapshot-1"
    assert source["publication_generation"] == 4
    assert "text" not in source
    assert "page_content" not in source


def test_vectordb_passes_matching_prior_files_into_single_retrieval_scope(monkeypatch):
    import src.nodes.vectordb as vectordb

    captured_scopes = []

    def fake_retrieve(query, scope):
        captured_scopes.append(scope)
        return [], {
            "runtime_mode": "native",
            "fetch_k": 0,
            "requested_k": 8,
        }

    monkeypatch.setattr(vectordb, "_retrieve_docs_with_scores", fake_retrieve)
    file_name = "company_2026-07-15_Acme_Broker_Outlook.pdf"

    result = vectordb.vectordb_node(
        {
            "question": "Acme outlook",
            "search_filters": {
                "target_name": "Acme",
                "report_type": "company",
                "report_date_start": "2026-07-01",
                "report_date_end": "2026-07-31",
            },
            "prior_search_scope": {"file_names": [file_name]},
        }
    )

    assert result["no_vector_results"] is True
    assert captured_scopes == [
        {
            "target_name": "Acme",
            "report_type": "company",
            "report_date_start": "2026-07-01",
            "report_date_end": "2026-07-31",
            "file_names": [file_name],
        }
    ]
