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
from src.core import issue_report_store
from src.core import monitoring
from src.core import status as status_module


MONITORING_EVAL_RUN_DIR = Path("debug") / "evaluation_runs"
MONITORING_REGRESSION_CANDIDATE_DIR = Path("debug") / "regression_candidates"


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


def _render_experiment_monitoring() -> None:
    st.subheader("실험 실행")
    st.caption("고정 evaluation dataset을 current data 또는 fixed snapshot 모드로 실행하고 route/filter/source/citation/latency pass/fail을 저장합니다.")
    try:
        dataset = monitoring.load_evaluation_dataset()
    except FileNotFoundError:
        st.warning("평가셋 fixture를 찾지 못했습니다: tests/fixtures/evaluation_dataset.json")
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

    reports = issue_report_store.list_issue_reports()
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
    candidates = monitoring.list_regression_candidates(MONITORING_REGRESSION_CANDIDATE_DIR)
    candidate_rows = monitoring.build_regression_candidate_rows(candidates)
    if not candidate_rows:
        st.caption("저장된 regression candidate가 없습니다.")
        return

    st.dataframe(candidate_rows, use_container_width=True, hide_index=True)
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
                st.warning("평가셋 fixture를 찾지 못했습니다: tests/fixtures/evaluation_dataset.json")
                return

            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Version", summary["version"])
            col2.metric("Cases", summary["case_count"])
            col3.metric("Expected sources", summary["expected_source_count"])
            col4.metric("Snapshot", summary["snapshot_date"] or "-")

            stability_policy = summary.get("stability_policy") or {}
            st.info(
                "테스트셋은 변경 사유가 생기기 전까지 고정합니다. "
                f"정책: `{stability_policy.get('policy', '-')}`"
            )

            left, right = st.columns(2)
            with left:
                st.markdown("#### Route case coverage")
                st.dataframe(_case_type_rows(summary), use_container_width=True, hide_index=True)
            with right:
                st.markdown("#### Monitoring dimensions")
                st.dataframe(_dimension_rows(summary), use_container_width=True, hide_index=True)

            with st.expander("변경 허용 사유"):
                st.write(stability_policy.get("allowed_change_reasons") or [])
            with st.expander("평가 케이스 목록"):
                st.dataframe(
                    [
                        {
                            "id": case.get("id"),
                            "type": case.get("type"),
                            "route": case.get("expected_route"),
                            "dimensions": ", ".join(case.get("monitoring_dimensions", [])),
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

