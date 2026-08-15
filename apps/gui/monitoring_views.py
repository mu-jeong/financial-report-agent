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
from src.core import expectation_suggester
from src.core import feedback_handoff
from src.core import issue_report_store
from src.core import monitoring
from src.core import status as status_module
from src.core.answer_requirements import (
    AnswerRequirementValidationError,
    MAX_ANSWER_REQUIREMENTS,
    canonicalize_answer_requirements,
)


MONITORING_EVAL_RUN_DIR = Path("debug") / "evaluation_runs"
MONITORING_REGRESSION_CANDIDATE_DIR = Path("debug") / "regression_candidates"
MONITORING_CANDIDATE_RUN_DIR = Path("debug") / "candidate_evaluation_runs"
MONITORING_CODEX_HANDOFF_DIR = Path("debug") / "codex_handoffs"

_EVALUATION_JOB_KEY = "native-v2-evaluation"
_REGRESSION_CANDIDATE_JOB_KEY = "native-v2-regression-candidates"

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


def _rerun_candidate_action(
    action,
    *,
    form_revision_key: str | None = None,
) -> None:
    try:
        action()
    except monitoring.CandidateConflictError:
        if form_revision_key:
            st.session_state.pop(form_revision_key, None)
        st.error(
            "다른 변경이 먼저 저장되었습니다. 현재 내용을 덮어쓰지 않았습니다. "
            "화면을 다시 불러온 뒤 변경 내용을 확인해 주세요."
        )
    except (
        monitoring.CandidateLoadError,
        monitoring.CandidateTransitionError,
        monitoring.CandidateValidationError,
        feedback_handoff.FeedbackHandoffError,
        issue_report_store.IssueReportLoadError,
        issue_report_store.IssueReportWriteError,
        RuntimeError,
        OSError,
    ) as exc:
        st.error(str(exc))
    else:
        if form_revision_key:
            st.session_state.pop(form_revision_key, None)
        st.rerun()


def _write_and_record_candidate_handoff(
    candidate: dict,
    baseline_run: dict,
    approval_reason: str,
) -> dict:
    written = feedback_handoff.write_codex_handoff(
        candidate,
        baseline_run,
        output_dir=MONITORING_CODEX_HANDOFF_DIR,
        approved_by="local_operator",
        approval_reason=approval_reason,
    )
    return monitoring.record_candidate_handoff(
        candidate["json_path"],
        handoff=written,
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
    )


def _load_candidate_source_report(candidate: dict) -> dict | None:
    """Load the report only to give the LLM the selected answer and comment."""

    for key in ("source_json_path", "source_file_path"):
        source_path = candidate.get(key)
        if not source_path:
            continue
        try:
            return issue_report_store.load_report(source_path)
        except (
            issue_report_store.IssueReportLoadError,
            OSError,
        ):
            continue
    return None


