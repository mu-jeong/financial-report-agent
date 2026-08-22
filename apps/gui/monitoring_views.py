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
    return sections


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
        st.caption("청크 본문은 저장하지 않고 안정적 ID·순위·점수만 표시합니다.")


def _render_global_chat_diagnostics(
    current_id: str,
    messages: list[dict],
    *,
    performance_first: bool = False,
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
        if performance_first:
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
    if performance_first:
        _render_chat_answer_performance(
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
    st.header("답변 모니터링")
    st.caption(
        f"현재 대화: {current_thread['name']} · 선택한 답변의 속도·검색 실행·근거를 우선 표시합니다."
    )
    messages = conversation_store.list_messages(current_id)
    st.markdown("#### 답변별 성능과 근거")
    st.caption(
        "응답을 선택하면 총시간, 실제 검색 실행 방식, 대상별 검색 성공, "
        "답변에 연결된 문서를 바로 확인할 수 있습니다."
    )
    _render_global_chat_diagnostics(
        current_id,
        messages,
        performance_first=True,
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
    st.header("답변 모니터링")
    st.caption(
        "답변 속도와 정확도를 먼저 확인하고, 아래에서 운영 진단과 "
        "성능 개선 실험을 나누어 선택합니다."
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

