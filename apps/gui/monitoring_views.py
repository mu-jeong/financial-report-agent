"""Monitoring views for the Streamlit GUI."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from apps.gui import data_views
from apps.gui import monitoring_jobs
from apps.gui import search_engine
from apps.gui import status_cache
from src.configs import config as config_module
from src.core import compare_pdf_extractors
from src.core import conversation_store
from src.core import monitoring
from src.core import status as status_module


MONITORING_EVAL_RUN_DIR = Path("debug") / "evaluation_runs"

_EVALUATION_JOB_KEY = "native-v2-evaluation"

_PROBLEM_AREA_LABELS = {
    "summary": "현재 문제",
    "response": "응답 원인 확인",
    "search_data": "검색 자료 준비",
    "evaluation": "정확도 평가",
    "parsing": "문서 읽기 품질 비교",
}

_MONITORING_AREA_GROUPS = {
    "operations": ("summary", "response", "search_data"),
    "experiments": ("evaluation", "parsing"),
}

_MONITORING_GROUP_LABELS = {
    "operations": "운영 모니터링",
    "experiments": "성능 개선 실험",
}

_V2_CHECK_LABELS = {
    "v2_runtime": "현재 검색 자료 준비",
    "native_snapshot": "현재 검색 자료 준비",
    "native_membership": "검색 대상 일치",
    "manifest_backlog": "아직 검색에 반영되지 않은 문서",
    "pdf_vs_manifest": "원문과 검색 자료 일치",
    "search_coverage": "검색 자료 반영률",
    "runtime_health": "검색 서비스 상태",
    "cleanup_backlog": "정리 대기 파일",
}

_V2_CHECK_ACTIONS = {
    "v2_runtime": "Native V2 검색 데이터와 실행 설정을 확인하세요.",
    "native_snapshot": "검색 자료 준비에서 현재 빌드와 스냅샷 상태를 확인하세요.",
    "native_membership": "카탈로그와 벡터 인덱스의 검색 대상 수를 확인하세요.",
    "manifest_backlog": "검색 자료 준비에서 미반영 문서를 확인한 뒤 업데이트하세요.",
    "pdf_vs_manifest": "원문 PDF 동기화 상태를 확인하세요.",
    "search_coverage": "검색 자료 준비에서 누락 보고서를 확인하세요.",
    "runtime_health": "검색 런타임의 세대와 쓰기 가능 상태를 확인하세요.",
    "cleanup_backlog": "데이터 업데이트 완료 여부와 정리 대기 파일을 확인하세요.",
}


def _restore_monitoring_area_selection(
    widget_key: str,
    storage_key: str,
    options: tuple[str, ...],
) -> None:
    """Restore a group selection after Streamlit cleaned up its hidden widget."""

    selected = st.session_state.get(storage_key, options[0])
    if selected not in options:
        selected = options[0]
    st.session_state[storage_key] = selected
    st.session_state[widget_key] = selected


def _store_monitoring_area_selection(widget_key: str, storage_key: str) -> None:
    """Copy the visible widget value into a permanent session-state key."""

    selected = st.session_state.get(widget_key)
    if selected is not None:
        st.session_state[storage_key] = selected


def _parse_monitoring_paths(raw_paths: str) -> list[str]:
    """Parse comma/newline-separated paths from the Monitoring UI."""
    paths: list[str] = []
    for part in raw_paths.replace(",", "\n").splitlines():
        cleaned = part.strip().strip('"')
        if cleaned:
            paths.append(cleaned)
    return paths


def _engine_summary_rows(summary: dict) -> list[dict]:
    return [
        {
            "engine": engine,
            "files": values.get("files"),
            "success": values.get("success"),
            "errors": values.get("errors"),
            "avg_elapsed_sec": values.get("avg_elapsed_sec"),
            "avg_char_count": values.get("avg_char_count"),
            "avg_block_count": values.get("avg_block_count"),
            "avg_numeric_line_ratio": values.get("avg_numeric_line_ratio"),
            "avg_korean_line_ratio": values.get("avg_korean_line_ratio"),
            "fallbacks": values.get("fallbacks"),
        }
        for engine, values in sorted((summary or {}).items())
    ]


def _render_parsing_engine_evaluation() -> None:
    st.subheader("Parsing engine evaluation")
    st.caption(
        "Run the same PDF sample through multiple parsing engines and compare extraction quality metrics."
    )

    default_path = str(Path(config_module.REPORT_PDF_DIR).expanduser())
    with st.form("parsing_engine_evaluation_form"):
        path_text = st.text_area(
            "PDF file or directory paths",
            value=default_path,
            help="Use one path per line, or comma-separated paths. Directories are sampled for *.pdf files.",
            height=72,
        )
        default_engines = [
            engine
            for engine in ["pymupdf", "opendataloader"]
            if engine in compare_pdf_extractors.SUPPORTED_EXTRACTION_ENGINES
        ]
        engines = st.multiselect(
            "Engines",
            options=sorted(compare_pdf_extractors.SUPPORTED_EXTRACTION_ENGINES),
            default=default_engines,
            help=(
                "Optional parsers are opt-in: opendataloader requires Java, "
                "docling requires `pip install docling`, and pdf-to-markdown "
                "requires the @pspdfkit/pdf-to-markdown CLI on PATH."
            ),
        )
        col1, col2, col3 = st.columns(3)
        limit = col1.number_input(
            "Sample limit",
            min_value=0,
            max_value=500,
            value=5,
            help="0 means all matching PDFs.",
        )
        raw = col2.checkbox(
            "Raw output",
            value=False,
            help="Compare raw extractor output before finance-report cleanup filters.",
        )
        write_samples = col3.checkbox(
            "Save samples",
            value=True,
            help="Persist per-engine extracted text samples for manual inspection.",
        )
        sample_chars = st.number_input(
            "Sample characters",
            min_value=0,
            max_value=200_000,
            value=4000,
            step=500,
            help="0 saves full extracted text when samples are enabled.",
        )
        submitted = st.form_submit_button("Run parsing evaluation", width="stretch")

    if submitted:
        paths = _parse_monitoring_paths(path_text)
        if not paths:
            st.warning("PDF path를 하나 이상 입력해 주세요.")
        elif not engines:
            st.warning("비교할 parsing engine을 하나 이상 선택해 주세요.")
        else:
            with st.spinner("Parsing engines are running..."):
                try:
                    result = compare_pdf_extractors.run_pdf_extraction_comparison(
                        paths,
                        engines,
                        limit=int(limit),
                        raw=raw,
                        write_samples=write_samples,
                        sample_chars=int(sample_chars),
                    )
                except Exception as exc:
                    st.error(f"Parsing evaluation failed: {exc}")
                else:
                    st.session_state.latest_parsing_evaluation = result
                    st.success("Parsing evaluation completed.")

    result = st.session_state.get("latest_parsing_evaluation")
    if not result:
        st.caption("아직 실행된 parsing evaluation 결과가 없습니다.")
        return

    st.markdown("#### Latest run")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Run ID", str(result.get("run_id")))
    col2.metric("Files", result.get("file_count", 0))
    col3.metric("Engines", len(result.get("engines") or []))
    col4.metric("Raw", "yes" if result.get("raw") else "no")

    st.markdown("#### Engine summary")
    st.dataframe(
        _engine_summary_rows(result.get("summary") or {}),
        width="stretch",
        hide_index=True,
    )

    st.markdown("#### Output artifacts")
    st.json(
        {
            "csv_path": result.get("csv_path"),
            "json_path": result.get("json_path"),
            "sample_dir": result.get("sample_dir"),
        }
    )

    rows = result.get("rows") or []
    st.markdown("#### Per-PDF rows")
    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)
        error_rows = [row for row in rows if row.get("status") != "ok"]
        if error_rows:
            with st.expander(f"Errors ({len(error_rows)})", expanded=True):
                st.dataframe(error_rows, width="stretch", hide_index=True)
    else:
        st.caption("No row data.")


def render_improvement_experiments_page() -> None:
    """Render the currently available product-improvement experiments."""
    st.header("개선 실험")
    st.caption("현재는 동일한 PDF의 파싱 엔진별 추출 품질 비교만 제공합니다.")
    _render_parsing_engine_evaluation()


def _all_thread_messages() -> list[dict]:
    threads = conversation_store.list_threads()
    return [
        {"thread": thread, "messages": conversation_store.list_messages(thread["id"])}
        for thread in threads
    ]


def _latest_saved_evaluation_run(
    exclude_path: str | None = None,
    execution_mode: str | None = None,
) -> dict | None:
    if not MONITORING_EVAL_RUN_DIR.exists():
        return None
    run_paths = sorted(MONITORING_EVAL_RUN_DIR.glob("evaluation_run_*.json"), reverse=True)
    loaded_runs: list[dict] = []
    for run_path in run_paths:
        if exclude_path and str(run_path) == exclude_path:
            continue
        try:
            run = monitoring.load_evaluation_run(run_path)
        except monitoring.CandidateLoadError:
            continue
        loaded_runs.append(run)
    matching_runs = monitoring.filter_evaluation_runs_by_mode(loaded_runs, execution_mode)
    if execution_mode == "native_v2":
        matching_runs = [
            run
            for run in matching_runs
            if monitoring.is_verified_native_v2_evaluation_run(run)
        ]
    return matching_runs[0] if matching_runs else None


def _render_experiment_monitoring(status: dict | None = None) -> None:
    st.subheader("답변 정확도 평가")
    st.caption(
        "승인된 기준 질문을 현재 Native V2 검색 데이터로 평가합니다. "
        "스키마가 검증된 V2 평가 run만 정확도 집계에 반영합니다."
    )
    try:
        dataset = monitoring.load_evaluation_dataset()
    except FileNotFoundError:
        st.info("현재 승인된 기준 질문이 없습니다.")
        return

    execution_mode = "native_v2"
    try:
        data_source = monitoring.build_native_v2_evaluation_data_source(
            status or status_cache.get_native_v2_data_status()
        )
    except monitoring.CandidateValidationError as exc:
        data_source = None
        st.warning(str(exc))
    st.info(
        "실행 시 canonical runtime이 선택한 현재 Native V2 catalog와 "
        "snapshot만 사용합니다."
    )

    cases = dataset.get("cases") or []
    case_ids = [str(case.get("id")) for case in cases]
    selected_case_ids = st.multiselect(
        "실행할 테스트 케이스",
        options=case_ids,
        default=case_ids,
        format_func=lambda case_id: next(
            (f"{case_id} · {case.get('question', '')}" for case in cases if str(case.get("id")) == case_id),
            case_id,
        ),
        help="개수가 아니라 실제로 실행할 테스트 케이스를 선택합니다.",
    )
    latency_threshold = st.number_input(
        "Latency threshold seconds",
        min_value=1.0,
        max_value=300.0,
        value=30.0,
        step=1.0,
    )
    selected_cases = monitoring.select_evaluation_cases(dataset, selected_case_ids)
    st.caption(f"선택된 테스트: {len(selected_cases)}개")
    evaluation_job_key = monitoring_jobs.session_job_key(_EVALUATION_JOB_KEY)
    evaluation_job = monitoring_jobs.get_job(evaluation_job_key)
    evaluation_running = bool(
        evaluation_job and evaluation_job["state"] == "running"
    )
    monitoring_jobs.render_job_status(
        evaluation_job_key,
        result_state_key="latest_evaluation_run",
        running_message=(
            "Evaluation을 백그라운드에서 실행 중입니다. 다른 화면을 사용해도 작업은 계속됩니다."
        ),
        success_message="Evaluation run이 저장되었습니다.",
        failure_prefix="Evaluation run failed",
    )
    if st.button(
        "Run selected evaluation cases",
        width="stretch",
        disabled=(
            not selected_cases or data_source is None or evaluation_running
        ),
    ):
        assert data_source is not None
        _job_id, started = monitoring_jobs.start_evaluation_job(
            evaluation_job_key,
            dataset=dataset,
            invoke_graph=search_engine.invoke_graph,
            output_dir=MONITORING_EVAL_RUN_DIR,
            selected_case_ids=selected_case_ids,
            latency_threshold_seconds=float(latency_threshold),
            execution_mode=execution_mode,
            data_source=data_source,
        )
        if started:
            st.toast("Evaluation run을 시작했습니다.", icon="⏳")
            st.rerun(scope="app")

    run = _latest_v2_accuracy_run()
    if not run:
        st.caption("아직 저장된 evaluation run이 없습니다.")
        return

    st.markdown("#### Latest run summary")
    st.caption(f"Execution mode: `{run.get('execution_mode')}`")
    summary = run.get("summary") or {}
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Cases", summary.get("case_count", 0))
    col2.metric("Passed", summary.get("passed", 0))
    col3.metric("Failed", summary.get("failed", 0))
    col4.metric("Pass rate", f"{summary.get('pass_rate', 0) * 100:.1f}%")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Source hit", f"{summary.get('source_hit_rate', 0) * 100:.1f}%")
    col2.metric("Citation valid", f"{summary.get('citation_valid_rate', 0) * 100:.1f}%")
    col3.metric("No-result", f"{summary.get('no_result_rate', 0) * 100:.1f}%")
    latency = summary.get("avg_latency_seconds")
    col4.metric("Avg latency", "-" if latency is None else f"{latency:.2f}s")

    previous = _latest_saved_evaluation_run(
        exclude_path=run.get("json_path"),
        execution_mode=run.get("execution_mode"),
    )
    comparison = monitoring.compare_evaluation_runs(run, previous)
    if comparison:
        st.markdown("#### Previous run comparison")
        st.caption("같은 execution mode의 이전 run과만 비교합니다.")
        st.dataframe([comparison], width="stretch", hide_index=True)

    st.markdown("#### Run artifacts")
    st.code(run.get("json_path") or "", language="text")
    st.markdown("#### Case results")
    results = run.get("results") or []
    st.dataframe(results, width="stretch", hide_index=True)

    failure_actions = monitoring.build_evaluation_failure_actions(results)
    st.markdown("#### Failure triage")
    if failure_actions:
        st.warning("Fail 케이스는 아래 권장 조치 기준으로 다음 작업을 선택하세요.")
        st.dataframe(failure_actions, width="stretch", hide_index=True)
        failed_case_ids = [str(row["case_id"]) for row in failure_actions if row.get("case_id")]
        if st.button(
            "Rerun failed cases only",
            width="stretch",
            disabled=data_source is None or evaluation_running,
        ):
            assert data_source is not None
            _job_id, started = monitoring_jobs.start_evaluation_job(
                evaluation_job_key,
                dataset=dataset,
                invoke_graph=search_engine.invoke_graph,
                output_dir=MONITORING_EVAL_RUN_DIR,
                selected_case_ids=failed_case_ids,
                latency_threshold_seconds=float(latency_threshold),
                execution_mode=execution_mode,
                data_source=data_source,
            )
            if started:
                st.toast("Failed cases rerun을 시작했습니다.", icon="⏳")
                st.rerun(scope="app")
    else:
        st.success("현재 run에는 triage가 필요한 fail 케이스가 없습니다.")


def _latest_v2_accuracy_run() -> dict | None:
    latest = st.session_state.get("latest_evaluation_run")
    if monitoring.is_verified_native_v2_evaluation_run(latest):
        return latest
    return _latest_saved_evaluation_run(execution_mode="native_v2")


def _render_answer_metrics(
    summary: dict,
    accuracy: dict,
) -> None:
    speed_column, accuracy_column = st.columns(2)
    p95_latency = summary.get("p95_latency_seconds")
    average_latency = summary.get("avg_latency_seconds")
    latency_count = int(summary.get("latency_sample_count") or 0)
    with speed_column:
        st.metric(
            "응답 속도",
            "측정 전" if p95_latency is None else f"{p95_latency:.2f}초",
        )
        if p95_latency is None:
            st.caption("아직 완료된 답변의 속도 표본이 없습니다.")
        else:
            st.caption(
                f"최근 {latency_count}개 답변의 P95 · "
                f"평균 {average_latency:.2f}초"
            )

    with accuracy_column:
        accuracy_rate = accuracy.get("accuracy_rate")
        st.metric(
            "답변 정확도",
            (
                "측정 전"
                if accuracy_rate is None
                else f"{float(accuracy_rate) * 100:.1f}%"
            ),
        )
        if accuracy_rate is None:
            st.caption("승인된 Native V2 정확도 평가가 없습니다.")
        else:
            st.caption(
                "정확성 검사만 반영 · "
                f"{accuracy.get('passed', 0)}/{accuracy.get('case_count', 0)}문항"
            )


def _render_global_monitoring(
    summary: dict,
    integrity: dict,
    accuracy: dict,
) -> None:
    problem_checks = [
        {
            "항목": _V2_CHECK_LABELS.get(key, key),
            "상태": value.get("status"),
            "세부 정보": value.get("detail"),
            "다음 확인": _V2_CHECK_ACTIONS.get(
                key,
                "검색 자료 준비에서 기술 세부정보를 확인하세요.",
            ),
        }
        for key, value in integrity["checks"].items()
        if value.get("status") != "pass"
    ]
    st.markdown("#### 검색 자료 확인 필요")
    if problem_checks:
        st.dataframe(
            problem_checks,
            width="stretch",
            hide_index=True,
        )
    else:
        st.success("현재 검색 자료에서 확인이 필요한 문제가 없습니다.")

    failures = summary.get("recent_failures") or []
    st.markdown("#### 문제 응답")
    if failures:
        st.dataframe(
            [
                {
                    "발생 시각": row.get("created_at"),
                    "대화": row.get("thread_name"),
                    "응답 시간": row.get("latency_seconds"),
                    "오류": row.get("error"),
                }
                for row in failures
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.caption("최근 실패 응답이 없습니다.")

    st.markdown("#### 정확도 평가 확인")
    if not accuracy.get("measured"):
        st.caption("확인할 승인된 Native V2 정확도 평가가 없습니다.")
    else:
        failed_accuracy_cases = max(
            int(accuracy.get("case_count") or 0)
            - int(accuracy.get("passed") or 0),
            0,
        )
        if failed_accuracy_cases:
            st.warning(
                f"정확성 검사를 통과하지 못한 평가 문항이 "
                f"{failed_accuracy_cases}건 있습니다."
            )
        else:
            st.success("최근 정확도 평가의 모든 문항이 통과했습니다.")


def _render_v2_data_diagnostics(status: dict) -> None:
    integrity = monitoring.summarize_v2_data_integrity(status)
    if not monitoring.is_native_v2_status(status):
        st.warning("Native V2 검색 데이터 상태를 사용할 수 없습니다.")
        with st.expander("기술 세부정보", expanded=False):
            st.dataframe(
                [
                    {
                        "항목": _V2_CHECK_LABELS.get(key, key),
                        "상태": value.get("status"),
                        "세부": value.get("detail"),
                    }
                    for key, value in integrity["checks"].items()
                ],
                width="stretch",
                hide_index=True,
            )
        return

    db_status = status["db"]
    retrieval_status = status.get("retrieval") or {}

    col1, col2, col3 = st.columns(3)
    col1.metric("검색 가능한 문서", f"{db_status['embedded_reports']}건")
    col2.metric("아직 반영되지 않은 문서", f"{db_status['pending_reports']}건")
    col3.metric("검색 자료 반영률", f"{status['search_coverage_ratio'] * 100:.1f}%")

    pending_cleanup_count = int(
        retrieval_status.get("pending_cleanup_file_count") or 0
    )
    if pending_cleanup_count:
        cleanup_size = status_module.format_bytes(
            int(retrieval_status.get("pending_cleanup_size_bytes") or 0)
        )
        cleanup_age = status_module.format_duration(
            int(
                retrieval_status.get(
                    "oldest_pending_cleanup_age_seconds"
                )
                or 0
            )
        )
        st.warning(
            "검색 데이터 정리 대기: "
            f"{pending_cleanup_count}개 파일 · "
            f"{cleanup_size} · 최장 {cleanup_age}"
        )

    with st.expander("기술 세부정보", expanded=False):
        st.dataframe(
            [
                {
                    "항목": _V2_CHECK_LABELS.get(key, key),
                    "상태": value.get("status"),
                    "세부": value.get("detail"),
                }
                for key, value in integrity["checks"].items()
            ],
            width="stretch",
            hide_index=True,
        )

    data_views.render_unembedded_reports(status)


def _chat_execution_label(execution: dict, route: str | None = None) -> str:
    strategy = execution.get("strategy") or execution.get("execution_strategy")
    mode = execution.get("execution_mode")
    if strategy == "company_comparison":
        if mode == "send":
            observed = execution.get("observed_peak_retrieval_concurrency")
            effective = execution.get("retrieval_concurrency_limit")
            if observed == 1 or (observed is None and effective == 1):
                return "Send 직렬 실행 (동시성 1)"
            if isinstance(observed, int) and observed > 1:
                return f"Send 병렬 실행 (동시성 {observed})"
            if isinstance(effective, int) and effective > 1:
                return f"Send 비교 · 실측 동시성 미계측 (상한 {effective})"
            return "Send 비교 · 실측 동시성 미계측"
        if mode == "sequential_reference":
            return "순차 비교"
        return "복수 기업 비교 · 방식 미계측"
    if strategy == "vectordb" or route == "vectordb":
        return "Vector DB 검색"
    if strategy == "rdb" or route == "rdb":
        return "RDB 조회"
    return "측정 전"


def _chat_target_coverage_label(execution: dict, source_count: int | None = None) -> str:
    requested = execution.get("requested_target_count")
    available = execution.get("available_target_count")
    if isinstance(requested, int) and isinstance(available, int):
        return f"{available}/{requested} 성공"
    if isinstance(source_count, int):
        return f"문서 {source_count}개"
    return "측정 전"


def _chat_grounding_label(status: str | None) -> str:
    return {
        "linked": "연결됨",
        "partial": "부분 연결",
        "unavailable": "근거 없음",
        "not_applicable": "해당 없음",
        "not_evaluated": "평가 안 함",
        "not_measured": "측정 전",
    }.get(status, "측정 전")


def _chat_execution_status_label(status: str | None) -> str:
    return {
        "complete": "전체 대상 성공",
        "partial": "일부 대상 누락",
        "insufficient": "비교 근거 부족",
        "all_failed": "전체 검색 실패",
        "revision_mismatch": "revision 불일치",
    }.get(status, "상태 미계측")


def _chat_scope_caption(detail: dict) -> str:
    filters = (detail.get("scope") or {}).get("search_filters") or {}
    parts: list[str] = []
    start = filters.get("report_date_start")
    end = filters.get("report_date_end")
    if start or end:
        parts.append(f"기간 {start or '-'}~{end or '-'}")
    report_type = {
        "company": "기업",
        "industry": "산업",
        "economy": "경제",
    }.get(filters.get("report_type"), filters.get("report_type"))
    if report_type:
        parts.append(f"유형 {report_type}")
    targets = filters.get("target_names") or []
    if not targets and filters.get("target_name"):
        targets = [filters["target_name"]]
    if isinstance(targets, list) and targets:
        parts.append(f"대상 {', '.join(str(target) for target in targets)}")
    return "검색 범위 · " + " · ".join(parts) if parts else "검색 범위가 기록되지 않았습니다."


def _chat_performance_timing_rows(detail: dict) -> list[dict]:
    timing = detail.get("timing") or {}
    branch_timing = (detail.get("execution") or {}).get("branch_timing") or {}
    specs = (
        ("전체 응답", timing.get("total_seconds"), "질문 제출부터 답변 저장까지"),
        (
            "비교 사전선택",
            timing.get("comparison_preflight_seconds"),
            "revision 고정·최신 문서 확정",
        ),
        (
            "Vector DB 검색",
            timing.get("vector_search_seconds"),
            "단일 검색 또는 비교 branch의 실제 경과시간",
        ),
        (
            "답변 합성",
            timing.get("answer_synthesis_seconds"),
            "최종 context를 사용한 LLM 호출",
        ),
        (
            "미분류 시간",
            timing.get("unattributed_seconds"),
            "전체시간에서 계측 구간을 제외한 값",
        ),
        ("RDB 조회", timing.get("rdb_query_seconds"), "SQL 검증·실행·결과 반환"),
        (
            "가장 느린 대상 검색",
            branch_timing.get("slowest_retrieval_seconds"),
            "복수 기업 branch 중 최댓값",
        ),
        (
            "대상 검색 작업 합",
            branch_timing.get("total_retrieval_work_seconds"),
            "병렬 branch 작업시간 합계",
        ),
        (
            "최대 검색 대기",
            branch_timing.get("max_queue_wait_seconds"),
            "동시성 제한 대기 최댓값",
        ),
    )
    return [
        {"구간": label, "시간": _format_chat_duration(value), "측정 경계": boundary}
        for label, value, boundary in specs
        if isinstance(value, (int, float))
    ]


def _chat_technical_sections(detail: dict) -> dict:
    """Normalize partial or legacy trace detail before rendering raw sections."""

    dict_sections = (
        "graph_manifest",
        "timing",
        "generation_performance",
        "execution",
        "retrieval_k",
        "query_rewrite",
        "scope",
        "routing",
        "retrieval",
        "answer",
        "grounding",
        "state_status",
        "state_transitions",
    )
    sections = {
        key: detail.get(key) if isinstance(detail.get(key), dict) else {}
        for key in dict_sections
    }
    sections["used_chunks"] = (
        detail.get("used_chunks")
        if isinstance(detail.get("used_chunks"), list)
        else []
    )
    sections["graph_schema_version"] = detail.get("graph_schema_version")
    sections["node_runs"] = (
        detail.get("node_runs") if isinstance(detail.get("node_runs"), list) else []
    )
    return sections


def _chat_monitoring_node_status_label(status: object) -> str:
    return {
        "completed": "완료",
        "partial": "일부 완료",
        "failed": "실패",
        "running": "처리 중",
        "no_results": "결과 없음",
        "not_applicable": "해당 없음",
        "not_measured": "측정 전",
        "not_run": "실행 안 함",
        "interrupted": "중단됨",
    }.get(str(status or "not_measured"), "측정 전")


def _chat_monitoring_node_button_label(node: dict) -> str:
    icon = {
        "completed": "✓",
        "partial": "!",
        "failed": "×",
        "running": "…",
        "no_results": "○",
        "not_applicable": "–",
        "not_measured": "·",
        "not_run": "◇",
        "interrupted": "∥",
    }.get(str(node.get("status") or "not_measured"), "·")
    duration = node.get("duration_seconds")
    duration_label = (
        f" · {_format_chat_duration(duration)}"
        if isinstance(duration, (int, float))
        else ""
    )
    return (
        f"{icon} {node.get('label')} · "
        f"{_chat_monitoring_node_status_label(node.get('status'))}{duration_label}"
    )


def _chat_monitoring_graph_dot(graph: dict) -> str:
    """Build a safe DOT projection from a persisted graph snapshot."""

    def quote(value: object) -> str:
        return (
            str(value or "")
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\r", "")
            .replace("\n", "\\n")
        )

    palette = {
        "completed": ("#E6FFFA", "#2F855A"),
        "partial": ("#FFFAF0", "#B7791F"),
        "failed": ("#FFF5F5", "#C53030"),
        "running": ("#EBF8FF", "#2B6CB0"),
        "interrupted": ("#FAF5FF", "#805AD5"),
        "not_run": ("#F7FAFC", "#A0AEC0"),
        "not_measured": ("#F7FAFC", "#718096"),
    }
    icons = {
        "completed": "✓",
        "partial": "!",
        "failed": "×",
        "running": "…",
        "interrupted": "∥",
        "not_run": "◇",
        "not_measured": "·",
    }
    lines = [
        "digraph chat_monitoring {",
        '  graph [rankdir="TB", bgcolor="transparent", ranksep="0.45", nodesep="0.24"];',
        '  node [shape="box", style="rounded,filled", fontname="Arial", fontsize="10", margin="0.12,0.08"];',
        '  edge [arrowsize="0.65", color="#A0AEC0"];',
    ]
    node_ids = set()
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict) or not node.get("id"):
            continue
        node_id = str(node["id"])
        node_ids.add(node_id)
        status = str(node.get("status") or "not_measured")
        fill_color, border_color = palette.get(status, palette["not_measured"])
        duration = node.get("duration_seconds")
        duration_label = (
            f"\\n{float(duration):.3f}s"
            if isinstance(duration, (int, float)) and not isinstance(duration, bool)
            else ""
        )
        label = f"{icons.get(status, '·')} {node.get('label') or node_id}{duration_label}"
        shape = "ellipse" if node.get("kind") == "boundary" else "box"
        lines.append(
            f'  "{quote(node_id)}" [label="{quote(label)}", shape="{shape}", '
            f'fillcolor="{fill_color}", color="{border_color}"];'
        )
    for edge in graph.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source not in node_ids or target not in node_ids:
            continue
        style = "dashed" if edge.get("conditional") else "solid"
        lines.append(
            f'  "{quote(source)}" -> "{quote(target)}" '
            f'[style="{style}", color="#A0AEC0", penwidth="1.0"];'
        )
    lines.append("}")
    return "\n".join(lines)


def _render_chat_graph_connector(symbol: str) -> None:
    st.markdown(
        f"<div style='text-align:center; color:#718096; line-height:1.1'>{symbol}</div>",
        unsafe_allow_html=True,
    )


def _render_chat_monitoring_graph(
    detail: dict,
    *,
    current_id: str,
    selected_message_id: object,
) -> tuple[str, dict]:
    graph = monitoring.build_chat_monitoring_graph(detail)
    node_by_id = {node["id"]: node for node in graph["nodes"]}
    selection_key = (
        f"_chat_monitoring_selected_node_{current_id}_{selected_message_id}"
    )
    valid_selections = {"overview", *node_by_id}
    selected_node_id = st.session_state.get(selection_key, "overview")
    if selected_node_id not in valid_selections:
        selected_node_id = "overview"
        st.session_state[selection_key] = selected_node_id

    def render_node(node: dict) -> None:
        nonlocal selected_node_id
        if st.button(
            _chat_monitoring_node_button_label(node),
            key=f"chat_monitoring_node_{current_id}_{selected_message_id}_{node['id']}",
            type="primary" if selected_node_id == node["id"] else "secondary",
            help=node.get("summary"),
            width="stretch",
        ):
            selected_node_id = node["id"]
            st.session_state[selection_key] = selected_node_id

    with st.container(border=True):
        st.markdown("#### 실행 그래프")
        is_persisted_graph = graph.get("source") == "persisted_manifest"
        is_graph_error = graph.get("source") == "persisted_graph_error"
        if is_persisted_graph:
            revision = str(graph.get("revision") or "")
            revision_label = revision[:12] if revision else "없음"
            st.caption(
                f"응답 저장 스냅샷 · schema v{graph.get('schema_version')} · "
                f"topology {revision_label}"
            )
        elif is_graph_error:
            st.error(
                str(graph.get("error_message") or "실행 그래프를 표시할 수 없습니다.")
            )
            st.caption(
                f"오류 코드: {graph.get('error_code')} · "
                f"오류 유형: {graph.get('error_type') or '해당 없음'} · "
                f"저장 schema: {graph.get('schema_version')}"
            )
        else:
            st.caption(
                "이전 응답용 6단계 호환 그래프입니다. 노드를 선택하면 우측에서 해당 단계만 확인합니다."
            )
        if st.button(
            "전체 지표",
            key=f"chat_monitoring_overview_{current_id}_{selected_message_id}",
            type="primary" if selected_node_id == "overview" else "secondary",
            width="stretch",
        ):
            selected_node_id = "overview"
            st.session_state[selection_key] = selected_node_id

        if is_persisted_graph:
            graph_height = min(900, max(420, len(node_by_id) * 28))
            st.graphviz_chart(
                _chat_monitoring_graph_dot(graph),
                width="stretch",
                height=graph_height,
            )
            task_nodes = [
                node
                for node in graph.get("nodes") or []
                if node.get("kind") != "boundary"
            ]
            executed_nodes = sorted(
                (
                    node
                    for node in task_nodes
                    if node.get("status") != "not_run"
                ),
                key=lambda node: (
                    int(node.get("first_run_sequence") or 10**9),
                    int(node.get("order") or 0),
                ),
            )
            skipped_nodes = sorted(
                (
                    node
                    for node in task_nodes
                    if node.get("status") == "not_run"
                ),
                key=lambda node: int(node.get("order") or 0),
            )
            if executed_nodes:
                st.markdown("**실행된 노드 선택**")
                for node in executed_nodes:
                    render_node(node)
            if skipped_nodes:
                with st.expander(
                    f"실행되지 않은 조건부 분기 ({len(skipped_nodes)})",
                    expanded=False,
                ):
                    for node in skipped_nodes:
                        render_node(node)
            st.caption(
                "실선은 일반 edge, 점선은 조건부 edge입니다. edge는 저장 topology이며 실제 통과 여부를 뜻하지 않습니다."
            )
        elif not is_graph_error:
            _render_chat_graph_connector("↓")
            render_node(node_by_id["input"])
            _render_chat_graph_connector("↙　↘")
            query_column, scope_column = st.columns(2, gap="small")
            with query_column:
                render_node(node_by_id["query_rewrite"])
            with scope_column:
                render_node(node_by_id["search_scope"])
            _render_chat_graph_connector("↘　↙")
            render_node(node_by_id["routing"])
            _render_chat_graph_connector("↓")
            render_node(node_by_id["retrieval"])
            _render_chat_graph_connector("↓")
            render_node(node_by_id["answer"])
            st.caption(
                "저장되지 않은 실제 LangGraph NodeRun과 노드별 시간은 추정하지 않습니다."
            )
    return selected_node_id, graph


def _render_chat_monitoring_node_detail(
    selected_node_id: str,
    *,
    detail: dict,
    graph: dict,
) -> None:
    node = next(
        (item for item in graph.get("nodes") or [] if item.get("id") == selected_node_id),
        None,
    )
    if not node:
        st.warning("선택한 실행 단계를 찾을 수 없습니다.")
        return

    st.subheader(str(node.get("label") or "실행 단계"))
    status_column, timing_column = st.columns(2)
    status_column.metric(
        "단계 상태",
        _chat_monitoring_node_status_label(node.get("status")),
    )
    timing_column.metric(
        "노드 실행 구간",
        _format_chat_duration(node.get("duration_seconds")),
    )
    st.caption(str(node.get("summary") or "저장된 요약이 없습니다."))
    status = node.get("status")
    if status == "failed":
        st.error("이 단계가 실패한 것으로 기록되었습니다.")
    elif status in {"partial", "no_results"}:
        st.warning("이 단계는 일부 결과만 남았거나 검색 결과가 없습니다.")
    elif status == "not_measured":
        st.info("이 응답에는 이 단계의 상태 또는 시간이 저장되지 않았습니다.")
    elif status == "not_run":
        st.info("이 응답에서는 이 조건부 분기를 실행하지 않았습니다.")

    mapped_detail_section = node.get("detail_section")
    detail_section = mapped_detail_section or selected_node_id
    if graph.get("source") == "persisted_manifest":
        runs = node.get("runs") or []
        incoming = [
            edge
            for edge in graph.get("edges") or []
            if edge.get("target") == selected_node_id
        ]
        outgoing = [
            edge
            for edge in graph.get("edges") or []
            if edge.get("source") == selected_node_id
        ]
        topology_columns = st.columns(3)
        topology_columns[0].metric("실행 횟수", len(runs))
        topology_columns[1].metric("들어오는 edge", len(incoming))
        topology_columns[2].metric("나가는 edge", len(outgoing))
        total_work_seconds = node.get("total_work_seconds")
        if len(runs) > 1 and isinstance(total_work_seconds, (int, float)):
            st.caption(
                "병렬 실행을 포함한 개별 invocation 시간 합계: "
                f"{_format_chat_duration(total_work_seconds)}"
            )
        if runs:
            st.markdown("**저장된 NodeRun**")
            st.dataframe(
                [
                    {
                        "순서": run.get("sequence"),
                        "실행 회차": run.get(
                            "invocation_index", run.get("attempt")
                        ),
                        "상태": _chat_monitoring_node_status_label(
                            run.get("status")
                        ),
                        "시간": _format_chat_duration(
                            run.get("duration_seconds")
                        ),
                        "결과 key": ", ".join(run.get("result_keys") or []),
                    }
                    for run in runs
                ],
                width="stretch",
                hide_index=True,
            )
        with st.expander("저장된 topology", expanded=False):
            st.json(
                {
                    "node": {
                        key: value
                        for key, value in node.items()
                        if key not in {"runs"}
                    },
                    "incoming_edges": incoming,
                    "outgoing_edges": outgoing,
                }
            )
        if not mapped_detail_section:
            return

    transitions = detail.get("state_transitions") or {}
    if detail_section == "input":
        input_detail = transitions.get("input") or {}
        st.markdown("**입력 질문**")
        st.write(
            (detail.get("query_rewrite") or {}).get("original_question")
            or input_detail.get("question")
            or "측정 전"
        )
        prior_scope = input_detail.get("prior_search_scope")
        if prior_scope:
            with st.expander("이전 대화에서 이어진 검색 범위", expanded=False):
                st.json(prior_scope)
    elif detail_section == "query_rewrite":
        query_rewrite = detail.get("query_rewrite") or {}
        st.markdown("**원문 질문**")
        st.write(query_rewrite.get("original_question") or "측정 전")
        st.markdown("**재작성 결과**")
        st.write(query_rewrite.get("rewritten_query") or "측정 전")
        history_column, followup_column = st.columns(2)
        history_column.metric(
            "대화 이력 사용",
            "예" if query_rewrite.get("uses_chat_history") is True else "아니오"
            if query_rewrite.get("uses_chat_history") is False
            else "측정 전",
        )
        followup_column.metric(
            "이전 범위 이어쓰기",
            "예" if query_rewrite.get("followup_scope_intent") is True else "아니오"
            if query_rewrite.get("followup_scope_intent") is False
            else "측정 전",
        )
        with st.expander("저장된 질문 재작성 정보", expanded=False):
            st.json(query_rewrite)
            st.json(transitions.get("after_query_rewrite") or {})
    elif detail_section == "search_scope":
        scope = detail.get("scope") or {}
        after_scope = transitions.get("after_search_scope") or {}
        scope_column, file_column = st.columns(2)
        scope_column.metric(
            "범위 출처",
            (detail.get("query_rewrite") or {}).get("scope_source") or "측정 전",
        )
        file_column.metric(
            "검색 파일",
            after_scope.get("search_scope_file_count")
            if after_scope.get("search_scope_file_count") is not None
            else "측정 전",
        )
        st.markdown("**적용 검색 조건**")
        search_filters = scope.get("search_filters") or {}
        if search_filters:
            st.json(search_filters)
        else:
            st.caption("저장된 검색 조건이 없습니다.")
        with st.expander("검색 범위 세부정보", expanded=False):
            st.json(scope)
            st.json(after_scope)
    elif detail_section == "routing":
        routing = detail.get("routing") or {}
        route_column, hint_column = st.columns(2)
        route_column.metric("선택 경로", routing.get("route") or "측정 전")
        hint_column.metric("경로 힌트", routing.get("route_hint") or "측정 전")
        with st.expander("저장된 라우팅 정보", expanded=True):
            st.json(routing)
            st.json(transitions.get("after_routing") or {})
    elif detail_section == "retrieval":
        retrieval = detail.get("retrieval") or {}
        retrieval_k = detail.get("retrieval_k") or {}
        execution = detail.get("execution") or {}
        metric_columns = st.columns(3)
        metric_columns[0].metric(
            "필터 후 후보",
            retrieval.get("candidate_count_after_filter")
            if retrieval.get("candidate_count_after_filter") is not None
            else "측정 전",
        )
        metric_columns[1].metric(
            "최종 context",
            retrieval_k.get("context_count")
            if retrieval_k.get("context_count") is not None
            else "측정 전",
        )
        metric_columns[2].metric(
            "검색 실행",
            _chat_execution_label(execution, (detail.get("routing") or {}).get("route")),
        )
        branches = execution.get("branches") or []
        if branches:
            st.markdown("**대상별 검색**")
            st.dataframe(branches, width="stretch", hide_index=True)
        documents = detail.get("used_documents") or detail.get("rdb_evidence") or []
        if documents:
            st.markdown("**답변에 전달된 문서**")
            st.dataframe(documents, width="stretch", hide_index=True)
        else:
            st.caption("이 단계에 연결된 문서 근거가 없습니다.")
        with st.expander("검색 기술 세부정보", expanded=False):
            st.json(retrieval_k)
            st.json(execution)
            st.json(retrieval)
            if detail.get("used_chunks"):
                st.dataframe(
                    detail["used_chunks"],
                    width="stretch",
                    hide_index=True,
                )
    elif detail_section == "answer":
        generation = detail.get("generation_performance") or {}
        answer = detail.get("answer") or {}
        grounding = detail.get("grounding") or {}
        metric_columns = st.columns(4)
        metric_columns[0].metric(
            "입력 토큰",
            _format_chat_token_count(generation.get("input_tokens")),
        )
        metric_columns[1].metric(
            "출력 토큰",
            _format_chat_token_count(generation.get("output_tokens")),
        )
        metric_columns[2].metric(
            "최초 토큰",
            _format_chat_duration(generation.get("time_to_first_token_seconds")),
        )
        metric_columns[3].metric(
            "실제 provider",
            generation.get("provider_name") or "측정 전",
        )
        st.markdown("**답변 요약**")
        st.write(answer.get("assistant_preview") or "측정 전")
        grounding_column, source_column = st.columns(2)
        grounding_column.metric(
            "인용 연결",
            _chat_grounding_label(grounding.get("status")),
        )
        source_column.metric(
            "사용 문서",
            answer.get("source_count")
            if answer.get("source_count") is not None
            else "측정 전",
        )
        with st.expander("답변 생성 세부정보", expanded=False):
            st.json(generation)
            st.json(answer)
            st.json(grounding)


def _render_chat_monitoring_workspace(
    detail: dict,
    *,
    trace_summary: dict,
    diff: dict,
    hints: list[str],
    current_id: str,
    selected_message_id: object,
) -> None:
    graph_column, detail_column = st.columns(
        [0.34, 0.66],
        gap="large",
        vertical_alignment="top",
    )
    with graph_column:
        selected_node_id, graph = _render_chat_monitoring_graph(
            detail,
            current_id=current_id,
            selected_message_id=selected_message_id,
        )
    with detail_column:
        if selected_node_id == "overview":
            st.subheader("전체 지표")
            _render_chat_answer_performance(
                detail,
                trace_summary=trace_summary,
                diff=diff,
                hints=hints,
                current_id=current_id,
                selected_message_id=selected_message_id,
            )
        else:
            _render_chat_monitoring_node_detail(
                selected_node_id,
                detail=detail,
                graph=graph,
            )


def _render_chat_answer_performance(
    detail: dict,
    *,
    trace_summary: dict,
    diff: dict,
    hints: list[str],
    current_id: str,
    selected_message_id: object,
) -> None:
    execution = detail.get("execution") or {}
    timing = detail.get("timing") or {}
    grounding = detail.get("grounding") or {}
    answer = detail.get("answer") or {}

    st.markdown("#### 선택 답변 상태")
    columns = st.columns(4)
    metric_specs = (
        (
            "선택 답변 총시간",
            _format_chat_duration(timing.get("total_seconds")),
            "질문 제출~답변 저장",
        ),
        (
            "검색 실행",
            _chat_execution_label(execution, (detail.get("routing") or {}).get("route")),
            _chat_execution_status_label(execution.get("status")),
        ),
        (
            "대상별 근거",
            _chat_target_coverage_label(execution, answer.get("source_count")),
            f"사용 문서 {len(detail.get('used_documents') or detail.get('rdb_evidence') or [])}개",
        ),
        (
            "인용 연결",
            _chat_grounding_label(grounding.get("status")),
            "의미 정확도 평가는 아님",
        ),
    )
    for column, (label, value, caption) in zip(columns, metric_specs, strict=True):
        with column:
            st.metric(label, value)
            st.caption(caption)
    st.caption(_chat_scope_caption(detail))
    latest_selection = execution.get("latest_selection") or {}
    if latest_selection.get("mode") == "latest_per_target":
        requested = latest_selection.get("requested_target_count")
        cited = latest_selection.get("cited_target_count")
        cited_label = cited if cited is not None else "측정 전"
        st.caption(
            "최신 문서 · "
            f"확정 {latest_selection.get('resolved_target_count', 0)}/{requested} · "
            f"prompt 반영 {latest_selection.get('context_target_count', 0)}/{requested} · "
            f"답변 인용 {cited_label}/{requested}"
        )

    comparison_status = execution.get("status")
    if hints:
        for hint in hints:
            st.warning(hint)
    elif comparison_status in {"partial", "insufficient", "all_failed", "revision_mismatch"}:
        st.warning(f"비교 검색 상태를 확인하세요: {_chat_execution_status_label(comparison_status)}")
    elif grounding.get("status") in {"partial", "unavailable"}:
        st.warning(f"답변 근거 연결 상태를 확인하세요: {_chat_grounding_label(grounding.get('status'))}")
    else:
        st.success("검색 범위와 근거 연결에서 자동 감지된 흔한 문제는 없습니다.")

    for gap in execution.get("measurement_gaps") or []:
        if gap == "execution_mode_not_measured":
            st.info("이 응답에는 비교 실행 방식이 저장되지 않아 Send 적용 여부를 단정할 수 없습니다.")
        elif gap == "comparison_branch_timing_not_measured":
            st.info("이 응답에는 대상별 검색 시간이 없어 총시간의 병목 구간을 판정할 수 없습니다.")

    timing_rows = _chat_performance_timing_rows(detail)
    st.markdown("#### 병목 확인")
    if timing_rows:
        st.dataframe(timing_rows, width="stretch", hide_index=True)
    else:
        st.caption("이 응답에는 사용할 수 있는 시간 계측값이 없습니다.")
    st.caption(
        "대상 검색 작업 합은 병렬 branch의 작업량 합계이므로 전체 응답시간과 직접 합산하지 않습니다. "
        "query rewrite·rerank 시간이 따로 기록되지 않으면 해당 구간은 병목으로 단정하지 않습니다."
    )

    generation = detail.get("generation_performance") or {}
    st.markdown("#### 모델 생성")
    generation_columns = st.columns(5)
    generation_specs = (
        (
            "입력 토큰",
            _format_chat_token_count(generation.get("input_tokens")),
            "모델에 전달된 prompt",
        ),
        (
            "출력 토큰",
            _format_chat_token_count(generation.get("output_tokens")),
            "모델이 생성한 completion",
        ),
        (
            "최초 토큰",
            _format_chat_duration(generation.get("time_to_first_token_seconds")),
            "요청 시작~첫 출력 chunk",
        ),
        (
            "실제 provider",
            generation.get("provider_name") or "측정 전",
            f"gateway {generation.get('gateway_provider') or '미확인'}",
        ),
        (
            "초당 생성 토큰",
            _format_chat_token_rate(generation.get("output_tokens_per_second")),
            "요청 시작~완료 기준",
        ),
    )
    for column, (label, value, caption) in zip(
        generation_columns,
        generation_specs,
        strict=True,
    ):
        with column:
            st.metric(label, value)
            st.caption(caption)
    st.caption(
        f"모델 {generation.get('model_name') or '미확인'} · "
        f"생성 호출 {generation.get('call_count') if generation.get('call_count') is not None else '측정 전'}회 · "
        f"스트리밍 계측 {generation.get('streamed_call_count') if generation.get('streamed_call_count') is not None else '측정 전'}회"
    )
    if generation.get("status") == "not_measured":
        st.info("이전 응답이거나 생성 계측을 지원하지 않는 경로여서 모델 생성 지표가 없습니다.")
    elif generation.get("measurement_gaps"):
        st.caption(
            "미계측 항목: "
            + ", ".join(str(item) for item in generation["measurement_gaps"])
        )

    branches = execution.get("branches") or []
    if branches:
        status_labels = {
            "success": "성공",
            "success_degraded": "성공(저하)",
            "no_result": "결과 없음",
            "failed": "실패",
            "revision_mismatch": "revision 불일치",
            "not_measured": "측정 전",
        }
        st.markdown("#### 대상별 검색")
        st.dataframe(
            [
                {
                    "대상": branch.get("target"),
                    "상태": status_labels.get(branch.get("status"), branch.get("status")),
                    "후보": branch.get("candidate_count"),
                    "검색시간": _format_chat_duration(branch.get("retrieval_seconds")),
                    "대기시간": _format_chat_duration(branch.get("queue_wait_seconds")),
                }
                for branch in branches
            ],
            width="stretch",
            hide_index=True,
        )
        st.caption(
            f"전역 rerank {execution.get('rerank_calls') if execution.get('rerank_calls') is not None else '측정 전'}회 · "
            f"답변 합성 {execution.get('synthesis_calls') if execution.get('synthesis_calls') is not None else '측정 전'}회 · "
            f"요청별 동시성 상한 {execution.get('retrieval_concurrency_limit') if execution.get('retrieval_concurrency_limit') is not None else '측정 전'} · "
            f"실측 최대 {execution.get('observed_peak_retrieval_concurrency') if execution.get('observed_peak_retrieval_concurrency') is not None else '측정 전'}"
        )

    document_rows = detail.get("used_documents") or []
    rdb_evidence_rows = detail.get("rdb_evidence") or []
    st.markdown("#### 답변에 사용된 문서")
    if document_rows:
        st.dataframe(
            [
                {
                    "대상": row.get("target_name"),
                    "발간일": row.get("report_date"),
                    "제목": row.get("title"),
                    "증권사": row.get("broker"),
                    "파일": row.get("file_name"),
                    "사용 chunk": row.get("chunk_count"),
                    "인용 chunk": row.get("cited_chunk_count"),
                }
                for row in document_rows
            ],
            width="stretch",
            hide_index=True,
        )
    elif rdb_evidence_rows:
        st.dataframe(
            [
                {
                    "대상": row.get("target_name"),
                    "발간일": row.get("report_date"),
                    "제목": row.get("title"),
                    "증권사": row.get("broker"),
                    "파일": row.get("file_name"),
                }
                for row in rdb_evidence_rows
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.caption("이 답변에 연결된 문서 근거가 없습니다.")

    diagnostic_view = st.segmented_control(
        "추가 진단",
        options=["overview", "diff", "technical"],
        default="overview",
        format_func={
            "overview": "기본만",
            "diff": "이전 응답 비교",
            "technical": "기술 세부정보",
        }.__getitem__,
        key=f"chat_monitoring_detail_{current_id}_{selected_message_id}",
        width="stretch",
    )
    if diagnostic_view == "diff":
        if diff:
            st.json(diff)
        else:
            st.caption("비교할 이전 성공 응답이 없습니다.")
    elif diagnostic_view == "technical":
        technical = _chat_technical_sections(detail)
        st.markdown("**요약·시간·실행 계획**")
        st.json(trace_summary)
        st.json(technical["timing"])
        st.json(technical["generation_performance"])
        st.json(technical["execution"])
        st.markdown("**검색 k·범위·라우팅**")
        st.json(technical["retrieval_k"])
        st.json(technical["query_rewrite"])
        st.json(technical["scope"])
        st.json(technical["routing"])
        st.markdown("**검색·답변·근거 연결**")
        st.json(technical["retrieval"])
        st.json(technical["answer"])
        st.json(technical["grounding"])
        if technical["used_chunks"]:
            st.markdown("**prompt에 사용한 chunk**")
            st.dataframe(
                technical["used_chunks"],
                width="stretch",
                hide_index=True,
            )
        st.markdown("**state 처리 흐름**")
        st.json(technical["state_status"])
        st.json(technical["state_transitions"])
        if technical["graph_manifest"]:
            st.markdown(
                f"**실행 그래프 스냅샷 · schema v{technical['graph_schema_version']}**"
            )
            st.json(technical["graph_manifest"])
            if technical["node_runs"]:
                st.dataframe(
                    technical["node_runs"],
                    width="stretch",
                    hide_index=True,
                )
        st.caption("청크 본문은 저장하지 않고 안정적 ID·순위·점수만 표시합니다.")


def _render_global_chat_diagnostics(
    current_id: str,
    messages: list[dict],
    *,
    interactive_graph: bool = False,
) -> None:
    rows = monitoring.build_message_monitoring_rows(messages)
    if rows:
        state_labels = {
            "succeeded": "완료",
            "failed": "실패",
            "running": "처리 중",
            "unknown": "측정 전",
        }
        grounding_labels = {
            "linked": "근거 연결",
            "partial": "부분 연결",
            "unavailable": "근거 없음",
            "not_applicable": "해당 없음",
            "not_evaluated": "평가하지 않음",
            "not_measured": "측정 전",
        }
        if interactive_graph:
            table_rows = [
                {
                    "발생 시각": row.get("created_at"),
                    "상태": state_labels.get(
                        row.get("state_status"), row.get("state_status") or "측정 전"
                    ),
                    "검색 실행": _chat_execution_label(row, row.get("route")),
                    "총시간": _format_chat_duration(row.get("latency_seconds")),
                    "대상별 근거": _chat_target_coverage_label(
                        row, row.get("source_count")
                    ),
                    "인용 연결": grounding_labels.get(
                        row.get("grounding_status"),
                        row.get("grounding_status") or "측정 전",
                    ),
                    "질문": row.get("user_question_preview"),
                }
                for row in rows
            ]
        else:
            table_rows = [
                {
                    "발생 시각": row.get("created_at"),
                    "상태": row.get("status"),
                    "응답 시간": row.get("latency_seconds"),
                    "state": state_labels.get(
                        row.get("state_status"), row.get("state_status") or "측정 전"
                    ),
                    "근거 연결": grounding_labels.get(
                        row.get("grounding_status"),
                        row.get("grounding_status") or "측정 전",
                    ),
                    "k (설정/요청/fetch/context)": " / ".join(
                        "-" if value is None else str(value)
                        for value in (
                            row.get("configured_top_k"),
                            row.get("requested_k"),
                            row.get("fetch_k"),
                            row.get("context_count"),
                        )
                    ),
                    "사용 chunk": row.get("chunk_count"),
                    "사용 문서": row.get("document_count"),
                    "RDB 근거": row.get("rdb_evidence_count"),
                    "질문": row.get("user_question_preview"),
                }
                for row in rows
            ]
        st.dataframe(table_rows, width="stretch", hide_index=True)
    else:
        st.caption("확인할 assistant 응답이 없습니다.")
        return

    selectable_rows = [row for row in rows if row.get("message_id") is not None]
    if not selectable_rows:
        return
    label_by_id = {
        row["message_id"]: row.get("label", str(row["message_id"]))
        for row in selectable_rows
    }
    selected_message_id = st.selectbox(
        "원인을 확인할 응답",
        [row["message_id"] for row in selectable_rows],
        index=len(selectable_rows) - 1,
        format_func=lambda message_id: label_by_id.get(message_id, str(message_id)),
        key=f"chat_monitoring_selected_response_{current_id}",
    )
    selected_message = next(
        (
            message
            for message in messages
            if message.get("id") == selected_message_id
        ),
        None,
    )
    if not selected_message:
        return
    selected_user_question = monitoring.user_question_before_message(
        messages,
        selected_message_id,
    )
    previous_message = monitoring.previous_successful_assistant(
        messages,
        selected_message_id,
    )
    detail = monitoring.build_message_trace_detail(
        selected_message,
        user_question=selected_user_question,
    )
    diff = monitoring.build_response_diff(selected_message, previous_message)
    hints = monitoring.build_chat_trace_debug_hints(
        selected_message,
        previous_message,
        user_question=selected_user_question,
    )

    trace_summary = monitoring.build_message_trace_summary(
        detail,
        diff=diff,
        hints=hints,
    )
    if interactive_graph:
        _render_chat_monitoring_workspace(
            detail,
            trace_summary=trace_summary,
            diff=diff,
            hints=hints,
            current_id=current_id,
            selected_message_id=selected_message_id,
        )
        return
    st.markdown("#### 자동 확인 결과")
    if hints:
        for hint in hints:
            st.warning(hint)
    else:
        st.success("자동으로 감지된 흔한 검색·답변 문제는 없습니다.")

    with st.expander("요약과 이전 응답 비교", expanded=True):
        st.json(trace_summary)
        if diff:
            st.json(diff)
        else:
            st.caption("비교할 이전 성공 응답이 없습니다.")
    with st.expander("처리시간과 검색 k", expanded=True):
        st.markdown("**처리시간**")
        st.json(detail["timing"])
        st.markdown("**검색 k 단계값**")
        st.caption(
            "설정 상한, backend 요청, 실제 fetch, 최종 prompt context 수를 "
            "서로 다른 값으로 기록합니다."
        )
        st.json(detail["retrieval_k"])
    with st.expander("검색어와 검색 범위", expanded=False):
        st.json(detail["query_rewrite"])
        st.json(detail["scope"])
        st.json(detail["routing"])
    with st.expander("사용한 chunk와 문서", expanded=True):
        st.markdown("**문서**")
        if detail["used_documents"]:
            st.dataframe(
                detail["used_documents"],
                width="stretch",
                hide_index=True,
            )
        else:
            st.caption("이 turn에 기록된 사용 문서가 없습니다.")
        st.markdown("**prompt에 사용한 chunk**")
        if detail["used_chunks"]:
            st.dataframe(
                detail["used_chunks"],
                width="stretch",
                hide_index=True,
            )
        else:
            st.caption("이 turn에 기록된 사용 chunk가 없습니다.")
        if detail["rdb_evidence"]:
            st.markdown("**RDB 참고 문서**")
            st.dataframe(
                detail["rdb_evidence"],
                width="stretch",
                hide_index=True,
            )
        st.caption("청크 본문은 저장하지 않고 안정적 ID·순위·점수만 표시합니다.")
    with st.expander("참고 자료 선택과 출처 표시", expanded=False):
        st.json(detail["retrieval"])
        st.json(detail["answer"])
        st.json(detail["grounding"])
    with st.expander("state 처리 흐름", expanded=True):
        st.json(detail["state_status"])
        st.json(detail["state_transitions"])


def _format_chat_duration(seconds: float | None) -> str:
    if seconds is None:
        return "측정 전"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.2f}초"


def _format_chat_token_count(value: int | None) -> str:
    return f"{value:,}" if isinstance(value, int) else "측정 전"


def _format_chat_token_rate(value: float | None) -> str:
    if not isinstance(value, (int, float)):
        return "측정 전"
    return f"{float(value):.1f} tok/s"


def _render_chat_latency_table(messages: list[dict]) -> None:
    rows = monitoring.build_chat_latency_rows(messages)
    if not rows:
        st.caption("아직 완료된 Native V2 답변의 시간 표본이 없습니다.")
        return
    route_labels = {"rdb": "RDB", "vectordb": "Vector DB"}
    st.dataframe(
        [
            {
                "완료 시각": row.get("created_at"),
                "경로": route_labels.get(row.get("route"), row.get("route") or "-"),
                "전체 응답": _format_chat_duration(row.get("response_seconds")),
                "RDB 조회": _format_chat_duration(row.get("rdb_seconds")),
                "Vector DB 검색": _format_chat_duration(row.get("vector_seconds")),
            }
            for row in reversed(rows)
        ],
        width="stretch",
        hide_index=True,
    )


def render_chat_monitoring_page(current_id: str, current_thread: dict) -> None:
    """Render current-thread Native V2 timing and turn evidence."""
    st.header("개별 Chat Monitoring")
    st.caption(
        f"현재 대화: {current_thread['name']} · 응답별 실행 흐름과 저장된 모니터링 근거를 확인합니다."
    )
    messages = conversation_store.list_messages(current_id)
    st.markdown("#### 응답별 실행 그래프와 근거")
    st.caption(
        "응답을 선택하면 좌측에 저장된 실행 단계를 표시합니다. 기본 화면은 전체 지표이며, "
        "노드를 선택하면 우측이 해당 단계의 정보로 전환됩니다."
    )
    _render_global_chat_diagnostics(
        current_id,
        messages,
        interactive_graph=True,
    )

    with st.expander("대화 전체 속도 추이", expanded=False):
        summary = monitoring.summarize_chat_latency_metrics(messages)
        columns = st.columns(4)
        metric_specs = (
            (
                "최근 답변 총시간",
                summary.get("latest_response_seconds"),
                "가장 최근 성공 응답",
            ),
            (
                "현재 대화 평균",
                summary.get("avg_response_seconds"),
                f"표본 {int(summary.get('response_sample_count') or 0)}건",
            ),
            (
                "RDB 평균 조회시간",
                summary.get("avg_rdb_seconds"),
                f"표본 {int(summary.get('rdb_sample_count') or 0)}건",
            ),
            (
                "Vector DB 평균 검색시간",
                summary.get("avg_vector_seconds"),
                f"표본 {int(summary.get('vector_sample_count') or 0)}건",
            ),
        )
        for column, (label, value, caption) in zip(columns, metric_specs, strict=True):
            with column:
                st.metric(label, _format_chat_duration(value))
                st.caption(caption)
        _render_chat_latency_table(messages)


def _render_global_monitoring_area(
    problem_area: str,
    *,
    status: dict,
    thread_messages: list[dict],
    summary: dict,
    integrity: dict,
    accuracy: dict,
) -> None:
    if problem_area == "summary":
        _render_global_monitoring(summary, integrity, accuracy)
    elif problem_area == "response":
        if not thread_messages:
            st.caption("확인할 대화가 없습니다.")
            return
        thread_by_id = {
            str(entry["thread"]["id"]): entry for entry in thread_messages
        }
        selected_thread_id = st.selectbox(
            "확인할 대화",
            options=list(thread_by_id),
            format_func=lambda thread_id: thread_by_id[thread_id]["thread"][
                "name"
            ],
            key="monitoring_diagnostic_thread",
        )
        selected_entry = thread_by_id[selected_thread_id]
        _render_global_chat_diagnostics(
            selected_thread_id,
            selected_entry["messages"],
        )
    elif problem_area == "search_data":
        _render_v2_data_diagnostics(status)
    elif problem_area == "evaluation":
        _render_experiment_monitoring(status)
    elif problem_area == "parsing":
        _render_parsing_engine_evaluation()


def _resolve_global_monitoring_status(status: dict | None) -> dict:
    """Reuse a supplied Native status snapshot when available."""

    if not status:
        return status_cache.get_native_v2_data_status()
    return status


def render_global_monitoring_page(status: dict | None = None) -> None:
    """Render the V2-only speed/accuracy dashboard and problem tools."""
    st.header("로컬 응답 진단")
    st.caption(
        "Supabase 신고 작업함이 아니라 이 기기의 대화·데이터 상태를 봅니다. "
        "답변 속도와 품질을 확인하고 성능 개선 실험을 나누어 선택합니다."
    )

    status = _resolve_global_monitoring_status(status)
    thread_messages = _all_thread_messages()
    summary = monitoring.summarize_all_chat_threads(thread_messages)
    integrity = monitoring.summarize_v2_data_integrity(status)
    accuracy = monitoring.summarize_evaluation_accuracy(
        _latest_v2_accuracy_run()
    )
    _render_answer_metrics(summary, accuracy)

    failed_count = int(summary.get("statuses", {}).get("failed") or 0)
    accuracy_failure_count = max(
        int(accuracy.get("case_count") or 0)
        - int(accuracy.get("passed") or 0),
        0,
    )
    problem_count = (
        failed_count
        + accuracy_failure_count
        + int(integrity.get("warning_count") or 0)
        + int(integrity.get("fail_count") or 0)
    )
    st.divider()
    monitoring_group = st.segmented_control(
        "용도",
        options=list(_MONITORING_AREA_GROUPS),
        default="operations",
        format_func=_MONITORING_GROUP_LABELS.__getitem__,
        key="monitoring_area_group",
        label_visibility="collapsed",
        width="stretch",
    )
    if monitoring_group is None:
        return

    if monitoring_group == "operations":
        st.caption(f"운영 상태와 원인을 확인합니다 · 확인 필요 {problem_count}건")
        area_options = _MONITORING_AREA_GROUPS["operations"]
        _restore_monitoring_area_selection(
            "monitoring_operations_area",
            "monitoring_operations_area_selection",
            area_options,
        )
        problem_area = st.segmented_control(
            "화면",
            options=list(area_options),
            format_func=_PROBLEM_AREA_LABELS.__getitem__,
            key="monitoring_operations_area",
            on_change=_store_monitoring_area_selection,
            args=(
                "monitoring_operations_area",
                "monitoring_operations_area_selection",
            ),
            label_visibility="collapsed",
            width="stretch",
        )
    else:
        st.caption("변경 전후의 정확도와 문서 읽기 품질을 검증합니다.")
        area_options = _MONITORING_AREA_GROUPS["experiments"]
        _restore_monitoring_area_selection(
            "monitoring_experiments_area",
            "monitoring_experiments_area_selection",
            area_options,
        )
        problem_area = st.segmented_control(
            "화면",
            options=list(area_options),
            format_func=_PROBLEM_AREA_LABELS.__getitem__,
            key="monitoring_experiments_area",
            on_change=_store_monitoring_area_selection,
            args=(
                "monitoring_experiments_area",
                "monitoring_experiments_area_selection",
            ),
            label_visibility="collapsed",
            width="stretch",
        )
    if problem_area is None:
        return
    _render_global_monitoring_area(
        problem_area,
        status=status,
        thread_messages=thread_messages,
        summary=summary,
        integrity=integrity,
        accuracy=accuracy,
    )