def _parse_condition_terms(value: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for part in value.replace("\n", ",").split(","):
        term = part.strip()
        normalized = term.casefold()
        if term and normalized not in seen:
            seen.add(normalized)
            terms.append(term)
    return terms


def _manual_assertions_from_text(value: str) -> list[dict[str, str]]:
    return [
        {
            "id": f"manual_assertion_{index}",
            "text": line.strip(),
        }
        for index, line in enumerate(value.splitlines(), 1)
        if line.strip()
    ]


def _render_candidate_lifecycle(candidate: dict) -> None:
    path = candidate.get("json_path")
    if not path:
        st.error("후보 파일 경로가 없어 상태를 변경할 수 없습니다.")
        return
    actions = monitoring.build_candidate_action_state(candidate)
    status = str(candidate.get("triage_status") or "new")
    status_labels = {
        "new": "신규",
        "triaged": "분류 완료",
        "needs_expectation": "기대 결과 작성 중",
        "ready": "수정 전 재현 준비",
        "reproduced": "오류 재현 완료",
        "fixing": "수정 중",
        "verified": "수정 후 검증 완료",
        "closed": "종료",
        "duplicate": "중복",
        "rejected": "처리 제외",
        "not_reproducible": "재현되지 않음",
    }
    st.markdown("#### 선택 후보 처리")
    st.caption(
        f"상태: {status_labels.get(status, status)} · "
        f"저장 개정 {candidate.get('record_revision', 0)} · "
        f"검증 계약 {candidate.get('contract_revision', 0)}"
    )
    readiness = monitoring.assess_candidate_reproduction_readiness(
        candidate
    )
    st.caption(
        f"품질 프로파일: {candidate.get('quality_profile') or 'unavailable'} · "
        f"검증 방식: {candidate.get('verification_type')} · "
        f"재현 정보: {'준비됨' if readiness['ready'] else '보강 필요'}"
    )
    if not readiness["ready"]:
        st.warning(readiness["reason"])
    with st.expander("검증 계획과 재현 매니페스트", expanded=False):
        st.json(
            {
                "validation_plan": candidate.get("validation_plan"),
                "reproduction_manifest": candidate.get(
                    "reproduction_manifest"
                ),
            }
        )
        st.caption(
            "검증 계획은 판정 기준이고 재현 매니페스트는 Native V2 환경 지문입니다."
        )
    form_revision_key = (
        f"feedback_candidate_form_revision_{candidate['id']}_{status}"
    )
    if status in {"new", "triaged", "needs_expectation", "verified"}:
        if form_revision_key not in st.session_state:
            st.session_state[form_revision_key] = candidate["record_revision"]
        form_record_revision = int(st.session_state[form_revision_key])
    else:
        form_record_revision = int(candidate["record_revision"])

    with st.expander("관찰 결과와 현재 기대 결과", expanded=False):
        observed_column, expected_column = st.columns(2)
        with observed_column:
            st.markdown("##### 관찰 결과")
            st.json(candidate.get("observed") or {})
        with expected_column:
            st.markdown("##### 현재 기대 결과(읽기 전용)")
            approved_at = candidate.get("expected_approved_at")
            approved_by = candidate.get("expected_approved_by")
            if approved_at:
                st.success("승인 여부: 승인됨")
                st.caption(
                    f"승인자: {approved_by or '기록 없음'} · "
                    f"승인 시각: {approved_at}"
                )
            else:
                st.warning("승인 여부: 미승인")
            st.json(candidate.get("expected") or {})

    run_recovery = monitoring.discover_candidate_orphan_runs(
        candidate,
        run_dir=MONITORING_CANDIDATE_RUN_DIR,
    )
    if run_recovery["warnings"]:
        st.warning("손상되어 연결할 수 없는 후보 실행 파일이 있습니다.")
        st.dataframe(
            [
                {
                    "경고": warning.get("code"),
                    "파일": Path(str(warning.get("path") or "")).name,
                }
                for warning in run_recovery["warnings"]
            ],
            width="stretch",
            hide_index=True,
        )
    for run in run_recovery["attachable"]:
        required_status = (
            "ready" if run.get("run_kind") == "baseline" else "fixing"
        )
        if st.button(
            f"미연결 실행 결과 연결: {run.get('run_id')}",
            key=f"candidate_attach_run_{run['run_id']}",
            width="stretch",
            disabled=status != required_status,
            help=(
                "현재 후보 상태와 실행 종류가 맞을 때만 연결할 수 있습니다."
            ),
        ):
            _rerun_candidate_action(
                lambda run=run: monitoring.record_candidate_run(
                    path,
                    run=run,
                    run_kind=str(run["run_kind"]),
                    expected_record_revision=candidate["record_revision"],
                    expected_contract_revision=candidate["contract_revision"],
                    expected_candidate_hash=candidate["candidate_hash"],
                )
            )
    if run_recovery["stale"]:
        st.info(
            f"현재 검증 계약과 맞지 않는 과거 실행 "
            f"{len(run_recovery['stale'])}건은 연결 대상에서 제외했습니다."
        )
    attempt_count = len(run_recovery["failed_attempts"]) + len(
        run_recovery["blocked_attempts"]
    )
    if attempt_count:
        st.info(
            f"실패 또는 자료 미준비로 끝난 실행 시도 {attempt_count}건은 "
            "검증 증거로 사용하지 않습니다."
        )

    handoff_recovery = feedback_handoff.discover_candidate_orphan_handoffs(
        candidate,
        output_dir=MONITORING_CODEX_HANDOFF_DIR,
    )
    handoff_artifacts = feedback_handoff.list_codex_handoff_artifacts(
        MONITORING_CODEX_HANDOFF_DIR,
        candidate_id=str(candidate["id"]),
    )
    if handoff_artifacts["warnings"]:
        st.warning("일부 Codex 전달물은 누락되었거나 손상되었습니다.")
        st.dataframe(
            [
                {
                    "경고": warning.get("code"),
                    "파일": Path(str(warning.get("path") or "")).name,
                    "연결 차단": bool(warning.get("blocking")),
                }
                for warning in handoff_artifacts["warnings"]
            ],
            width="stretch",
            hide_index=True,
        )
    for item in handoff_artifacts["items"]:
        if item.get("companion_status") != "missing":
            continue
        if st.button(
            f"전달 문서 재생성: {item.get('handoff_id')}",
            key=f"candidate_repair_handoff_{item['handoff_id']}",
            width="stretch",
        ):
            _rerun_candidate_action(
                lambda item=item: feedback_handoff.repair_codex_handoff_markdown(
                    item["manifest_path"],
                    output_root=MONITORING_CODEX_HANDOFF_DIR,
                )
            )
    for item in handoff_recovery["attachable"]:
        if st.button(
            f"미연결 전달물 연결: {item.get('handoff_id')}",
            key=f"candidate_attach_handoff_{item['handoff_id']}",
            width="stretch",
            disabled=status not in {"reproduced", "fixing"},
        ):
            _rerun_candidate_action(
                lambda item=item: monitoring.record_candidate_handoff(
                    path,
                    handoff=item,
                    expected_record_revision=candidate["record_revision"],
                    expected_contract_revision=candidate[
                        "contract_revision"
                    ],
                    expected_candidate_hash=candidate["candidate_hash"],
                )
            )
    if handoff_recovery["stale"]:
        st.info(
            f"현재 후보 내용과 맞지 않는 과거 전달물 "
            f"{len(handoff_recovery['stale'])}건은 연결 대상에서 제외했습니다."
        )

    if candidate.get("handoffs"):
        st.caption(f"연결된 Codex 전달물: {len(candidate['handoffs'])}건")

    if actions["create_handoff"]["enabled"]:
        baseline_runs: list[dict] = []
        for reference in reversed(
            (candidate.get("evidence") or {}).get("baseline_runs") or []
        ):
            artifact_path = reference.get("artifact_path")
            if not artifact_path:
                continue
            try:
                baseline_run = monitoring.load_evaluation_run(artifact_path)
            except monitoring.CandidateLoadError:
                continue
            if (
                reference.get("status") == "fail"
                and monitoring.is_current_candidate_contract(
                    candidate,
                    baseline_run,
                )
            ):
                baseline_runs.append(baseline_run)

        if not baseline_runs:
            st.warning(
                "현재 검증 계약에 속한 수정 전 실패 파일을 다시 읽을 수 없어 "
                "Codex 전달물을 만들 수 없습니다."
            )
        else:
            baseline_ids = [str(run["run_id"]) for run in baseline_runs]
            selected_baseline_id = st.selectbox(
                "전달물에 사용할 수정 전 실패",
                options=baseline_ids,
                key=f"candidate_handoff_baseline_{candidate['id']}",
            )
            selected_baseline = next(
                run
                for run in baseline_runs
                if str(run["run_id"]) == selected_baseline_id
            )
            try:
                preview_payload = feedback_handoff.build_codex_handoff_payload(
                    candidate,
                    selected_baseline,
                )
                preview_markdown = (
                    feedback_handoff.render_codex_handoff_markdown(
                        preview_payload
                    )
                )
            except feedback_handoff.FeedbackHandoffError as exc:
                st.error(f"전달물 미리보기를 만들 수 없습니다: {exc}")
            else:
                with st.expander(
                    "민감정보 제거 후 Codex 전달물 미리보기",
                    expanded=False,
                ):
                    st.code(preview_markdown, language="markdown")
                approval_reason = st.text_input(
                    "전달물 승인 사유",
                    key=f"candidate_handoff_reason_{candidate['id']}",
                )
                handoff_confirmed = st.checkbox(
                    "민감정보 제거 결과와 전달 내용을 확인했습니다.",
                    key=f"candidate_handoff_confirm_{candidate['id']}",
                )
                if st.button(
                    "Codex 전달물 저장 및 후보에 연결",
                    key=f"candidate_handoff_save_{candidate['id']}",
                    width="stretch",
                    disabled=(
                        not handoff_confirmed or not approval_reason.strip()
                    ),
                ):
                    _rerun_candidate_action(
                        lambda: _write_and_record_candidate_handoff(
                            candidate,
                            selected_baseline,
                            approval_reason,
                        )
                    )

    if status in {"ready", "reproduced", "fixing", "verified"}:
        edit_contract_reason = st.text_input(
            "기대 결과를 다시 편집하는 사유",
            key=f"candidate_edit_contract_reason_{candidate['id']}",
        )
        if st.button(
            "기대 결과 다시 편집",
            key=f"candidate_edit_contract_{candidate['id']}",
            width="stretch",
            disabled=not edit_contract_reason.strip(),
            help=(
                "승인을 취소하고 검증 계약 개정을 올립니다. "
                "기존 실행 결과와 전달물은 과거 이력으로 남습니다."
            ),
        ):
            _rerun_candidate_action(
                lambda: monitoring.revoke_candidate_expectation(
                    path,
                    expected_record_revision=candidate["record_revision"],
                    reason=edit_contract_reason,
                )
            )

    if status == "new":
        impact_options = [
            "routing",
            "filter_scope",
            "retrieval_source",
            "citation",
            "latency",
            "ui",
            "answer_quality",
        ]
        severity_options = ["S1", "S2", "S3", "S4"]
        decision_options = [
            "accepted",
            "needs_info",
            "rejected",
        ]
        current_impact = candidate.get("impact_area")
        with st.form(f"candidate_triage_{candidate['id']}"):
            severity = st.selectbox(
                "심각도",
                severity_options,
                index=(
                    severity_options.index(candidate.get("severity"))
                    if candidate.get("severity") in severity_options
                    else 2
                ),
            )
            impact_area = st.selectbox(
                "영향 영역",
                impact_options,
                index=(
                    impact_options.index(current_impact)
                    if current_impact in impact_options
                    else impact_options.index("answer_quality")
                ),
            )
            impact_summary = st.text_area(
                "사용자 영향 요약",
                value=str(candidate.get("impact_summary") or ""),
            )
            decision = st.selectbox(
                "처리 결정",
                decision_options,
                index=(
                    decision_options.index(candidate.get("operator_decision"))
                    if candidate.get("operator_decision") in decision_options
                    else 0
                ),
            )
            save_triage = st.form_submit_button(
                "분류 저장",
                width="stretch",
            )
        if save_triage:
            _rerun_candidate_action(
                lambda: monitoring.update_regression_candidate(
                    path,
                    expected_record_revision=form_record_revision,
                    changes={
                        "severity": severity,
                        "impact_area": impact_area,
                        "impact_summary": impact_summary,
                        "operator_decision": decision,
                    },
                    reason="모니터링 화면에서 분류 저장",
                ),
                form_revision_key=form_revision_key,
            )
        if st.button(
            "분류 완료",
            key=f"candidate_mark_triaged_{candidate['id']}",
            width="stretch",
            disabled=not actions["mark_triaged"]["enabled"],
            help=actions["mark_triaged"]["reason"],
        ):
            _rerun_candidate_action(
                lambda: monitoring.transition_regression_candidate(
                    path,
                    to_status="triaged",
                    expected_record_revision=candidate["record_revision"],
                    reason="운영자 분류 완료",
                )
            )

    elif status == "triaged":
        decision = candidate.get("operator_decision")
        if decision == "needs_info":
            impact_options = [
                "routing",
                "filter_scope",
                "retrieval_source",
                "citation",
                "latency",
                "ui",
                "answer_quality",
            ]
            severity_options = ["S1", "S2", "S3", "S4"]
            decision_options = [
                "accepted",
                "needs_info",
                "rejected",
            ]
            current_impact = candidate.get("impact_area")
            current_decision = candidate.get("operator_decision")
            with st.form(f"candidate_triage_followup_{candidate['id']}"):
                severity = st.selectbox(
                    "심각도",
                    severity_options,
                    index=(
                        severity_options.index(candidate.get("severity"))
                        if candidate.get("severity") in severity_options
                        else 2
                    ),
                )
                impact_area = st.selectbox(
                    "영향 영역",
                    impact_options,
                    index=(
                        impact_options.index(current_impact)
                        if current_impact in impact_options
                        else impact_options.index("answer_quality")
                    ),
                )
                impact_summary = st.text_area(
                    "사용자 영향 요약",
                    value=str(candidate.get("impact_summary") or ""),
                )
                revised_decision = st.selectbox(
                    "추가 정보 반영 후 처리 결정",
                    decision_options,
                    index=(
                        decision_options.index(current_decision)
                        if current_decision in decision_options
                        else decision_options.index("needs_info")
                    ),
                )
                save_followup = st.form_submit_button(
                    "추가 정보 반영",
                    width="stretch",
                )
            if save_followup:
                _rerun_candidate_action(
                    lambda: monitoring.update_regression_candidate(
                        path,
                        expected_record_revision=form_record_revision,
                        changes={
                            "severity": severity,
                            "impact_area": impact_area,
                            "impact_summary": impact_summary,
                            "operator_decision": revised_decision,
                        },
                        reason="추가 정보 반영 후 후보 재분류",
                    ),
                    form_revision_key=form_revision_key,
                )
            st.info(
                "추가 정보를 반영해 처리 결정을 변경할 수 있습니다."
            )
            return
        if decision == "accepted":
            if st.button(
                "기대 결과 작성으로 이동",
                key=f"candidate_needs_expectation_{candidate['id']}",
                width="stretch",
                disabled=not actions["request_expectation"]["enabled"],
                help=actions["request_expectation"]["reason"],
            ):
                _rerun_candidate_action(
                    lambda: monitoring.transition_regression_candidate(
                        path,
                        to_status="needs_expectation",
                        expected_record_revision=candidate["record_revision"],
                        reason="기대 결과 작성 시작",
                    )
                )
        elif decision == "rejected":
            rejection_reason = st.text_input(
                "처리 제외 사유",
                key=f"candidate_rejection_reason_{candidate['id']}",
            )
            if st.button(
                "처리 제외 확정",
                key=f"candidate_reject_{candidate['id']}",
                width="stretch",
                disabled=not rejection_reason.strip(),
            ):
                _rerun_candidate_action(
                    lambda: monitoring.transition_regression_candidate(
                        path,
                        to_status="rejected",
                        expected_record_revision=candidate["record_revision"],
                        reason=rejection_reason,
                    )
                )
        else:
            st.info("추가 정보가 확보되면 분류 내용을 갱신해 주세요.")

    elif status == "needs_expectation":
        expected = candidate.get("expected") or {}
        observed = candidate.get("observed") or {}
        reproduction_input = observed.get("reproduction_input") or {}
        actual = observed.get("actual") or {}
        current_plan = candidate.get("validation_plan") or {}
        current_profile = (
            candidate.get("quality_profile") or "accuracy_first"
        )
        question = str(reproduction_input.get("question") or "").strip()
        scenario = str(reproduction_input.get("scenario") or "").strip()
        supports_answer_requirements = bool(question)
        st.markdown("##### 재현 대상")
        st.write(question or scenario or "재현 질문 또는 시나리오가 없습니다.")
        chat_history = reproduction_input.get("chat_history") or []
        if chat_history:
            st.caption(f"이전 대화 {len(chat_history)}개 turn도 자동으로 사용합니다.")

        suggestion_key = (
            f"candidate_expectation_suggestion_{candidate['id']}_"
            f"{candidate['record_revision']}"
        )
        draft_version_key = f"{suggestion_key}_version"
        count_key = f"{suggestion_key}_count"
        if st.button(
            "LLM으로 최소 조건 제안",
            key=f"candidate_suggest_expectation_{candidate['id']}",
            width="stretch",
            disabled=not supports_answer_requirements,
            help=(
                "신고된 질문·답변·검색 메타데이터를 기존 생성 모델로 분석합니다. "
                "제안은 저장되거나 승인되지 않습니다."
            ),
        ):
            with st.spinner("현재 답변에서 빠진 최소 조건을 찾는 중입니다..."):
                try:
                    suggestion = (
                        expectation_suggester.suggest_minimum_expectation(
                            candidate,
                            source_report=_load_candidate_source_report(
                                candidate
                            ),
                        )
                    )
                except expectation_suggester.ExpectationSuggestionError as exc:
                    st.error(str(exc))
                else:
                    st.session_state[suggestion_key] = suggestion
                    st.session_state[draft_version_key] = (
                        int(st.session_state.get(draft_version_key, 0)) + 1
                    )
                    st.session_state[count_key] = len(
                        suggestion["requirements"]
                    )
                    st.rerun()
        st.caption(
            "버튼을 누를 때 선택 turn의 질문·답변·신고 사유와 제한된 출처 "
            "메타데이터가 현재 설정된 LLM으로 전송됩니다. LLM은 초안만 만들며 "
            "저장과 승인은 운영자가 각각 수행합니다."
        )
        if not supports_answer_requirements:
            st.info(
                "답변이 없는 화면·동작 신고입니다. 고급 설정에 수동 확인 "
                "조건을 한 줄씩 입력해 주세요."
            )

        suggestion = st.session_state.get(suggestion_key) or {}
        if suggestion.get("summary"):
            st.success(f"제안 요약: {suggestion['summary']}")
        initial_requirements = (
            expected.get("answer_requirements")
            or suggestion.get("requirements")
            or []
        )
        if supports_answer_requirements:
            if count_key in st.session_state:
                condition_count = int(
                    st.number_input(
                        "최소 답변 조건 수",
                        min_value=1,
                        max_value=MAX_ANSWER_REQUIREMENTS,
                        step=1,
                        key=count_key,
                    )
                )
            else:
                condition_count = int(
                    st.number_input(
                        "최소 답변 조건 수",
                        min_value=1,
                        max_value=MAX_ANSWER_REQUIREMENTS,
                        value=min(
                            MAX_ANSWER_REQUIREMENTS,
                            max(1, len(initial_requirements)),
                        ),
                        step=1,
                        key=count_key,
                    )
                )
        else:
            condition_count = 0
        draft_version = int(st.session_state.get(draft_version_key, 0))
        edited_requirements: list[dict] = []
        form_key = (
            f"candidate_contract_{candidate['id']}_{draft_version}"
        )
        with st.form(form_key):
            st.markdown("##### 최소 답변 조건")
            st.caption(
                "모든 조건을 만족해야 통과합니다. 표현은 쉼표로 구분하며, "
                "같은 대상의 정식명과 별칭을 함께 적을 수 있습니다."
            )
            for index in range(condition_count):
                requirement = (
                    initial_requirements[index]
                    if index < len(initial_requirements)
                    else {}
                )
                with st.expander(
                    f"조건 {index + 1}",
                    expanded=index == 0,
                ):
                    description = st.text_input(
                        "통과 조건",
                        value=str(requirement.get("description") or ""),
                        key=(
                            f"candidate_requirement_description_"
                            f"{candidate['id']}_{draft_version}_{index}"
                        ),
                        placeholder=(
                            "예: SK하이닉스 리포트 내용을 근거와 함께 다룬다."
                        ),
                    )
                    answer_terms = st.text_input(
                        "답변에서 확인할 표현",
                        value=", ".join(
                            requirement.get("answer_terms_any") or []
                        ),
                        key=(
                            f"candidate_requirement_answer_terms_"
                            f"{candidate['id']}_{draft_version}_{index}"
                        ),
                        placeholder="SK하이닉스, 하이닉스",
                    )
                    source_terms = st.text_input(
                        "근거 문서에서 확인할 대상",
                        value=", ".join(
                            requirement.get("source_terms_any") or []
                        ),
                        key=(
                            f"candidate_requirement_source_terms_"
                            f"{candidate['id']}_{draft_version}_{index}"
                        ),
                        placeholder="SK하이닉스, 하이닉스",
                    )
                    require_citation = st.checkbox(
                        "일치하는 근거 문서를 인용해야 통과",
                        value=bool(
                            requirement.get("require_citation", True)
                        ),
                        key=(
                            f"candidate_requirement_citation_"
                            f"{candidate['id']}_{draft_version}_{index}"
                        ),
                    )
                    edited_requirements.append(
                        {
                            "id": str(
                                requirement.get("id")
                                or f"answer_requirement_{index + 1}"
                            ),
                            "description": description,
                            "answer_terms_any": _parse_condition_terms(
                                answer_terms
                            ),
                            "source_terms_any": _parse_condition_terms(
                                source_terms
                            ),
                            "require_citation": require_citation,
                        }
                    )

            current_manual_assertions = expected.get(
                "manual_assertions"
            ) or []
            with st.expander("고급 설정", expanded=False):
                st.caption(
                    "품질 프로파일, 성능 예산, 재현 입력과 환경 지문은 기존 "
                    "후보 설정에서 자동으로 유지합니다."
                )
                manual_assertions_text = st.text_area(
                    "수동 확인 조건(선택, 한 줄에 하나)",
                    value="\n".join(
                        str(item.get("text") or "")
                        for item in current_manual_assertions
                    ),
                    key=(
                        f"candidate_manual_assertions_text_"
                        f"{candidate['id']}_{draft_version}"
                    ),
                )
                st.caption(
                    f"자동 유지: 품질 프로파일 {current_profile} · "
                    f"재현 매니페스트 "
                    f"{str((candidate.get('reproduction_manifest') or {}).get('manifest_hash') or '')[:12] or '미준비'}"
                )
            save_contract = st.form_submit_button(
                "최소 조건 저장",
                width="stretch",
            )
        if save_contract:
            populated_requirements = [
                requirement
                for requirement in edited_requirements
                if (
                    requirement["description"].strip()
                    or requirement["answer_terms_any"]
                    or requirement["source_terms_any"]
                )
            ]
            try:
                answer_requirements = canonicalize_answer_requirements(
                    populated_requirements
                )
            except AnswerRequirementValidationError as exc:
                st.error(f"최소 답변 조건을 확인해 주세요: {exc}")
            else:
                manual_assertions = _manual_assertions_from_text(
                    manual_assertions_text
                )
                prerequisite_values = {
                    "route_pass": expected.get("route"),
                    "filter_pass": expected.get("filters"),
                    "source_hit": expected.get("sources"),
                    "expected_state_pass": expected.get("state"),
                }
                preserved_checks = [
                    check
                    for check in (
                        current_plan.get("hard_checks")
                        or candidate.get("active_checks")
                        or []
                    )
                    if check
                    not in {
                        "answer_requirements_pass",
                        "manual_assertions_pass",
                        "performance_p95_pass",
                    }
                    and (
                        check not in prerequisite_values
                        or prerequisite_values[check]
                    )
                ]
                hard_checks = (
                    ["answer_requirements_pass"]
                    if answer_requirements
                    else []
                )
                hard_checks.extend(
                    check
                    for check in preserved_checks
                    if check not in hard_checks
                )
                if manual_assertions:
                    hard_checks.append("manual_assertions_pass")
                current_budget = dict(
                    current_plan.get("performance_budget")
                    or monitoring.QUALITY_PROFILE_RULES[current_profile][
                        "default_performance_budget"
                    ]
                )
                automatic_checks = [
                    check
                    for check in hard_checks
                    if check != "manual_assertions_pass"
                ]
                if (
                    current_budget.get("enforcement") == "hard"
                    and automatic_checks
                ):
                    hard_checks.append("performance_p95_pass")
                elif current_budget.get("enforcement") == "hard":
                    current_budget["enforcement"] = "soft"
                if not hard_checks:
                    st.error(
                        "최소 답변 조건 또는 수동 확인 조건을 하나 이상 입력해 주세요."
                    )
                    return
                soft_objectives = list(
                    current_plan.get("soft_objectives")
                    or monitoring.QUALITY_PROFILE_RULES[current_profile][
                        "default_soft_objectives"
                    ]
                )
                validation_plan = {
                    "schema_version": 1,
                    "quality_profile": current_profile,
                    "hard_checks": hard_checks,
                    "soft_objectives": soft_objectives,
                    "performance_budget": current_budget,
                }
                _rerun_candidate_action(
                    lambda: monitoring.update_regression_candidate(
                        path,
                        expected_record_revision=form_record_revision,
                        changes={
                            "expected": {
                                "route": (
                                    expected.get("route")
                                    or actual.get("route")
                                    if automatic_checks
                                    else None
                                ),
                                "filters": expected.get("filters") or {},
                                "sources": expected.get("sources") or [],
                                "state": expected.get("state") or {},
                                "manual_assertions": manual_assertions,
                                "answer_requirements": answer_requirements,
                            },
                            "validation_plan": validation_plan,
                            "verification_type": (
                                "manual_ui"
                                if manual_assertions
                                and not automatic_checks
                                else "mixed"
                                if manual_assertions
                                else "graph_contract"
                            ),
                        },
                        reason="LLM 보조 최소 기대 조건 저장",
                    ),
                    form_revision_key=form_revision_key,
                )
        if st.button(
            "기대 결과 승인",
            key=f"candidate_approve_{candidate['id']}",
            width="stretch",
            disabled=not actions["approve_expectation"]["enabled"],
            help=actions["approve_expectation"]["reason"],
        ):
            _rerun_candidate_action(
                lambda: monitoring.approve_candidate_expectation(
                    path,
                    expected_record_revision=form_record_revision,
                    reason="모니터링 화면에서 기대 결과 검토 완료",
                ),
                form_revision_key=form_revision_key,
            )
        if st.button(
            "수정 전 재현 준비 완료",
            key=f"candidate_ready_{candidate['id']}",
            width="stretch",
            disabled=not actions["mark_ready"]["enabled"],
            help=actions["mark_ready"]["reason"],
        ):
            _rerun_candidate_action(
                lambda: monitoring.transition_regression_candidate(
                    path,
                    to_status="ready",
                    expected_record_revision=form_record_revision,
                    reason="기대 결과와 검사 승인 완료",
                ),
                form_revision_key=form_revision_key,
            )

    elif status in {"ready", "fixing"}:
        automatic_action_key = (
            "run_baseline"
            if status == "ready"
            else "run_verification"
        )
        manual_action_key = (
            "record_manual_reproduction"
            if status == "ready"
            else "record_manual_verification"
        )
        if actions[automatic_action_key]["enabled"]:
            st.info(
                "자동 재현·검증은 Native V2 revision을 고정한 실행 결과만 "
                "증거로 사용합니다. 준비된 실행 결과가 있으면 이 화면의 "
                "미연결 실행 목록에서 후보에 연결하세요."
            )
        if actions[manual_action_key]["enabled"]:
            assertions = (
                candidate.get("expected") or {}
            ).get("manual_assertions") or []
            st.markdown("##### 승인된 수동 검사")
            checklist_results: list[dict] = []
            for assertion in assertions:
                assertion_id = str(assertion.get("id") or "")
                passed = st.checkbox(
                    str(assertion.get("text") or assertion_id),
                    value=status == "fixing",
                    key=(
                        f"candidate_manual_check_{status}_"
                        f"{candidate['id']}_{assertion_id}"
                    ),
                )
                checklist_results.append(
                    {
                        "assertion_id": assertion_id,
                        "passed": passed,
                        "note": "",
                    }
                )
            manual_note = st.text_area(
                "수동 검사 메모",
                key=f"candidate_manual_note_{status}_{candidate['id']}",
            )
            manual_reason = st.text_input(
                "수동 검사 승인 사유",
                key=f"candidate_manual_reason_{status}_{candidate['id']}",
            )
            manual_confirmed = st.checkbox(
                "각 검사 결과를 직접 확인했습니다.",
                key=f"candidate_manual_confirm_{status}_{candidate['id']}",
            )
            for result in checklist_results:
                result["note"] = manual_note
            evidence_kind = (
                "manual_reproduction"
                if status == "ready"
                else "manual_verification"
            )
            if st.button(
                "수동 검사 결과 저장",
                key=f"candidate_record_manual_{status}_{candidate['id']}",
                width="stretch",
                disabled=(
                    not actions[manual_action_key]["enabled"]
                    or not manual_confirmed
                    or not manual_reason.strip()
                ),
            ):
                _rerun_candidate_action(
                    lambda: monitoring.record_candidate_manual_evidence(
                        path,
                        evidence_kind=evidence_kind,
                        checklist_results=checklist_results,
                        expected_record_revision=candidate[
                            "record_revision"
                        ],
                        expected_contract_revision=candidate[
                            "contract_revision"
                        ],
                        expected_candidate_hash=candidate[
                            "candidate_hash"
                        ],
                        reason=manual_reason,
                    )
                )

        if status == "ready":
            if st.button(
                "오류 재현 확정",
                key=f"candidate_mark_reproduced_{candidate['id']}",
                width="stretch",
                disabled=not actions["mark_reproduced"]["enabled"],
                help=actions["mark_reproduced"]["reason"],
            ):
                _rerun_candidate_action(
                    lambda: monitoring.transition_regression_candidate(
                        path,
                        to_status="reproduced",
                        expected_record_revision=candidate[
                            "record_revision"
                        ],
                        reason="현재 검증 계약에서 수정 전 오류를 재현함",
                    )
                )
            not_reproduced_reason = st.text_input(
                "재현되지 않음 처리 사유",
                key=f"candidate_not_reproduced_reason_{candidate['id']}",
            )
            if st.button(
                "재현되지 않음으로 종료",
                key=f"candidate_not_reproducible_{candidate['id']}",
                width="stretch",
                disabled=(
                    not actions["mark_not_reproducible"]["enabled"]
                    or not not_reproduced_reason.strip()
                ),
                help=actions["mark_not_reproducible"]["reason"],
            ):
                _rerun_candidate_action(
                    lambda: monitoring.transition_regression_candidate(
                        path,
                        to_status="not_reproducible",
                        expected_record_revision=candidate[
                            "record_revision"
                        ],
                        reason=not_reproduced_reason,
                    )
                )
        elif st.button(
            "수정 후 검증 완료",
            key=f"candidate_mark_verified_{candidate['id']}",
            width="stretch",
            disabled=not actions["mark_verified"]["enabled"],
            help=actions["mark_verified"]["reason"],
        ):
            _rerun_candidate_action(
                lambda: monitoring.transition_regression_candidate(
                    path,
                    to_status="verified",
                    expected_record_revision=candidate["record_revision"],
                    reason="현재 검증 계약의 수정 후 검사가 통과함",
                )
            )

    elif status == "reproduced":
        st.success("수정 전 오류 재현 증거가 연결되었습니다.")
        if st.button(
            "수정 시작",
            key=f"candidate_fixing_{candidate['id']}",
            width="stretch",
        ):
            _rerun_candidate_action(
                lambda: monitoring.transition_regression_candidate(
                    path,
                    to_status="fixing",
                    expected_record_revision=candidate["record_revision"],
                    reason="운영자가 수정 작업을 시작함",
                )
            )

    elif status == "verified":
        with st.form(f"candidate_close_{candidate['id']}"):
            fixed_version = st.text_input(
                "수정 버전",
                value=str(candidate.get("fixed_in_version") or ""),
            )
            closure_reason = st.text_area(
                "종료 사유",
                value=str(candidate.get("closure_reason") or ""),
            )
            suite_case_id = st.text_input(
                "장기 회귀 사례 ID",
                value=str(candidate.get("suite_case_id") or ""),
            )
            exclusion_reason = st.selectbox(
                "편입 제외 사유",
                ["", "sensitive_input", "manual_only", "snapshot_unavailable"],
                index=0,
            )
            save_closure = st.form_submit_button(
                "종료 근거 저장",
                width="stretch",
            )
        if save_closure:
            _rerun_candidate_action(
                lambda: monitoring.update_regression_candidate(
                    path,
                    expected_record_revision=form_record_revision,
                    changes={
                        "fixed_in_version": fixed_version,
                        "closure_reason": closure_reason,
                        "suite_case_id": suite_case_id or None,
                        "suite_exclusion_reason": exclusion_reason or None,
                    },
                    reason="모니터링 화면에서 종료 근거 저장",
                ),
                form_revision_key=form_revision_key,
            )
        if st.button(
            "후보 종료",
            key=f"candidate_close_action_{candidate['id']}",
            width="stretch",
            disabled=not actions["close"]["enabled"],
            help=actions["close"]["reason"],
        ):
            _rerun_candidate_action(
                lambda: monitoring.transition_regression_candidate(
                    path,
                    to_status="closed",
                    expected_record_revision=form_record_revision,
                    reason="검증과 회귀 편입 판단 완료",
                ),
                form_revision_key=form_revision_key,
            )

    elif status == "closed":
        reopen_reason = st.text_input(
            "다시 열기 사유",
            key=f"candidate_reopen_reason_{candidate['id']}",
        )
        if st.button(
            "후보 다시 열기",
            key=f"candidate_reopen_{candidate['id']}",
            width="stretch",
            disabled=not reopen_reason.strip(),
        ):
            _rerun_candidate_action(
                lambda: monitoring.transition_regression_candidate(
                    path,
                    to_status="triaged",
                    expected_record_revision=candidate["record_revision"],
                    reason=reopen_reason,
                )
            )

    elif status in {"duplicate", "rejected", "not_reproducible"}:
        st.info("종료된 대체 상태입니다. 이 후보는 읽기 전용으로 보존됩니다.")


def _render_issue_report_monitoring() -> None:
    st.subheader("신고·회귀 후보 관리")
    st.info(
        "이 화면은 최종 평가 묶음보다 앞선 단계입니다. 사용자 신고를 "
        "수정 후보로 만들고 재현·검증하는 곳이며, verified 또는 closed "
        "후보만 이후 새 평가 사례의 입력으로 사용할 수 있습니다."
    )
    st.caption(
        "평가 묶음에 후보를 추가할 때는 새로 고정한 데이터 기준에서 "
        "기대 조건과 출처를 다시 검토해야 합니다."
    )

    st.markdown("#### Import emailed issue report")
    imported_text = st.text_area(
        "이메일로 받은 Finance LLM 문제 신고 텍스트",
        key="email_issue_report_import_text",
        height=220,
        placeholder="Finance LLM 문제 신고\n====================\nReport ID: ...",
    )
    if st.button("Import emailed issue report", width="stretch", disabled=not imported_text.strip()):
        try:
            imported_report = issue_report_store.import_issue_report_text(imported_text)
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.success(f"Imported issue report: {imported_report['id']}")
            st.code(imported_report["file_path"], language="text")

    report_artifacts = issue_report_store.list_v2_issue_report_artifacts()
    reports = report_artifacts["items"]
    if report_artifacts["warnings"]:
        st.warning("일부 신고 파일이 누락되었거나 손상되었습니다.")
        st.dataframe(
            [
                {
                    "경고": warning.get("code"),
                    "파일": Path(str(warning.get("path") or "")).name,
                    "처리 차단": bool(warning.get("blocking")),
                }
                for warning in report_artifacts["warnings"]
            ],
            width="stretch",
            hide_index=True,
        )
        for warning_index, warning in enumerate(report_artifacts["warnings"]):
            if warning.get("code") != "missing_text_companion":
                continue
            if st.button(
                f"신고 설명 파일 재생성: "
                f"{Path(str(warning.get('path') or '')).name}",
                key=f"repair_issue_report_text_{warning_index}",
                width="stretch",
            ):
                _rerun_candidate_action(
                    lambda warning=warning: (
                        issue_report_store.repair_issue_report_text_companion(
                            Path(str(warning["path"])).with_suffix(".json")
                        )
                    )
                )
    thread_names = {thread["id"]: thread["name"] for thread in conversation_store.list_threads()}
    summary = monitoring.summarize_issue_reports(reports)

    col1, col2, col3 = st.columns(3)
    col1.metric("Reports", summary["report_count"])
    col2.metric("Threads", summary["thread_count"])
    col3.metric("Categories", len(summary["categories"]))

    if summary["categories"]:
        st.markdown("#### Category counts")
        st.dataframe(
            [{"category": category, "count": count} for category, count in sorted(summary["categories"].items(), key=lambda item: (-item[1], item[0]))],
            width="stretch",
            hide_index=True,
        )

    rows = monitoring.build_issue_report_rows(reports, thread_names=thread_names)
    st.markdown("#### Report rows")
    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)
        selected_report_id = st.selectbox("상세 보기", options=[row["id"] for row in rows])
        selected = next((report for report in reports if report.get("id") == selected_report_id), None)
        if selected:
            selected_row = next((row for row in rows if row.get("id") == selected_report_id), {})
            st.code(selected.get("file_path") or "", language="text")
            st.caption(
                f"Draft readiness: {selected_row.get('draft_readiness', '-')} · "
                f"Next: {selected_row.get('recommended_next_step', '-')}"
            )
            if st.button("Promote selected report to regression candidate", width="stretch"):
                candidate = monitoring.promote_issue_report_to_eval_candidate(
                    selected,
                    output_dir=MONITORING_REGRESSION_CANDIDATE_DIR,
                )
                st.success("Regression candidate artifact를 저장했습니다.")
                st.code(candidate["json_path"], language="text")
            with st.expander("원문 보기", expanded=False):
                st.text(selected.get("content") or "")
    else:
        st.caption("저장된 issue report가 없습니다.")

    st.markdown("#### Regression candidates")
    candidate_artifacts = monitoring.list_v2_regression_candidate_artifacts(
        MONITORING_REGRESSION_CANDIDATE_DIR
    )
    candidates = candidate_artifacts["items"]
    if candidate_artifacts["warnings"]:
        st.warning("일부 개선 후보 파일을 안전하게 읽을 수 없습니다.")
        st.dataframe(
            candidate_artifacts["warnings"],
            width="stretch",
            hide_index=True,
        )
    candidate_rows = monitoring.build_regression_candidate_rows(candidates)
    if not candidate_rows:
        st.caption("저장된 regression candidate가 없습니다.")
        return

    st.dataframe(candidate_rows, width="stretch", hide_index=True)
    lifecycle_candidate_id = st.selectbox(
        "상태를 관리할 개선 후보",
        options=[str(candidate.get("id")) for candidate in candidates],
        key="feedback_loop_candidate_selector",
    )
    lifecycle_candidate = next(
        (
            candidate
            for candidate in candidates
            if str(candidate.get("id")) == lifecycle_candidate_id
        ),
        None,
    )
    if lifecycle_candidate:
        _render_candidate_lifecycle(lifecycle_candidate)

    draft_candidates = [candidate for candidate in candidates if candidate.get("eval_case_draft")]
    if not draft_candidates:
        st.info("아직 evaluation case draft가 있는 candidate가 없습니다. 이메일로 받은 issue report를 가져오거나 저장된 report를 검토한 뒤, 이 화면에서 regression suite 후보로 저장하세요.")
        manual_candidates = [candidate for candidate in candidates if not candidate.get("eval_case_draft")]
        if manual_candidates:
            st.warning("draft가 없는 candidate는 자동 실행할 수 없습니다. recommended_next_step=manual_eval_case_required 항목은 수동 eval case 작성/보강이 필요합니다.")
        return

    candidate_ids = [str(candidate.get("id")) for candidate in draft_candidates]
    selected_candidate_ids = st.multiselect(
        "실행할 regression candidate draft",
        options=candidate_ids,
        default=candidate_ids,
        format_func=lambda candidate_id: next(
            (
                f"{candidate_id} · {(candidate.get('eval_case_draft') or {}).get('question', '')}"
                for candidate in draft_candidates
                if str(candidate.get("id")) == candidate_id
            ),
            candidate_id,
        ),
        help="정식 fixture 반영 전, 선택한 candidate draft만 현재 Native V2 데이터로 진단 실행합니다.",
    )
    selected_dataset = monitoring.build_regression_candidate_dataset(draft_candidates, selected_candidate_ids)
    st.caption(f"선택된 draft: {len(selected_dataset['cases'])}개")
    if selected_dataset["cases"]:
        with st.expander("선택된 evaluation case draft JSON", expanded=False):
            st.json(selected_dataset)
    regression_job_key = monitoring_jobs.session_job_key(
        _REGRESSION_CANDIDATE_JOB_KEY
    )
    regression_job = monitoring_jobs.get_job(regression_job_key)
    regression_running = bool(
        regression_job and regression_job["state"] == "running"
    )
    monitoring_jobs.render_job_status(
        regression_job_key,
        result_state_key="latest_regression_candidate_run",
        running_message=(
            "Regression candidate를 백그라운드에서 실행 중입니다. "
            "다른 화면을 사용해도 작업은 계속됩니다."
        ),
        success_message="Regression candidate run이 저장되었습니다.",
        failure_prefix="Regression candidate run failed",
    )
    if st.button(
        "Run selected regression candidates",
        width="stretch",
        disabled=not selected_dataset["cases"] or regression_running,
    ):
        try:
            candidate_data_source = monitoring.build_native_v2_evaluation_data_source(
                status_cache.get_native_v2_data_status(),
                candidate_ids=selected_candidate_ids,
            )
        except Exception as exc:
            st.error(f"Regression candidate run failed: {exc}")
        else:
            _job_id, started = monitoring_jobs.start_evaluation_job(
                regression_job_key,
                dataset=selected_dataset,
                invoke_graph=search_engine.invoke_graph,
                output_dir=MONITORING_EVAL_RUN_DIR,
                selected_case_ids=[
                    str(case.get("id")) for case in selected_dataset["cases"]
                ],
                execution_mode="regression_candidate_native_v2",
                data_source=candidate_data_source,
            )
            if started:
                st.toast("Regression candidate run을 시작했습니다.", icon="⏳")
                st.rerun(scope="app")

    latest_candidate_run = st.session_state.get("latest_regression_candidate_run")
    if latest_candidate_run:
        st.markdown("#### Latest regression candidate run")
        st.json(latest_candidate_run.get("summary") or {})
        st.dataframe(latest_candidate_run.get("results") or [], width="stretch", hide_index=True)


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
        st.caption("변경 전후의 품질을 검증하고 회귀 후보를 관리합니다.")
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

