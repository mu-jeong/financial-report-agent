"""Monitoring views for the Streamlit GUI."""

from __future__ import annotations

import json
import subprocess
import sys
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


def _dimension_rows(summary: dict) -> list[dict]:
    return [
        {"dimension": key, "case_count": value}
        for key, value in sorted(
            (summary.get("monitoring_dimensions") or {}).items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]


def _case_type_rows(summary: dict) -> list[dict]:
    return [
        {"case_type": key, "case_count": value}
        for key, value in sorted((summary.get("case_types") or {}).items())
    ]


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
            run = json.loads(run_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        loaded_runs.append(run)
    matching_runs = monitoring.filter_evaluation_runs_by_mode(loaded_runs, execution_mode)
    return matching_runs[0] if matching_runs else None


def _run_fixed_snapshot_evaluation(
    *,
    selected_case_ids: list[str],
    latency_threshold_seconds: float,
) -> dict:
    repo_root = Path(__file__).resolve().parents[2]
    command = [
        sys.executable,
        str(repo_root / "scripts" / "run_evaluation_snapshot.py"),
        "--dataset",
        str(repo_root / "tests" / "fixtures" / "evaluation_dataset.json"),
        "--snapshot-root",
        str(repo_root / "tests" / "fixtures" / "eval_snapshot"),
        "--output-dir",
        str(repo_root / MONITORING_EVAL_RUN_DIR),
        "--latency-threshold-seconds",
        str(latency_threshold_seconds),
    ]
    for case_id in selected_case_ids:
        command.extend(["--case-id", case_id])
    completed = subprocess.run(
        command,
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=None,
    )
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    try:
        payload = json.loads(stdout.splitlines()[-1]) if stdout else {}
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Snapshot runner returned non-JSON output. stdout={stdout!r}, stderr={stderr!r}") from exc
    if completed.returncode != 0 or payload.get("status") != "ok":
        detail = payload.get("validation") or payload.get("error") or stderr or stdout
        raise RuntimeError(f"Snapshot evaluation failed: {detail}")
    json_path = Path(payload["json_path"])
    if not json_path.is_absolute():
        json_path = repo_root / json_path
    return json.loads(json_path.read_text(encoding="utf-8"))


def _run_candidate_snapshot_evaluation(
    candidate: dict,
    *,
    run_kind: str,
    latency_threshold_seconds: float,
) -> dict:
    repo_root = Path(__file__).resolve().parents[2]
    command = [
        sys.executable,
        str(repo_root / "scripts" / "run_candidate_evaluation_snapshot.py"),
        "--candidate",
        str(candidate["json_path"]),
        "--dataset",
        str(repo_root / "tests" / "fixtures" / "evaluation_dataset.json"),
        "--snapshot-root",
        str(repo_root / "tests" / "fixtures" / "eval_snapshot"),
        "--output-dir",
        str(repo_root / MONITORING_CANDIDATE_RUN_DIR),
        "--run-kind",
        run_kind,
        "--latency-threshold-seconds",
        str(latency_threshold_seconds),
    ]
    completed = subprocess.run(
        command,
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=None,
    )
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    try:
        payload = json.loads(stdout.splitlines()[-1]) if stdout else {}
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "고정 자료 후보 실행기가 올바른 결과를 반환하지 않았습니다."
        ) from exc
    if completed.returncode != 0 or payload.get("status") != "ok":
        detail = (
            payload.get("stage")
            or payload.get("error_type")
            or stderr
            or "unknown"
        )
        raise RuntimeError(f"고정 자료 후보 실행 실패: {detail}")
    return monitoring.load_evaluation_run(payload["json_path"])


def _fixed_snapshot_assets_present() -> bool:
    required_paths = (
        monitoring.EVALUATION_DATASET_PATH,
        monitoring.EVALUATION_SNAPSHOT_MANIFEST_PATH,
        monitoring.EVALUATION_SNAPSHOT_ROOT / "reports.db",
        monitoring.EVALUATION_SNAPSHOT_ROOT / "vector_db" / "index.faiss",
        monitoring.EVALUATION_SNAPSHOT_ROOT / "vector_db" / "index.pkl",
    )
    return all(path.is_file() for path in required_paths)


def _render_experiment_monitoring() -> None:
    st.subheader("실험 실행")
    st.caption(
        "승인된 evaluation dataset이 준비된 이후 current data 또는 fixed "
        "snapshot 모드로 평가합니다."
    )
    try:
        dataset = monitoring.load_evaluation_dataset()
    except FileNotFoundError:
        st.info(
            "현재 정식 evaluation dataset과 fixed snapshot은 없습니다. "
            "원천 데이터 준비 후 docs/EVALUATION_DATASET.md의 절차에 따라 "
            "생성·검토해야 합니다."
        )
        return

    mode_label = st.radio(
        "실험 실행 모드",
        ["현재 데이터로 실행", "고정 테스트 snapshot으로 실행"],
        index=1,
        horizontal=True,
        help="baseline 비교는 같은 실행 모드끼리만 의미 있습니다.",
    )
    execution_mode = "fixed_snapshot" if mode_label == "고정 테스트 snapshot으로 실행" else "current_data"
    snapshot_validation = None
    if execution_mode == "current_data":
        st.info("현재 `data/reports.db`와 `data/vector_db`를 사용합니다. DB/index가 바뀌면 baseline 비교가 흔들릴 수 있습니다.")
    else:
        st.info("`tests/fixtures/eval_snapshot`의 고정 DB/index를 별도 Python 프로세스에서 사용합니다.")
        try:
            manifest = monitoring.load_evaluation_snapshot_manifest()
        except FileNotFoundError:
            st.error("Snapshot manifest를 찾지 못했습니다: tests/fixtures/eval_snapshot/manifest.json")
        else:
            snapshot_validation = monitoring.validate_evaluation_snapshot(dataset, manifest)
            if snapshot_validation["status"] == "pass":
                st.success("Fixed snapshot validation passed.")
            else:
                st.error("Fixed snapshot validation failed. Snapshot DB/index를 생성한 뒤 실행할 수 있습니다.")
                st.dataframe(snapshot_validation["checks"], use_container_width=True, hide_index=True)

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
    snapshot_ready = execution_mode == "current_data" or (snapshot_validation or {}).get("status") == "pass"
    if st.button("Run selected evaluation cases", use_container_width=True, disabled=not selected_cases or not snapshot_ready):
        with st.spinner("Evaluation dataset 실행 중..."):
            try:
                if execution_mode == "fixed_snapshot":
                    run = _run_fixed_snapshot_evaluation(
                        selected_case_ids=selected_case_ids,
                        latency_threshold_seconds=float(latency_threshold),
                    )
                else:
                    run = monitoring.run_evaluation_dataset(
                        dataset,
                        search_engine.invoke_graph,
                        output_dir=MONITORING_EVAL_RUN_DIR,
                        selected_case_ids=selected_case_ids,
                        latency_threshold_seconds=float(latency_threshold),
                        execution_mode="current_data",
                        data_source={"db_path": "data/reports.db", "faiss_dir": "data/vector_db"},
                    )
            except Exception as exc:
                st.error(f"Evaluation run failed: {exc}")
            else:
                st.session_state.latest_evaluation_run = run
                st.success("Evaluation run saved.")

    latest_run = st.session_state.get("latest_evaluation_run")
    if latest_run and (latest_run.get("execution_mode") or "current_data") != execution_mode:
        latest_run = None
    run = latest_run or _latest_saved_evaluation_run(execution_mode=execution_mode)
    if not run:
        st.caption("아직 저장된 evaluation run이 없습니다.")
        return

    st.markdown("#### Latest run summary")
    st.caption(f"Execution mode: `{run.get('execution_mode') or 'current_data'}`")
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
        execution_mode=run.get("execution_mode") or "current_data",
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
        if st.button("Rerun failed cases only", use_container_width=True):
            with st.spinner("Failed cases 재실행 중..."):
                try:
                    if (run.get("execution_mode") or "current_data") == "fixed_snapshot":
                        rerun = _run_fixed_snapshot_evaluation(
                            selected_case_ids=failed_case_ids,
                            latency_threshold_seconds=float(latency_threshold),
                        )
                    else:
                        rerun = monitoring.run_evaluation_dataset(
                            dataset,
                            search_engine.invoke_graph,
                            output_dir=MONITORING_EVAL_RUN_DIR,
                            selected_case_ids=failed_case_ids,
                            latency_threshold_seconds=float(latency_threshold),
                            execution_mode="current_data",
                            data_source={"db_path": "data/reports.db", "faiss_dir": "data/vector_db"},
                        )
                except Exception as exc:
                    st.error(f"Failed-case rerun failed: {exc}")
                else:
                    st.session_state.latest_evaluation_run = rerun
                    st.success("Failed cases rerun saved.")
                    st.rerun()
    else:
        st.success("현재 run에는 triage가 필요한 fail 케이스가 없습니다.")


def _render_global_monitoring(status: dict) -> None:
    st.subheader("전체 Monitoring")
    st.caption("모든 대화와 저장소 상태를 집계해 운영 품질을 봅니다. 개별 chat 원문은 기본 노출하지 않습니다.")
    thread_messages = _all_thread_messages()
    summary = monitoring.summarize_all_chat_threads(thread_messages)
    integrity = monitoring.summarize_data_integrity(status)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Threads", summary["thread_count"])
    col2.metric("Assistant", summary["assistant_message_count"])
    col3.metric("Failure rate", f"{summary['failure_rate'] * 100:.1f}%")
    col4.metric("No-result rate", f"{summary['no_result_rate'] * 100:.1f}%")

    col1, col2, col3 = st.columns(3)
    avg_latency = summary.get("avg_latency_seconds")
    p95_latency = summary.get("p95_latency_seconds")
    col1.metric("Avg latency", "-" if avg_latency is None else f"{avg_latency:.2f}s")
    col2.metric("P95 latency", "-" if p95_latency is None else f"{p95_latency:.2f}s")
    col3.metric("Integrity issues", integrity["warning_count"] + integrity["fail_count"])

    left, right = st.columns(2)
    with left:
        st.markdown("#### Status counts")
        st.dataframe([{"status": key, "count": value} for key, value in sorted(summary["statuses"].items())], use_container_width=True, hide_index=True)
    with right:
        st.markdown("#### Route counts")
        st.dataframe([{"route": key, "count": value} for key, value in sorted(summary["routes"].items())], use_container_width=True, hide_index=True)

    st.markdown("#### Data integrity checks")
    st.dataframe(
        [{"check": key, **value} for key, value in integrity["checks"].items()],
        use_container_width=True,
        hide_index=True,
    )
    failures = summary.get("recent_failures") or []
    st.markdown("#### Recent failed responses")
    if failures:
        st.dataframe(failures, use_container_width=True, hide_index=True)
    else:
        st.caption("최근 실패 응답이 없습니다.")


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


def _run_and_record_candidate_snapshot(
    candidate: dict,
    *,
    run_kind: str,
    latency_threshold_seconds: float,
) -> dict:
    run = _run_candidate_snapshot_evaluation(
        candidate,
        run_kind=run_kind,
        latency_threshold_seconds=latency_threshold_seconds,
    )
    return monitoring.record_candidate_run(
        candidate["json_path"],
        run=run,
        run_kind=run_kind,
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
        run_kind = "baseline" if status == "ready" else "verification"
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
            snapshot_assets_present = _fixed_snapshot_assets_present()
            if snapshot_assets_present:
                st.info(
                    "후보 실행은 별도 프로세스에서 승인된 고정 DB·벡터 "
                    "스냅샷만 사용합니다. 현재 데이터 경로는 사용하지 "
                    "않습니다."
                )
            else:
                st.info(
                    "현재 정식 evaluation dataset과 fixed snapshot이 없어 "
                    "자동 재현·검증 실행은 비활성화됩니다. 데이터 준비 후 "
                    "docs/EVALUATION_DATASET.md의 절차를 완료해야 합니다."
                )
            snapshot_confirmed = st.checkbox(
                "정식 evaluation dataset과 fixed snapshot의 검토가 끝났습니다.",
                key=(
                    f"candidate_snapshot_confirm_{status}_"
                    f"{candidate['id']}"
                ),
                disabled=not snapshot_assets_present,
            )
            latency_threshold = st.number_input(
                "허용 지연 시간(초)",
                min_value=1.0,
                max_value=300.0,
                value=30.0,
                step=1.0,
                key=(
                    f"candidate_latency_threshold_{status}_"
                    f"{candidate['id']}"
                ),
            )
            action_label = (
                "수정 전 오류 재현 실행"
                if status == "ready"
                else "수정 후 검증 실행"
            )
            if st.button(
                action_label,
                key=f"candidate_run_{run_kind}_{candidate['id']}",
                use_container_width=True,
                disabled=(
                    not actions[automatic_action_key]["enabled"]
                    or not snapshot_assets_present
                    or not snapshot_confirmed
                ),
                help=actions[automatic_action_key]["reason"],
            ):
                _rerun_candidate_action(
                    lambda: _run_and_record_candidate_snapshot(
                        candidate,
                        run_kind=run_kind,
                        latency_threshold_seconds=float(latency_threshold),
                    )
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

    report_artifacts = issue_report_store.list_issue_report_artifacts()
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
    candidate_artifacts = monitoring.list_regression_candidate_artifacts(
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
        help="정식 fixture 반영 전, 선택한 candidate draft만 current data 기준으로 재현 실행합니다.",
    )
    selected_dataset = monitoring.build_regression_candidate_dataset(draft_candidates, selected_candidate_ids)
    st.caption(f"선택된 draft: {len(selected_dataset['cases'])}개")
    if selected_dataset["cases"]:
        with st.expander("선택된 evaluation case draft JSON", expanded=False):
            st.json(selected_dataset)
    if st.button("Run selected regression candidates", use_container_width=True, disabled=not selected_dataset["cases"]):
        with st.spinner("Regression candidate draft 실행 중..."):
            try:
                run = monitoring.run_evaluation_dataset(
                    selected_dataset,
                    search_engine.invoke_graph,
                    output_dir=MONITORING_EVAL_RUN_DIR,
                    selected_case_ids=[case.get("id") for case in selected_dataset["cases"]],
                    execution_mode="regression_candidate_current_data",
                    data_source={
                        "db_path": "data/reports.db",
                        "faiss_dir": "data/vector_db",
                        "candidate_ids": selected_candidate_ids,
                    },
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


def render_chat_monitoring_page(current_id: str, current_thread: dict) -> None:
    """Render metrics for the currently selected chat only."""
    st.header("Chat Monitoring")
    st.caption(f"현재 선택된 chat: {current_thread['name']}")
    messages = conversation_store.list_messages(current_id)
    summary = monitoring.summarize_chat_messages(messages)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Messages", summary["message_count"])
    col2.metric("Assistant", summary["assistant_message_count"])
    col3.metric("Avg sources", f"{summary['avg_rerank_source_count']:.1f}")
    latency = summary["avg_latency_seconds"]
    col4.metric("Avg latency", "-" if latency is None else f"{latency:.2f}s")

    left, right = st.columns(2)
    with left:
        st.markdown("#### Status counts")
        st.json(summary["statuses"])
    with right:
        st.markdown("#### Route counts")
        st.json(summary["routes"])

    st.markdown("#### Assistant response rows")
    rows = monitoring.build_message_monitoring_rows(messages)
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.caption("아직 모니터링할 assistant 응답이 없습니다.")

    st.markdown("#### 응답 선택 상세")
    selectable_rows = [row for row in rows if row.get("message_id") is not None]
    if selectable_rows:
        label_by_id = {row["message_id"]: row.get("label", str(row["message_id"])) for row in selectable_rows}
        selected_message_id = st.selectbox(
            "상세 볼 응답 선택",
            [row["message_id"] for row in selectable_rows],
            index=len(selectable_rows) - 1,
            format_func=lambda message_id: label_by_id.get(message_id, str(message_id)),
            key=f"chat_monitoring_selected_response_{current_id}",
        )
        selected_message = next(
            (message for message in messages if message.get("id") == selected_message_id),
            None,
        )
        if selected_message:
            selected_user_question = monitoring.user_question_before_message(messages, selected_message_id)
            previous_message = monitoring.previous_successful_assistant(messages, selected_message_id)
            detail = monitoring.build_message_trace_detail(selected_message, user_question=selected_user_question)
            diff = monitoring.build_response_diff(selected_message, previous_message)
            hints = monitoring.build_chat_trace_debug_hints(
                selected_message,
                previous_message,
                user_question=selected_user_question,
            )

            trace_summary = monitoring.build_message_trace_summary(detail, diff=diff, hints=hints)
            trace_tabs = st.tabs([
                "Trace summary",
                "Scope / routing",
                "Advanced diagnostics",
            ])
            with trace_tabs[0]:
                st.json(trace_summary)
                st.markdown("#### Debug hints")
                if hints:
                    for hint in hints:
                        st.warning(hint)
                else:
                    st.success("현재 선택 응답에서 자동 감지된 흔한 RAG 실패 패턴은 없습니다.")

                st.markdown("#### Previous vs selected diff")
                if diff:
                    st.json(diff)
                else:
                    st.caption("비교할 이전 성공 assistant 응답이 없습니다.")
            with trace_tabs[1]:
                st.markdown("##### Query rewrite / follow-up")
                st.json(detail["query_rewrite"])
                st.markdown("##### Scope / filters")
                st.json(detail["scope"])
                st.markdown("##### Routing")
                st.json(detail["routing"])
            with trace_tabs[2]:
                with st.expander("State transitions", expanded=True):
                    st.json(detail["state_transitions"])
                with st.expander("Retrieval / rerank", expanded=False):
                    st.json(detail["retrieval"])
                with st.expander("Answer / citations", expanded=False):
                    st.json(detail["answer"])

    else:
        st.caption("상세 trace를 표시할 assistant 응답이 없습니다.")


def render_global_monitoring_page() -> None:
    """Render global Monitoring Mode pages that do not depend on a selected chat."""
    st.header("Monitoring Mode")
    st.caption(
        "성능개선을 위한 지표 모니터링 화면입니다. parsing, chunking, retrieval/rerank, "
        "모델 변경에 따른 답변 안정성, latency/비용을 같은 기준선으로 비교하기 위한 정보를 모읍니다."
    )

    status = status_module.get_data_status()
    db_status = status["db"]
    vector_status = status["vector_db"]
    config = status["config"]
    category_labels = [label for label in monitoring.build_monitoring_tab_labels() if label != "Chat Monitoring"]
    category = st.radio(
        "Monitoring category",
        category_labels,
        horizontal=True,
        key="global_monitoring_category",
    )
    section_labels = monitoring.build_global_monitoring_section_labels(category)

    if category == "운영 상태":
        data_tab, unembedded_tab, global_monitoring_tab = st.tabs(section_labels)

        with data_tab:
            st.subheader("데이터 준비 상태")
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("리포트", f"{db_status['total_reports']}건")
            col2.metric("임베딩 완료", f"{db_status['embedded_reports']}건")
            col3.metric("미완료", f"{db_status['pending_reports']}건")
            col4.metric("검색 커버리지", f"{status['search_coverage_ratio'] * 100:.1f}%")

            col1, col2, col3, col4 = st.columns(4)
            col1.metric("FAISS", "있음" if vector_status["has_faiss_index"] else "없음")
            col2.metric("Vector files", f"{vector_status['file_count']}개")
            col3.metric("Parent chunks", f"{db_status['parent_chunks']}건")
            col4.metric("PDF", f"{status['downloaded_pdfs']}개")

            st.subheader("현재 파이프라인 설정")
            st.json(
                {
                    "generation_model": config["generation_model"],
                    "embedding_model": config["embedding_model"],
                    "extraction_engine": config["extraction_engine"],
                    "unembedded_extraction_engine": config.get("unembedded_extraction_engine"),
                    "use_parent_child": config["use_parent_child"],
                    "use_reranker": config["use_reranker"],
                    "search_top_k": config["search_top_k"],
                    "test_limit": config["test_limit"],
                }
            )

            st.subheader("날짜별 데이터 캘린더 원천")
            date_counts = [
                {
                    "report_date": report_date,
                    "embedded_count": count,
                    **(db_status.get("report_date_type_counts") or {}).get(report_date, {}),
                }
                for report_date, count in (db_status.get("report_date_counts") or {}).items()
            ]
            st.dataframe(date_counts, use_container_width=True, hide_index=True)

        with unembedded_tab:
            data_views.render_unembedded_reports(status)

        with global_monitoring_tab:
            _render_global_monitoring(status)
        return

    if category == "평가/실험":
        eval_tab, experiment_tab, parsing_tab = st.tabs(section_labels)

        with eval_tab:
            st.subheader("고정 평가 테스트셋")
            try:
                dataset = monitoring.load_evaluation_dataset()
                summary = monitoring.summarize_evaluation_dataset(dataset)
            except FileNotFoundError:
                st.info(
                    "현재 정식 evaluation dataset과 fixed snapshot은 "
                    "없습니다. 생성 TODO는 docs/EVALUATION_DATASET.md에 "
                    "기록되어 있습니다."
                )
            else:
                col1, col2, col3, col4 = st.columns(4)
                col1.metric("Version", summary["version"])
                col2.metric("Cases", summary["case_count"])
                col3.metric(
                    "Expected sources",
                    summary["expected_source_count"],
                )
                col4.metric("Snapshot", summary["snapshot_date"] or "-")

                stability_policy = summary.get("stability_policy") or {}
                st.info(
                    "테스트셋은 변경 사유가 생기기 전까지 고정합니다. "
                    f"정책: `{stability_policy.get('policy', '-')}`"
                )

                left, right = st.columns(2)
                with left:
                    st.markdown("#### Route case coverage")
                    st.dataframe(
                        _case_type_rows(summary),
                        use_container_width=True,
                        hide_index=True,
                    )
                with right:
                    st.markdown("#### Monitoring dimensions")
                    st.dataframe(
                        _dimension_rows(summary),
                        use_container_width=True,
                        hide_index=True,
                    )

                with st.expander("변경 허용 사유"):
                    st.write(
                        stability_policy.get("allowed_change_reasons") or []
                    )
                with st.expander("평가 케이스 목록"):
                    st.dataframe(
                        [
                            {
                                "id": case.get("id"),
                                "type": case.get("type"),
                                "route": case.get("expected_route"),
                                "dimensions": ", ".join(
                                    case.get("monitoring_dimensions", [])
                                ),
                                "question": case.get("question"),
                            }
                            for case in dataset.get("cases", [])
                        ],
                        use_container_width=True,
                        hide_index=True,
                    )

        with experiment_tab:
            _render_experiment_monitoring()

        with parsing_tab:
            _render_parsing_engine_evaluation()
        return

    _render_issue_report_monitoring()

