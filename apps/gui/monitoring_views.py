"""Monitoring views for the Streamlit GUI."""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from apps.gui import data_views
from apps.gui import search_engine
from src.configs import config as config_module
from src.core import compare_pdf_extractors
from src.core import conversation_store
from src.core import feedback_handoff
from src.core import issue_report_store
from src.core import monitoring
from src.core import reproduction_manifest
from src.core import status as status_module


MONITORING_EVAL_RUN_DIR = Path("debug") / "evaluation_runs"
MONITORING_REGRESSION_CANDIDATE_DIR = Path("debug") / "regression_candidates"
MONITORING_CANDIDATE_RUN_DIR = Path("debug") / "candidate_evaluation_runs"
MONITORING_CODEX_HANDOFF_DIR = Path("debug") / "codex_handoffs"

_PROBLEM_AREA_LABELS = {
    "summary": "현재 문제",
    "response": "응답 원인 확인",
    "search_data": "검색 자료 준비",
    "evaluation": "정확도 평가",
    "parsing": "문서 읽기 품질 비교",
    "issues": "신고·수정 확인",
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
        "Run the same PDF sample through multiple parsing engines and compare extraction quality metrics. "
        "Marker is opt-in because it can be heavy on CPU-only machines."
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
                "docling requires `pip install docling`, marker can be heavy, "
                "and pdf-to-markdown requires the @pspdfkit/pdf-to-markdown CLI on PATH."
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
        submitted = st.form_submit_button("Run parsing evaluation", use_container_width=True)

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
        use_container_width=True,
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
        st.dataframe(rows, use_container_width=True, hide_index=True)
        error_rows = [row for row in rows if row.get("status") != "ok"]
        if error_rows:
            with st.expander(f"Errors ({len(error_rows)})", expanded=True):
                st.dataframe(error_rows, use_container_width=True, hide_index=True)
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


def _render_experiment_monitoring() -> None:
    st.subheader("답변 정확도 평가")
    st.caption(
        "승인된 기준 질문을 현재 Native V2 검색 데이터로 평가합니다. "
        "스키마가 검증된 V2 평가 run만 정확도 집계에 반영합니다."
    )
    try:
        dataset = monitoring.load_evaluation_dataset()
    except FileNotFoundError:
        st.info(
            "현재 승인된 기준 질문이 없습니다. "
            "원천 데이터 준비 후 docs/EVALUATION_DATASET.md의 절차에 따라 "
            "생성·검토해야 합니다."
        )
        return

    execution_mode = "native_v2"
    try:
        data_source = monitoring.build_native_v2_evaluation_data_source(
            status_module.get_native_v2_data_status()
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
    latency_threshold = st.number_input("Latency threshold seconds", min_value=1.0, max_value=300.0, value=30.0, step=1.0)
    selected_cases = monitoring.select_evaluation_cases(dataset, selected_case_ids)
    st.caption(f"선택된 테스트: {len(selected_cases)}개")
    if st.button(
        "Run selected evaluation cases",
        use_container_width=True,
        disabled=not selected_cases or data_source is None,
    ):
        assert data_source is not None
        with st.spinner("Evaluation dataset 실행 중..."):
            try:
                run = monitoring.run_evaluation_dataset(
                    dataset,
                    search_engine.invoke_graph,
                    output_dir=MONITORING_EVAL_RUN_DIR,
                    selected_case_ids=selected_case_ids,
                    latency_threshold_seconds=float(latency_threshold),
                    execution_mode=execution_mode,
                    data_source=data_source,
                )
            except Exception as exc:
                st.error(f"Evaluation run failed: {exc}")
            else:
                st.session_state.latest_evaluation_run = run
                st.success("Evaluation run saved.")

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
        st.dataframe([comparison], use_container_width=True, hide_index=True)

    st.markdown("#### Run artifacts")
    st.code(run.get("json_path") or "", language="text")
    st.markdown("#### Case results")
    results = run.get("results") or []
    st.dataframe(results, use_container_width=True, hide_index=True)

    failure_actions = monitoring.build_evaluation_failure_actions(results)
    st.markdown("#### Failure triage")
    if failure_actions:
        st.warning("Fail 케이스는 아래 권장 조치 기준으로 다음 작업을 선택하세요.")
        st.dataframe(failure_actions, use_container_width=True, hide_index=True)
        failed_case_ids = [str(row["case_id"]) for row in failure_actions if row.get("case_id")]
        if st.button(
            "Rerun failed cases only",
            use_container_width=True,
            disabled=data_source is None,
        ):
            assert data_source is not None
            with st.spinner("Failed cases 재실행 중..."):
                try:
                    rerun = monitoring.run_evaluation_dataset(
                        dataset,
                        search_engine.invoke_graph,
                        output_dir=MONITORING_EVAL_RUN_DIR,
                        selected_case_ids=failed_case_ids,
                        latency_threshold_seconds=float(latency_threshold),
                        execution_mode=execution_mode,
                        data_source=data_source,
                    )
                except Exception as exc:
                    st.error(f"Failed-case rerun failed: {exc}")
                else:
                    st.session_state.latest_evaluation_run = rerun
                    st.success("Failed cases rerun saved.")
                    st.rerun()
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
        }
        for key, value in integrity["checks"].items()
        if value.get("status") != "pass"
    ]
    st.markdown("#### 검색 자료 확인 필요")
    if problem_checks:
        st.dataframe(
            problem_checks,
            use_container_width=True,
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
            use_container_width=True,
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
                use_container_width=True,
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
            use_container_width=True,
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
        f"품질 프로파일: {candidate.get('quality_profile') or 'legacy'} · "
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
    form_revision_key = (
        f"feedback_candidate_form_revision_{candidate['id']}_{status}"
    )
    if status in {"new", "triaged", "needs_expectation", "verified"}:
        if form_revision_key not in st.session_state:
            st.session_state[form_revision_key] = candidate["record_revision"]
        form_record_revision = int(st.session_state[form_revision_key])
    else:
        form_record_revision = int(candidate["record_revision"])

    with st.expander("관찰 결과와 승인 기대 결과", expanded=False):
        observed_column, expected_column = st.columns(2)
        with observed_column:
            st.markdown("##### 관찰 결과")
            st.json(candidate.get("observed") or {})
        with expected_column:
            st.markdown("##### 승인 기대 결과")
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
            use_container_width=True,
            hide_index=True,
        )
    for run in run_recovery["attachable"]:
        required_status = (
            "ready" if run.get("run_kind") == "baseline" else "fixing"
        )
        if st.button(
            f"미연결 실행 결과 연결: {run.get('run_id')}",
            key=f"candidate_attach_run_{run['run_id']}",
            use_container_width=True,
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
            use_container_width=True,
            hide_index=True,
        )
    for item in handoff_artifacts["items"]:
        if item.get("companion_status") != "missing":
            continue
        if st.button(
            f"전달 문서 재생성: {item.get('handoff_id')}",
            key=f"candidate_repair_handoff_{item['handoff_id']}",
            use_container_width=True,
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
            use_container_width=True,
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
                    use_container_width=True,
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
            use_container_width=True,
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
            "duplicate",
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
            duplicate_of = st.text_input(
                "중복 대상 후보 ID",
                value=str(candidate.get("duplicate_of") or ""),
                help="처리 결정을 중복으로 선택했을 때만 입력합니다.",
            )
            save_triage = st.form_submit_button(
                "분류 저장",
                use_container_width=True,
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
                        "duplicate_of": (
                            duplicate_of.strip()
                            if decision == "duplicate"
                            else None
                        ),
                    },
                    reason="모니터링 화면에서 분류 저장",
                ),
                form_revision_key=form_revision_key,
            )
        if st.button(
            "분류 완료",
            key=f"candidate_mark_triaged_{candidate['id']}",
            use_container_width=True,
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
                "duplicate",
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
                duplicate_of = st.text_input(
                    "중복 대상 후보 ID",
                    value=str(candidate.get("duplicate_of") or ""),
                    help="처리 결정을 중복으로 선택했을 때만 입력합니다.",
                )
                save_followup = st.form_submit_button(
                    "추가 정보 반영",
                    use_container_width=True,
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
                            "duplicate_of": (
                                duplicate_of.strip()
                                if revised_decision == "duplicate"
                                else None
                            ),
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
                use_container_width=True,
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
                use_container_width=True,
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
        elif decision == "duplicate":
            duplicate_reason = st.text_input(
                "중복 처리 사유",
                key=f"candidate_duplicate_reason_{candidate['id']}",
            )
            if st.button(
                "중복 후보로 종료",
                key=f"candidate_duplicate_{candidate['id']}",
                use_container_width=True,
                disabled=not duplicate_reason.strip(),
            ):
                _rerun_candidate_action(
                    lambda: monitoring.transition_regression_candidate(
                        path,
                        to_status="duplicate",
                        expected_record_revision=candidate["record_revision"],
                        reason=duplicate_reason,
                    )
                )
        else:
            st.info("추가 정보가 확보되면 분류 내용을 갱신해 주세요.")

    elif status == "needs_expectation":
        expected = candidate.get("expected") or {}
        route_options = [None, "vectordb", "rdb"]
        current_route = expected.get("route")
        current_plan = candidate.get("validation_plan") or {}
        current_profile = candidate.get("quality_profile") or "balanced"
        profile_options = [
            "accuracy_first",
            "balanced",
            "speed_first",
        ]
        check_options = [
            "route_pass",
            "filter_pass",
            "source_hit",
            "citation_valid",
            "latency_pass",
            "no_result_absent",
            "expected_state_pass",
        ]
        with st.form(f"candidate_contract_{candidate['id']}"):
            reproduction_input_text = st.text_area(
                "재현 입력(JSON)",
                value=json.dumps(
                    (candidate.get("observed") or {}).get(
                        "reproduction_input"
                    )
                    or {},
                    ensure_ascii=False,
                    indent=2,
                ),
                help=(
                    "응답 문제는 question과 필요한 prior_search_scope, "
                    "응답 없는 UI 문제는 scenario를 사용합니다."
                ),
            )
            quality_profile = st.selectbox(
                "품질 프로파일",
                profile_options,
                index=(
                    profile_options.index(current_profile)
                    if current_profile in profile_options
                    else 1
                ),
                format_func=lambda value: {
                    "accuracy_first": "정확성 우선",
                    "balanced": "균형형",
                    "speed_first": "속도 우선",
                }[value],
            )
            st.caption(
                "속도 우선도 정확성·안전성 경성 기준을 "
                "최소 하나 포함해야 합니다."
            )
            route = st.selectbox(
                "기대 경로",
                route_options,
                index=route_options.index(current_route)
                if current_route in route_options
                else 0,
                format_func=lambda value: value
                or "수동 검사에서는 사용하지 않음",
            )
            filters_text = st.text_area(
                "기대 검색 조건(JSON)",
                value=json.dumps(
                    expected.get("filters") or {},
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            sources_text = st.text_area(
                "기대 출처(JSON 배열)",
                value=json.dumps(
                    expected.get("sources") or [],
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            state_text = st.text_area(
                "기대 상태(JSON)",
                value=json.dumps(
                    expected.get("state") or {},
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            assertions_text = st.text_area(
                "수동 검사 항목(JSON 배열)",
                value=json.dumps(
                    expected.get("manual_assertions") or [],
                    ensure_ascii=False,
                    indent=2,
                ),
                help=(
                    '예: [{"id": "answer_grounded", '
                    '"text": "근거가 명확함"}]'
                ),
            )
            selected_checks = st.multiselect(
                "자동 경성 검사",
                check_options,
                default=[
                    check
                    for check in current_plan.get("hard_checks")
                    or candidate.get("active_checks")
                    or []
                    if check in check_options
                ],
            )
            include_manual = st.checkbox(
                "수동 검사도 경성 기준으로 포함",
                value=(
                    "manual_assertions_pass"
                    in (
                        current_plan.get("hard_checks")
                        or candidate.get("active_checks")
                        or []
                    )
                ),
            )
            manual_mode = st.selectbox(
                "수동 검사 유형",
                ["manual_answer_quality", "manual_ui"],
                index=(
                    1
                    if candidate.get("verification_type")
                    == "manual_ui"
                    else 0
                ),
                format_func=lambda value: {
                    "manual_answer_quality": "답변 품질",
                    "manual_ui": "화면·동작",
                }[value],
                disabled=not include_manual,
            )
            soft_objectives = st.multiselect(
                "연성 목표",
                [
                    "latency_p95",
                    "answer_conciseness",
                    "answer_depth",
                ],
                default=list(
                    current_plan.get("soft_objectives") or []
                ),
            )
            current_budget = (
                current_plan.get("performance_budget") or {}
            )
            performance_enforcement = st.selectbox(
                "반복 성능 예산 판정",
                ["soft", "hard"],
                index=(
                    1
                    if current_budget.get("enforcement") == "hard"
                    or quality_profile == "speed_first"
                    else 0
                ),
                disabled=quality_profile == "speed_first",
                format_func=lambda value: {
                    "soft": "연성 목표",
                    "hard": "경성 기준",
                }[value],
            )
            max_p95_seconds = st.number_input(
                "p95 최대 시간(초)",
                min_value=0.1,
                max_value=600.0,
                value=float(
                    current_budget.get("max_p95_seconds") or 20.0
                ),
                step=0.5,
            )
            min_runs = st.number_input(
                "측정 반복 횟수",
                min_value=1,
                max_value=50,
                value=int(current_budget.get("min_runs") or 3),
                step=1,
            )
            warmup_runs = st.number_input(
                "워밍업 횟수",
                min_value=0,
                max_value=max(0, int(min_runs) - 1),
                value=min(
                    int(current_budget.get("warmup_runs") or 0),
                    max(0, int(min_runs) - 1),
                ),
                step=1,
            )
            manifest = candidate.get("reproduction_manifest") or {}
            data_revision = st.text_input(
                "데이터 개정 ID",
                value=str(manifest.get("data_revision") or ""),
            )
            index_revision = st.text_input(
                "인덱스 개정 ID",
                value=str(manifest.get("index_revision") or ""),
            )
            if manifest:
                st.caption(
                    "현재 재현 매니페스트: "
                    f"{str(manifest.get('manifest_hash') or '')[:12]} "
                    f"({'완전' if manifest.get('complete') else '보강 필요'})"
                )
            save_contract = st.form_submit_button(
                "기대 결과·프로파일·재현 정보 저장",
                use_container_width=True,
            )
        if save_contract:
            try:
                filters = json.loads(filters_text)
                sources = json.loads(sources_text)
                expected_state = json.loads(state_text)
                manual_assertions = json.loads(assertions_text)
                reproduction_input = json.loads(
                    reproduction_input_text
                )
            except json.JSONDecodeError as exc:
                st.error(f"JSON 형식 오류: {exc}")
            else:
                enforcement = (
                    "hard"
                    if quality_profile == "speed_first"
                    else performance_enforcement
                )
                hard_checks = list(selected_checks)
                if include_manual:
                    hard_checks.append("manual_assertions_pass")
                if enforcement == "hard":
                    hard_checks.append("performance_p95_pass")
                validation_plan = {
                    "schema_version": 1,
                    "quality_profile": quality_profile,
                    "hard_checks": hard_checks,
                    "soft_objectives": soft_objectives,
                    "performance_budget": {
                        "max_p95_seconds": float(max_p95_seconds),
                        "min_runs": int(min_runs),
                        "warmup_runs": int(warmup_runs),
                        "enforcement": enforcement,
                    },
                }
                current_reproduction_manifest = (
                    reproduction_manifest
                    .build_runtime_reproduction_manifest(
                        data_revision=data_revision.strip() or None,
                        index_revision=index_revision.strip() or None,
                    )
                )
                _rerun_candidate_action(
                    lambda: monitoring.update_regression_candidate(
                        path,
                        expected_record_revision=form_record_revision,
                        changes={
                            "observed": {
                                "reproduction_input": (
                                    reproduction_input
                                )
                            },
                            "expected": {
                                "route": route,
                                "filters": filters,
                                "sources": sources,
                                "state": expected_state,
                                "manual_assertions": (
                                    manual_assertions
                                    if include_manual
                                    else []
                                ),
                            },
                            "validation_plan": validation_plan,
                            "reproduction_manifest": (
                                current_reproduction_manifest
                            ),
                            "verification_type": (
                                manual_mode
                                if include_manual
                                and not selected_checks
                                else "mixed"
                                if include_manual
                                else "graph_contract"
                            ),
                        },
                        reason="모니터링 화면에서 기대 결과 저장",
                    ),
                    form_revision_key=form_revision_key,
                )
        if st.button(
            "기대 결과 승인",
            key=f"candidate_approve_{candidate['id']}",
            use_container_width=True,
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
            use_container_width=True,
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
                use_container_width=True,
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
                use_container_width=True,
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
                use_container_width=True,
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
            use_container_width=True,
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
            use_container_width=True,
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
                use_container_width=True,
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
            use_container_width=True,
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
            use_container_width=True,
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
    st.subheader("Issue reports")
    st.caption("사용자 신고를 전체 개선 루프의 입력으로 모아 봅니다. 필요하면 실패 케이스를 regression 후보로 승격할 수 있습니다.")

    st.markdown("#### Import emailed issue report")
    imported_text = st.text_area(
        "이메일로 받은 Finance LLM 문제 신고 텍스트",
        key="email_issue_report_import_text",
        height=220,
        placeholder="Finance LLM 문제 신고\n====================\nReport ID: ...",
    )
    if st.button("Import emailed issue report", use_container_width=True, disabled=not imported_text.strip()):
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
            use_container_width=True,
            hide_index=True,
        )
        for warning_index, warning in enumerate(report_artifacts["warnings"]):
            if warning.get("code") != "missing_text_companion":
                continue
            if st.button(
                f"신고 설명 파일 재생성: "
                f"{Path(str(warning.get('path') or '')).name}",
                key=f"repair_issue_report_text_{warning_index}",
                use_container_width=True,
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
            use_container_width=True,
            hide_index=True,
        )

    rows = monitoring.build_issue_report_rows(reports, thread_names=thread_names)
    st.markdown("#### Report rows")
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
        selected_report_id = st.selectbox("상세 보기", options=[row["id"] for row in rows])
        selected = next((report for report in reports if report.get("id") == selected_report_id), None)
        if selected:
            selected_row = next((row for row in rows if row.get("id") == selected_report_id), {})
            st.code(selected.get("file_path") or "", language="text")
            st.caption(
                f"Draft readiness: {selected_row.get('draft_readiness', '-')} · "
                f"Next: {selected_row.get('recommended_next_step', '-')}"
            )
            if st.button("Promote selected report to regression candidate", use_container_width=True):
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
            use_container_width=True,
            hide_index=True,
        )
    candidate_rows = monitoring.build_regression_candidate_rows(candidates)
    if not candidate_rows:
        st.caption("저장된 regression candidate가 없습니다.")
        return

    st.dataframe(candidate_rows, use_container_width=True, hide_index=True)
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
    if st.button("Run selected regression candidates", use_container_width=True, disabled=not selected_dataset["cases"]):
        with st.spinner("Regression candidate draft 실행 중..."):
            try:
                candidate_data_source = (
                    monitoring.build_native_v2_evaluation_data_source(
                        status_module.get_native_v2_data_status(),
                        candidate_ids=selected_candidate_ids,
                    )
                )
                run = monitoring.run_evaluation_dataset(
                    selected_dataset,
                    search_engine.invoke_graph,
                    output_dir=MONITORING_EVAL_RUN_DIR,
                    selected_case_ids=[case.get("id") for case in selected_dataset["cases"]],
                    execution_mode="regression_candidate_native_v2",
                    data_source=candidate_data_source,
                )
            except Exception as exc:
                st.error(f"Regression candidate run failed: {exc}")
            else:
                st.session_state.latest_regression_candidate_run = run
                st.success("Regression candidate run saved.")
                st.code(run.get("json_path") or "", language="text")

    latest_candidate_run = st.session_state.get("latest_regression_candidate_run")
    if latest_candidate_run:
        st.markdown("#### Latest regression candidate run")
        st.json(latest_candidate_run.get("summary") or {})
        st.dataframe(latest_candidate_run.get("results") or [], use_container_width=True, hide_index=True)


def _render_global_chat_diagnostics(current_id: str, messages: list[dict]) -> None:
    rows = monitoring.build_message_monitoring_rows(messages)
    if rows:
        st.dataframe(
            [
                {
                    "발생 시각": row.get("created_at"),
                    "상태": row.get("status"),
                    "응답 시간": row.get("latency_seconds"),
                    "질문": row.get("user_question_preview"),
                }
                for row in rows
            ],
            use_container_width=True,
            hide_index=True,
        )
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
    with st.expander("검색어와 검색 범위", expanded=False):
        st.json(detail["query_rewrite"])
        st.json(detail["scope"])
        st.json(detail["routing"])
    with st.expander("참고 자료 선택과 출처 표시", expanded=False):
        st.json(detail["retrieval"])
        st.json(detail["answer"])
    with st.expander("처리 흐름 기술 정보", expanded=False):
        st.json(detail["state_transitions"])


def _format_chat_duration(seconds: float | None) -> str:
    if seconds is None:
        return "측정 전"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.2f}초"


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
        use_container_width=True,
        hide_index=True,
    )


def render_chat_monitoring_page(current_id: str, current_thread: dict) -> None:
    """Render current-thread Native V2 latency metrics only."""
    st.header("답변 모니터링")
    st.caption(f"현재 대화: {current_thread['name']} · 성공한 Native V2 응답만 집계")
    messages = conversation_store.list_messages(current_id)
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

    st.markdown("#### 응답별 시간")
    _render_chat_latency_table(messages)


def render_global_monitoring_page() -> None:
    """Render the V2-only speed/accuracy dashboard and problem tools."""
    st.header("답변 모니터링")
    st.caption(
        "평소에는 답변 속도와 정확도만 확인합니다. 나머지 정보는 "
        "문제가 있을 때 필요한 진단 도구에서 확인합니다."
    )

    status = status_module.get_native_v2_data_status()
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
    with st.expander(
        f"문제 상황 자세히 보기 · 확인 필요 {problem_count}건",
        expanded=problem_count > 0,
    ):
        problem_area = st.selectbox(
            "확인할 내용",
            options=list(_PROBLEM_AREA_LABELS),
            format_func=_PROBLEM_AREA_LABELS.__getitem__,
            key="monitoring_problem_area",
        )
        if problem_area == "summary":
            _render_global_monitoring(summary, integrity, accuracy)
        elif problem_area == "response":
            if not thread_messages:
                st.caption("확인할 대화가 없습니다.")
            else:
                thread_by_id = {
                    str(entry["thread"]["id"]): entry
                    for entry in thread_messages
                }
                selected_thread_id = st.selectbox(
                    "확인할 대화",
                    options=list(thread_by_id),
                    format_func=lambda thread_id: thread_by_id[thread_id][
                        "thread"
                    ]["name"],
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
            _render_experiment_monitoring()
        elif problem_area == "parsing":
            _render_parsing_engine_evaluation()
        else:
            _render_issue_report_monitoring()

