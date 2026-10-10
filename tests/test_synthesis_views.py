from __future__ import annotations

import hashlib
import sqlite3
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

from apps.gui import synthesis_views as views


class _State(dict):
    __getattr__ = dict.get

    def __setattr__(self, name, value):
        self[name] = value


class _Region:
    def __init__(self, ui):
        self.ui = ui

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def metric(self, label, value):
        self.ui.metrics.append((label, value))

    def button(self, label, **kwargs):
        return self.ui.button(label, **kwargs)


class _FakeStreamlit:
    def __init__(self, *, pressed=()):
        self.session_state = _State()
        self.query_params = {}
        self.pressed = set(pressed)
        self.messages = []
        self.markdowns = []
        self.markdown_calls = []
        self.buttons = []
        self.downloads = []
        self.metrics = []
        self.reruns = []
        self.forms = []
        self.expanders = []
        self.containers = []

    def __getattr__(self, name):
        if name in {"header", "subheader", "caption", "info", "warning", "error", "success", "write"}:
            return lambda message, **_kwargs: self.messages.append((name, str(message)))
        raise AttributeError(name)

    def selectbox(self, _label, options, **_kwargs):
        return options[0] if options else None

    def radio(self, _label, options, **_kwargs):
        return options[0]

    def date_input(self, _label, value="today", **kwargs):
        return self.session_state.get(kwargs.get("key"), value)

    def columns(self, count, **_kwargs):
        size = count if isinstance(count, int) else len(count)
        return [_Region(self) for _ in range(size)]

    def button(self, label, **kwargs):
        self.buttons.append((label, kwargs))
        return label in self.pressed and not kwargs.get("disabled", False)

    def form(self, key, **kwargs):
        self.forms.append((key, kwargs))
        return _Region(self)

    def form_submit_button(self, label, **kwargs):
        return self.button(label, **kwargs)

    def download_button(self, label, **kwargs):
        self.downloads.append((label, kwargs))
        return False

    def expander(self, label, **kwargs):
        self.expanders.append((label, kwargs))
        return _Region(self)

    def container(self, **kwargs):
        self.containers.append(kwargs)
        return _Region(self)

    def markdown(self, body, **kwargs):
        self.markdowns.append(str(body))
        self.markdown_calls.append((str(body), kwargs))

    def progress(self, _value):
        return None

    def divider(self):
        return None

    def rerun(self, **kwargs):
        self.reruns.append(kwargs)

    def fragment(self, func=None, **_kwargs):
        def decorator(target):
            return target

        return decorator(func) if func is not None else decorator


def _document(**overrides):
    base = {
        "report_uid": "a" * 64,
        "canonical_relative_path": "downloaded/one.pdf",
        "file_name": "one.pdf",
        "source_sha256": "b" * 64,
        "report_date": "2026-09-10",
        "broker": "테스트증권",
        "title": "테스트 리포트",
        "status": "completed",
        "analysis": "문서 분석",
    }
    base.update(overrides)
    return base


def _result(*, status="completed", documents=None, failed=0):
    documents = documents if documents is not None else [_document()]
    sources = [
        {
            **documents[0],
            "rank": 1,
            "passage_rank": 1,
            "document_rank": 1,
        }
    ] if documents else []
    return {
        "status": status,
        "company": "삼성전자",
        "start_date": "2026-09-01",
        "end_date": "2026-09-30",
        "revision": {"publication_generation": 2},
        "fingerprint": "fixed-scope",
        "content": "핵심 내용 [1]",
        "documents": documents,
        "selected_sources": sources,
        "citation_contract": {
            "version": 2,
            "rank_kind": "document",
            "passage_count": len(sources),
            "document_count": len(sources),
        } if sources else None,
        "counts": {
            "total": len(documents),
            "completed": len(documents) - failed,
            "failed": failed,
            "excluded": 0,
            "brokers": 1 if documents else 0,
        },
    }


def _raw_result(*, status="completed", documents=None, failed=0, evidence=None):
    prepared = documents if documents is not None else [
        _document(
            status="prepared",
            analysis=None,
            preparation={"total_segments": 2, "body_chars": 1234},
        )
    ]
    return {
        **_result(status=status, documents=prepared, failed=failed),
        "processing_mode": "raw_body",
        "evidence": evidence or [],
    }


def test_preset_range_uses_calendar_months_and_includes_both_ends():
    assert views._preset_range("최근 1개월", today=date(2026, 3, 31)) == (
        date(2026, 2, 28),
        date(2026, 3, 31),
    )


def test_owner_id_rehydrates_from_valid_query_param_and_replaces_invalid(monkeypatch):
    valid = "4c4ebf17-79d1-4d75-b5d0-47fb27851564"
    fake_st = _FakeStreamlit()
    fake_st.query_params[views._OWNER_QUERY_PARAM] = valid
    monkeypatch.setattr(views, "st", fake_st)

    assert views._session_owner_id() == valid
    assert fake_st.session_state[views._OWNER_KEY] == valid

    fake_st.session_state.clear()
    fake_st.query_params[views._OWNER_QUERY_PARAM] = "../../shared"
    regenerated = views._session_owner_id()
    assert regenerated != "../../shared"
    assert str(__import__("uuid").UUID(regenerated)) == regenerated
    assert fake_st.query_params[views._OWNER_QUERY_PARAM] == regenerated
    assert views._preset_range("최근 3개월", today=date(2026, 10, 5)) == (
        date(2026, 7, 5),
        date(2026, 10, 5),
    )


@pytest.mark.parametrize("available", [None, {
    "start_date": "2026-01-02", "end_date": "2026-08-19", "documents": 17,
}])
def test_empty_corpus_prevents_job_and_explains_unknown_coverage(monkeypatch, available):
    fake_st = _FakeStreamlit(pressed={"종합 생성"})
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views.report_corpus, "list_companies", lambda: ["삼성전자"])
    monkeypatch.setattr(
        views.report_corpus,
        "load_report_corpus",
        lambda *_args: {"company": "삼성전자", "documents": [], "excluded": [], "warnings": [],
                       "fingerprint": "empty", "available_period": available},
    )
    monkeypatch.setattr(views.synthesis_jobs, "list_jobs", lambda *_args, **_kwargs: [])
    started = []
    monkeypatch.setattr(views.synthesis_jobs, "_start_corpus", lambda *args, **kwargs: started.append((args, kwargs)))

    views.render_synthesis_page()

    assert started == []
    generate = next(kwargs for label, kwargs in fake_st.buttons if label == "종합 생성")
    assert generate["disabled"] is False  # Changing the form can fix an empty scope.
    assert any("발행된 리포트가 없다는 뜻으로 단정" in message for _, message in fake_st.messages)
    if available:
        assert any("2026-01-02 ~ 2026-08-19" in message and "17건" in message
                   for kind, message in fake_st.messages if kind == "caption")


def test_page_describes_a_new_period_flow_report(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views.report_corpus, "list_companies", lambda: [])
    monkeypatch.setattr(views, "_render_job", lambda *_args, **_kwargs: None)

    views.render_synthesis_page()

    copy = "\n".join(message for _, message in fake_st.messages)
    assert "기간 시작 시점" in copy
    assert "주요 전개와 동인" in copy
    assert "종료 시점" in copy


def test_generate_button_starts_one_fixed_scope_job(monkeypatch):
    fake_st = _FakeStreamlit(pressed={"종합 생성"})
    monkeypatch.setattr(views, "st", fake_st)
    corpus = {
        "company": "삼성전자", "start_date": "2026-09-01", "end_date": "2026-09-30",
        "documents": [{**_document(), "parents": [{"content": "본문"}]}],
        "excluded": [],
        "warnings": [],
        "fingerprint": "ready",
    }
    monkeypatch.setattr(views.report_corpus, "list_companies", lambda: ["삼성전자"])
    monkeypatch.setattr(views.report_corpus, "load_report_corpus", lambda *_args: corpus)
    monkeypatch.setattr(views.synthesis_jobs, "list_jobs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(views.synthesis_jobs, "get_job",
                        lambda *_: {"id": "job-1", "status": "running"})
    calls = []
    monkeypatch.setattr(
        views.synthesis_jobs,
        "_start_corpus",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "job-1",
    )
    monkeypatch.setattr(views, "_workload_error", lambda _corpus: None)

    views.render_synthesis_page()

    assert len(calls) == 1
    assert calls[0][0][0] is corpus
    assert calls[0][1]["owner_id"] == fake_st.session_state[views._OWNER_KEY]
    assert fake_st.session_state[views._JOB_KEY] == "job-1"
    assert not any(label == "조회" for label, _ in fake_st.buttons)


def test_opening_completed_history_does_not_allow_another_running_job(monkeypatch):
    fake_st = _FakeStreamlit(pressed={"종합 생성"})
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views.report_corpus, "list_companies", lambda: ["삼성전자"])
    monkeypatch.setattr(views.report_corpus, "load_report_corpus", lambda *_args: {
        "company": "삼성전자", "start_date": "2026-09-01", "end_date": "2026-09-30",
        "documents": [_document()], "excluded": [], "warnings": [], "fingerprint": "ready",
    })
    monkeypatch.setattr(views, "_latest_owned_job", lambda _owner: {"id": "old", "status": "completed"})
    monkeypatch.setattr(views.synthesis_jobs, "list_jobs", lambda *_args, **_kwargs: [
        {"id": "new", "status": "running"}, {"id": "old", "status": "completed"},
    ])
    monkeypatch.setattr(views, "_render_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(views, "_workload_error", lambda _corpus: None)
    calls = []
    monkeypatch.setattr(views.synthesis_jobs, "_start_corpus", lambda *_args, **_kwargs: calls.append(True))
    views.render_synthesis_page()
    assert not calls
    assert next(kwargs for label, kwargs in fake_st.buttons if label == "종합 생성")["disabled"]


def test_generation_explains_workload_limit_rejected_before_body_loading(monkeypatch):
    fake_st = _FakeStreamlit(pressed={"종합 생성"})
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views.report_corpus, "list_companies", lambda: ["삼성전자"])
    monkeypatch.setattr(views.synthesis_jobs, "list_jobs", lambda *_args, **_kwargs: [])

    def overlarge(*_args):
        raise ValueError("분석 범위가 실행 한도를 초과했습니다. 기간을 줄여 주세요.")

    monkeypatch.setattr(views.report_corpus, "load_report_corpus", overlarge)
    views.render_synthesis_page()
    assert any("기간을 줄여 주세요" in message for kind, message in fake_st.messages if kind == "warning")
    assert not next(kwargs for label, kwargs in fake_st.buttons if label == "종합 생성")["disabled"]


def test_page_does_not_query_before_scope_submission(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views.report_corpus, "list_companies", lambda: ["삼성전자"])
    monkeypatch.setattr(views.synthesis_jobs, "list_jobs", lambda *_args, **_kwargs: [])
    queries = []
    monkeypatch.setattr(views.report_corpus, "load_report_corpus", lambda *args: queries.append(args))

    views.render_synthesis_page()

    assert queries == []
    assert fake_st.forms == [("synthesis_scope_form", {"enter_to_submit": False})]
    assert not next(kwargs for label, kwargs in fake_st.buttons if label == "종합 생성")["disabled"]


def test_generation_reads_submitted_scope_once_instead_of_reusing_old_preview(monkeypatch):
    fake_st = _FakeStreamlit(pressed={"종합 생성"})
    confirmed = {"company": "삼성전자", "start_date": "2026-01-01", "end_date": "2026-01-31",
                 "documents": [{**_document(), "parents": [{"content": "본문"}]}],
                 "excluded": [], "warnings": [], "fingerprint": "confirmed"}
    fake_st.session_state[views._PREVIEW_KEY] = confirmed
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views.report_corpus, "list_companies", lambda: ["다른 회사"])
    monkeypatch.setattr(views.synthesis_jobs, "list_jobs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(views, "_workload_error", lambda _: None)
    queries, started, source_checks = [], [], []
    fresh = {**confirmed, "company": "다른 회사", "fingerprint": "fresh"}

    def load(company, start, end):
        queries.append((company, start, end))
        fresh.update(start_date=start, end_date=end)
        return fresh

    monkeypatch.setattr(views.report_corpus, "load_report_corpus", load)
    monkeypatch.setattr(views.synthesis_jobs, "_start_corpus", lambda corpus, **kwargs: started.append(corpus) or "new-job")
    monkeypatch.setattr(views, "_render_job", lambda owner, **kwargs: source_checks.append(kwargs["current_corpus"]))

    views.render_synthesis_page()

    assert len(queries) == 1 and queries[0][0] == "다른 회사"
    assert started == [fresh]
    assert source_checks == [fresh]
    fake_st.pressed.clear()
    views.render_synthesis_page()
    assert len(queries) == 1 and len(started) == 1
    assert source_checks[-1] is None  # Stored scope cannot certify current source identity.


@pytest.mark.parametrize("phase", ["lookup", "submission"])
@pytest.mark.parametrize("error", [OSError("private storage details"), sqlite3.DatabaseError("private storage details")])
def test_one_click_storage_failure_does_not_start_work_or_expose_details(monkeypatch, phase, error):
    fake_st = _FakeStreamlit(pressed={"종합 생성"})
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views.report_corpus, "list_companies", lambda: ["Alpha"])
    monkeypatch.setattr(views, "_latest_owned_job", lambda _: None)
    monkeypatch.setattr(views.synthesis_jobs, "list_jobs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(views, "_render_job", lambda *_args, **_kwargs: None)
    corpus = {"company": "Alpha", "start_date": "2026-09-01", "end_date": "2026-09-30",
              "documents": [{**_document(), "parents": [{"content": "본문"}]}],
              "excluded": [], "warnings": [], "fingerprint": "ready"}

    def unavailable(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(views.report_corpus, "load_report_corpus",
                        unavailable if phase == "lookup" else lambda *_: corpus)
    submissions = []

    def start(*args, **kwargs):
        submissions.append(args)
        raise error

    monkeypatch.setattr(views.synthesis_jobs, "_start_corpus", start)
    views.render_synthesis_page()
    assert len(submissions) == (0 if phase == "lookup" else 1)
    assert views._JOB_KEY not in fake_st.session_state
    assert any(kind in {"warning", "error"} for kind, _ in fake_st.messages)
    assert all("private storage details" not in message for _, message in fake_st.messages)
    assert not fake_st.reruns


def test_background_completion_refreshes_app_without_confirmation(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    completed = {"id": "job", "status": "completed", "result": _result()}
    statuses = iter([{"id": "job", "status": "running"}, completed])
    monkeypatch.setattr(views, "_latest_owned_job", lambda _: next(statuses))
    rendered = []
    monkeypatch.setattr(
        views,
        "_render_result",
        lambda result, **kwargs: rendered.append((result, kwargs)),
    )
    views._render_job("owner")

    assert fake_st.reruns == [{"scope": "app"}]
    assert rendered == [(completed["result"], {"current_corpus": None})]
    assert not any(label == "결과 확인" for label, _ in fake_st.buttons)


def test_background_preparing_phase_uses_body_preparation_copy(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    statuses = iter([
        {"id": "job", "status": "running", "phase": "preparing", "completed": 0, "total": 2},
        {"id": "job", "status": "running", "phase": "preparing", "completed": 1, "total": 2},
    ])
    monkeypatch.setattr(views, "_latest_owned_job", lambda _: next(statuses))

    views._render_job("owner")

    assert any("본문 준비 · 1/2건 처리" in message for _, message in fake_st.messages)


@pytest.mark.parametrize("status", ["completed", "partial", "failed", "empty"])
def test_terminal_refresh_disables_polling_on_the_next_app_run(monkeypatch, status):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    schedules = []

    def fragment(**kwargs):
        schedules.append(kwargs["run_every"])
        return lambda function: function

    monkeypatch.setattr(fake_st, "fragment", fragment)
    terminal = {"id": "job", "status": status, "result": _result(status=status)}
    states = iter([{"id": "job", "status": "running"}, terminal, terminal, terminal])
    monkeypatch.setattr(views, "_latest_owned_job", lambda _owner: next(states))
    monkeypatch.setattr(views, "_render_result", lambda *_args, **_kwargs: None)
    views._render_job("owner")
    views._render_job("owner")
    assert schedules == [2.0, None]
    assert fake_st.reruns == [{"scope": "app"}]


def test_background_failure_shows_manual_retry_without_retrying_automatically(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    statuses = iter([
        {"id": "job", "status": "running"},
        {"id": "job", "status": "failed", "error": "모델 응답 형식 검증 실패"},
    ])
    monkeypatch.setattr(views, "_latest_owned_job", lambda _: next(statuses))
    retried = []
    monkeypatch.setattr(
        views.synthesis_jobs,
        "retry_job",
        lambda job_id, owner_id: retried.append((job_id, owner_id)) or "retry-2",
    )

    views._render_job("owner")

    assert retried == []
    assert fake_st.reruns == [{"scope": "app"}]
    assert ("error", "모델 응답 형식 검증 실패") in fake_st.messages
    assert any("자동으로 재시도하지 않습니다" in message for _, message in fake_st.messages)
    assert any(label == "실패한 작업 다시 시도" for label, _ in fake_st.buttons)
    assert not any(label == "결과 확인" for label, _ in fake_st.buttons)


def test_background_empty_shows_stored_result_without_confirmation_or_retry(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    empty_result = _result(status="empty", documents=[])
    empty = {"id": "job", "status": "empty", "result": empty_result}
    statuses = iter([{"id": "job", "status": "running"}, empty])
    monkeypatch.setattr(views, "_latest_owned_job", lambda _: next(statuses))
    rendered = []
    monkeypatch.setattr(
        views,
        "_render_result",
        lambda result, **kwargs: rendered.append((result, kwargs)),
    )

    views._render_job("owner")

    assert rendered == [(empty_result, {"current_corpus": None})]
    assert any("분석 가능한 리포트가 없습니다" in message for _, message in fake_st.messages)
    assert fake_st.reruns == [{"scope": "app"}]
    assert not any("다시 시도" in label or label == "결과 확인" for label, _ in fake_st.buttons)


@pytest.mark.parametrize("polling", [True, False])
def test_failure_explains_stored_diagnostics_without_exposing_internal_data(monkeypatch, polling):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    failed = {"id": "job", "status": "failed", "error": "종합 작성 실패", "result": {
        "diagnostic": {"category": "evidence_mismatch", "private_payload": "secret-body"},
    }}
    statuses = iter([{"id": "job", "status": "running"} if polling else failed, failed])
    monkeypatch.setattr(views, "_latest_owned_job", lambda _: next(statuses))
    monkeypatch.setattr(views, "_render_result", lambda *_args, **_kwargs: None)
    views._render_job("owner")
    assert any("원문 인용 불일치" in message for _, message in fake_st.messages)
    assert not any("secret-body" in message for _, message in fake_st.messages)
    assert any(label == "실패한 작업 다시 시도" for label, _ in fake_st.buttons)


def test_background_partial_manual_retry_starts_only_after_click(monkeypatch):
    fake_st = _FakeStreamlit(pressed={"미완료 문서 다시 시도"})
    monkeypatch.setattr(views, "st", fake_st)
    partial = {"id": "job", "status": "partial", "result": _result(status="partial", failed=1)}
    statuses = iter([partial, partial])
    monkeypatch.setattr(views, "_latest_owned_job", lambda _: next(statuses))
    monkeypatch.setattr(views, "_render_result", lambda *_args, **_kwargs: None)
    retried = []
    monkeypatch.setattr(
        views.synthesis_jobs,
        "retry_job",
        lambda job_id, owner_id: retried.append((job_id, owner_id)) or "retry-2",
    )

    views._render_job("owner")

    assert retried == [("job", "owner")]
    assert fake_st.session_state[views._JOB_KEY] == "retry-2"
    assert fake_st.reruns == [{"scope": "app"}]


def test_background_partial_shows_result_and_retry_without_automatic_action(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    partial = {"id": "job", "status": "partial", "result": _result(status="partial", failed=1)}
    statuses = iter([{"id": "job", "status": "running"}, partial])
    monkeypatch.setattr(views, "_latest_owned_job", lambda _: next(statuses))
    rendered = []
    monkeypatch.setattr(
        views,
        "_render_result",
        lambda result, **kwargs: rendered.append((result, kwargs)),
    )
    retried = []
    monkeypatch.setattr(
        views.synthesis_jobs,
        "retry_job",
        lambda job_id, owner_id: retried.append((job_id, owner_id)) or "retry-2",
    )

    current_corpus = {"fingerprint": "current-scope"}
    views._render_job("owner", current_corpus=current_corpus)

    assert retried == []
    assert fake_st.reruns == [{"scope": "app"}]
    assert rendered == [(partial["result"], {"current_corpus": current_corpus})]
    assert any(label == "미완료 문서 다시 시도" for label, _ in fake_st.buttons)
    assert not any(label == "결과 확인" for label, _ in fake_st.buttons)


def test_background_citation_partial_reports_its_actual_cause(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views, "_current_fingerprint", lambda _result: "fixed-scope")
    monkeypatch.setattr(views, "_verified_pdf", lambda _document: (None, "없음"))
    result = _raw_result(status="partial")
    result["coverage"] = {"prepared": 2, "cited": 1, "uncited": 1}
    statuses = iter([{"id": "job", "status": "running"},
                     {"id": "job", "status": "partial", "result": result}])
    monkeypatch.setattr(views, "_latest_owned_job", lambda _: next(statuses))
    views._render_job("owner")
    warnings = [message for kind, message in fake_st.messages if kind == "warning"]
    assert any("대상 리포트 중 1건은 최종 분석에 반영되지 않았습니다." in message
               for kind, message in fake_st.messages if kind == "caption")
    assert all("최종 인용 근거로 채택되지 않아" not in message for message in warnings)
    assert all("준비 제외" not in message for message in warnings)
    assert fake_st.reruns == [{"scope": "app"}]


def test_optional_comparison_partial_shows_real_cause_in_result_and_polling(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views, "_current_fingerprint", lambda _result: "fixed-scope")
    monkeypatch.setattr(views, "_verified_pdf", lambda _document: (None, "없음"))
    result = _raw_result(status="partial")
    result["coverage"] = {"prepared": 1, "cited": 1, "uncited": 0}
    result["validation_warnings"] = [{"stage": "report", "section": "changes", "reason": "broker_changes"}]
    views._render_result(result)
    monkeypatch.setattr(views, "_render_result", lambda *_args, **_kwargs: None)
    job = {"id": "optional-partial", "status": "partial", "result": result}
    statuses = iter([{"id": "optional-partial", "status": "running"}, job])
    monkeypatch.setattr(views, "_latest_owned_job", lambda _: next(statuses))
    views._render_job("owner")
    warnings = [message for kind, message in fake_st.messages if kind == "warning"]
    assert all("비교 근거" not in message for message in warnings)
    assert all("준비 제외" not in message for message in warnings)
    assert any(label == "부분 종합 다시 생성" for label, _ in fake_st.buttons)
    assert fake_st.reruns == [{"scope": "app"}]


def test_background_partial_reports_combined_causes_without_hiding_omissions(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views, "_current_fingerprint", lambda _result: "fixed-scope")
    monkeypatch.setattr(views, "_verified_pdf", lambda _document: (None, "없음"))
    result = _raw_result(status="partial")
    result["counts"].update(total=4, completed=3, failed=1)
    result["coverage"] = {"prepared": 3, "cited": 1, "uncited": 2}
    result["validation_warnings"] = [{"stage": "report", "section": "changes", "reason": "broker_changes"}]
    statuses = iter([{"id": "job", "status": "running"}, {"id": "job", "status": "partial", "result": result}])
    monkeypatch.setattr(views, "_latest_owned_job", lambda _: next(statuses))
    views._render_job("owner")
    warnings = [message for kind, message in fake_st.messages if kind == "warning"]
    assert all("비교 근거" not in message for message in warnings)
    assert any("대상 리포트 중 2건은 최종 분석에 반영되지 않았습니다." in message
               for kind, message in fake_st.messages if kind == "caption")
    assert any("본문을 읽지 못" in message for message in warnings)


def test_partial_and_single_result_link_citation_and_show_status(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views, "_current_fingerprint", lambda _result: "fixed-scope")
    monkeypatch.setattr(views, "_verified_pdf", lambda _document: (None, "없음"))

    views._render_result(_result(status="partial", failed=1, documents=[
        _document(), _document(report_uid="failed-doc", status="failed", analysis=None),
    ]))

    assert any("일부 리포트를 분석하지 못해" in message for _, message in fake_st.messages)
    assert any("증권사 간 합의를 판단할 수 없습니다" in message for _, message in fake_st.messages)
    assert any("#synthesis-result-source-1" in markdown for markdown in fake_st.markdowns)
    assert any("id='synthesis-result-source-1'" in markdown for markdown in fake_st.markdowns)


def test_result_presents_sources_as_a_collapsed_evidence_appendix(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views, "_current_fingerprint", lambda _result: "fixed-scope")
    monkeypatch.setattr(views, "_verified_pdf", lambda _document: (None, "없음"))

    views._render_result(_result())

    assert ("근거 부록 · 분석한 문서 (1개)", {"expanded": False}) in fake_st.expanders
    copy = "\n".join(message for _, message in fake_st.messages)
    assert "기간 내 기업 흐름 리포트" in copy
    assert "근거 부록 · 전체 대상 리포트" not in copy
    assert "리포트 발행일은 사건 발생일과 다를 수 있습니다" in copy
    assert "이전 작성 방식" not in copy


def test_result_uses_scoped_reading_measure_without_trusting_generated_html(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views, "_current_fingerprint", lambda _result: "fixed-scope")
    monkeypatch.setattr(views, "_verified_pdf", lambda _document: (None, "없음"))
    result = _result()
    result["content"] = "## 핵심 판단\n영업이익률은 5~10% 범위다. [1]"

    views._render_result(result)

    assert {"key": "synthesis_report_body"} in fake_st.containers
    assert {"key": "synthesis_report_metrics"} in fake_st.containers
    style, style_kwargs = next(
        (body, kwargs)
        for body, kwargs in fake_st.markdown_calls
        if ".st-key-synthesis_report_body" in body
    )
    assert "max-width: 88ch" in style
    assert "line-height: 1.8" in style
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in style
    assert '[data-testid="stColumn"]' in style
    assert "h3:first-child" in style
    assert "margin-top: 0" in style
    assert style_kwargs == {"unsafe_allow_html": True}
    report, report_kwargs = next(
        (body, kwargs)
        for body, kwargs in fake_st.markdown_calls
        if "영업이익률" in body
    )
    assert "5\\~10%" in report
    assert report_kwargs == {}
    assert result["content"] == "## 핵심 판단\n영업이익률은 5~10% 범위다. [1]"


def test_exact_raw_report_wrapper_is_hidden_without_changing_content_or_citations(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views, "_current_fingerprint", lambda _result: "fixed-scope")
    monkeypatch.setattr(views, "_verified_pdf", lambda _document: (None, "없음"))
    result = _raw_result()
    result["coverage"] = {"prepared": 1, "cited": 1, "uncited": 0}
    result["content"] = (
        "## 삼성전자 기간 내 리포트 종합\n\n"
        "2026-09-01 ~ 2026-09-30 · 본문 준비 1건 · 인용 근거 1건 · 1개 증권사\n\n"
        "### 핵심 판단\n\n영업이익률은 5~10% 범위다. [1]"
    )
    canonical = result["content"]

    views._render_result(result)

    report = next(body for body in fake_st.markdowns if "영업이익률" in body)
    assert report.startswith("### 핵심 판단")
    assert "## 삼성전자 기간 내 리포트 종합" not in report
    assert "본문 준비 1건 · 인용 근거 1건" not in report
    assert "#synthesis-result-source-1" in report
    assert "5\\~10%" in report
    assert result["content"] == canonical


def test_unknown_legacy_report_heading_is_not_stripped(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views, "_current_fingerprint", lambda _result: "fixed-scope")
    monkeypatch.setattr(views, "_verified_pdf", lambda _document: (None, "없음"))
    result = _result()
    result["content"] = (
        "## 삼성전자 기간 내 리포트 종합\n\n"
        "알 수 없는 이전 형식의 메타데이터\n\n본문 [1]"
    )

    views._render_result(result)

    report = next(body for body in fake_st.markdowns if "알 수 없는 이전 형식" in body)
    assert report.startswith("## 삼성전자 기간 내 리포트 종합")


def test_result_keeps_all_five_metrics_inside_responsive_container(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views, "_current_fingerprint", lambda _result: "fixed-scope")
    monkeypatch.setattr(views, "_verified_pdf", lambda _document: (None, "없음"))
    result = _raw_result()
    result["counts"].update(total=5, completed=4, failed=1, excluded=2, brokers=3)

    views._render_result(result)

    assert {"key": "synthesis_report_metrics"} in fake_st.containers
    assert fake_st.metrics == [
        ("확인", "5건"),
        ("본문 준비", "4건"),
        ("준비 실패", "1건"),
        ("제외", "2건"),
        ("증권사", "3곳"),
    ]


def test_document_markdown_escapes_numeric_ranges_without_mutating_evidence(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views, "_verified_pdf", lambda _document: (None, "없음"))
    legacy = _document(analysis={"claims": [{
        "topic": "전망 1~2년",
        "claim": "매출은 10~20% 증가 전망이다.",
        "quote": "원문 성장률 8~12%",
    }]})
    raw = _document(
        report_uid="b" * 64,
        status="prepared",
        analysis=None,
        preparation={"total_segments": 1, "body_chars": 100},
    )
    evidence = {"report_uid": raw["report_uid"], "quote": "검증 인용 3~5%"}

    views._render_document_list(_result(documents=[legacy]), source_changed=False)
    views._render_document_list(
        _raw_result(documents=[raw], evidence=[evidence]),
        source_changed=False,
    )

    rendered = "\n".join(fake_st.markdowns)
    assert "전망 1\\~2년" in rendered
    assert "10\\~20%" in rendered
    assert "8\\~12%" in rendered
    assert "3\\~5%" in rendered
    assert evidence["quote"] == "검증 인용 3~5%"


def test_raw_body_result_shows_preparation_and_only_result_level_evidence(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views, "_current_fingerprint", lambda _result: "fixed-scope")
    monkeypatch.setattr(views, "_verified_pdf", lambda _document: (None, "없음"))
    document = _document(
        status="prepared",
        analysis={"claims": [{"claim": "이전 문서 분석", "quote": "이전 추출 인용"}]},
        preparation={"total_segments": 3, "body_chars": 4567},
        parents=[{"content": "노출되면 안 되는 원문"}],
    )
    result = _raw_result(
        documents=[document],
        evidence=[
            {
                "report_uid": document["report_uid"],
                "parent_uid": "parent-1",
                "start": 12,
                "end": 23,
                "topic": "실적",
                "claim": "최종 리포트의 검증된 주장",
                "quote": "원문과 일치하는 인용",
                "private_payload": "숨겨야 하는 내부 값",
            },
            {
                "report_uid": document["report_uid"],
                "parent_uid": "parent-1",
                "start": 12,
                "end": 23,
                "topic": "종합 결론",
                "claim": "다른 최종 문단에서도 같은 원문을 사용",
                "quote": "원문과 일치하는 인용",
            },
            {"report_uid": "other-document", "quote": "다른 문서 인용"},
        ],
    )

    views._render_result(result)

    assert ("본문 준비", "1건") in fake_st.metrics
    assert ("근거 부록 · 인용한 문서 (1개)", {"expanded": False}) in fake_st.expanders
    assert any("본문 준비 완료" in label for label, _ in fake_st.expanders)
    assert any("본문 4,567자 · 3개 구간 준비" in message for _, message in fake_st.messages)
    assert any("리포트에 사용한 원문 근거" in message for _, message in fake_st.messages)
    rendered = "\n".join(fake_st.markdowns)
    assert rendered.count("> 원문과 일치하는 인용") == 1
    assert "최종 리포트의 검증된 주장" not in rendered
    assert "이전 문서 분석" not in rendered
    assert "이전 추출 인용" not in rendered
    assert "다른 문서 인용" not in rendered
    assert "노출되면 안 되는 원문" not in str(fake_st.messages) + rendered
    assert "숨겨야 하는 내부 값" not in str(fake_st.messages) + rendered


def test_raw_body_followup_uses_prepared_documents():
    result = _raw_result(evidence=[])

    metadata = views._followup_metadata(result)

    assert metadata["document_uids"] == ["a" * 64]
    assert metadata["selected_sources"] == result["selected_sources"]


def test_appendix_shows_each_cited_document_once_with_anchor_and_pdf(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views, "_current_fingerprint", lambda _result: "fixed-scope")
    monkeypatch.setattr(views, "_verified_pdf", lambda _document: (Path("original.pdf"), ""))
    result = _raw_result()
    views._render_result(result)
    title = result["documents"][0]["title"]
    labels = [label for label, _ in fake_st.expanders if title in label]
    assert len(labels) == 1 and labels[0].startswith("[1] ")
    assert not any(title in message for kind, message in fake_st.messages if kind == "write")
    anchor = views.citations.source_anchor_id(views._ANCHOR_PREFIX, 1)
    assert sum(f"id='{anchor}'" in markup for markup in fake_st.markdowns) == 1
    assert sum(label == "원문 PDF 열기" for label, _ in fake_st.buttons) == 1
    assert not fake_st.downloads


def test_appendix_keeps_source_only_legacy_results(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views, "_verified_pdf", lambda _document: (None, "없음"))
    result = _raw_result()
    result["documents"] = []
    views._render_document_list(result, source_changed=False)
    assert len(fake_st.expanders) == 1
    assert fake_st.expanders[0][0].startswith("[1] ")


@pytest.mark.parametrize("reverse_documents", [False, True])
def test_appendix_orders_citations_before_separate_other_documents(monkeypatch, reverse_documents):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views, "_verified_pdf", lambda _document: (Path("original.pdf"), ""))
    cited = [
        _document(
            report_uid=f"{rank:064x}",
            title=f"인용 문서 {rank}",
            status="prepared",
            synthesis_status="cited",
        )
        for rank in range(1, 11)
    ]
    uncited = _document(
        report_uid="c" * 64, title="6월 29일 미인용 문서",
        status="prepared", synthesis_status="uncited", reason="최종 인용 미채택",
    )
    failed = _document(report_uid="d" * 64, title="본문 준비 실패 문서",
                       status="failed", reason="본문 읽기 실패")
    documents = [*cited[:9], uncited, cited[9], failed]
    if reverse_documents:
        documents.reverse()
    result = _raw_result(status="partial", documents=documents, failed=1)
    result["selected_sources"], result["citation_contract"] = (
        views.citations.annotate_document_citation_sources(cited)
    )
    result["coverage"] = {"prepared": 11, "cited": 10, "uncited": 1}
    saved_result = deepcopy(result)

    views._render_document_list(result, source_changed=False)

    labels = [label for label, _ in fake_st.expanders]
    assert [label.split(" ")[0] for label in labels[:10]] == [
        f"[{rank}]" for rank in range(1, 11)
    ]
    assert fake_st.expanders[10] == ("기타 대상 문서 (2개)", {"expanded": False})
    assert len(labels) == 13
    assert any("6월 29일 미인용 문서" in label and "최종 분석 미반영" in label
               for label in labels[11:])
    assert any("본문 준비 실패 문서" in label for label in labels[11:])
    assert ("caption", "사유: 본문 읽기 실패") in fake_st.messages
    assert sum("<div id=" in markup for markup in fake_st.markdowns) == 10
    for rank in range(1, 11):
        anchor = views.citations.source_anchor_id(views._ANCHOR_PREFIX, rank)
        assert sum(f"id='{anchor}'" in markup for markup in fake_st.markdowns) == 1
    pdf_keys = [options["key"] for label, options in fake_st.buttons if label == "원문 PDF 열기"]
    assert len(pdf_keys) == len(set(pdf_keys)) == 12
    assert views._followup_metadata(result)["document_uids"] == [
        document["report_uid"] for document in cited
    ]
    assert result == saved_result


def test_saved_raw_report_hides_only_legacy_renderer_notices(monkeypatch):
    # The process-persistent generator may still be the version loaded before
    # this UI update. Display must work without a new generator API.
    monkeypatch.delattr(views.report_synthesis, "display_report_content", raising=False)
    result = _raw_result()
    result["counts"].update(failed=0, excluded=0)
    result["coverage"] = {"prepared": 3, "cited": 1, "uncited": 2}
    body = "### 핵심 판단\n\n기업의 실적 개선 근거입니다. [1]"
    result["content"] = (
        body + "\n\n동일 증권사의 전망 변화를 판단할 근거가 부족합니다."
        "\n\n부분 종합: 준비 실패 0건, 준비 제외 0건, 인용 누락 2건. "
        "검증된 인용 근거가 확인된 범위의 부분 결과이며 전체 대상 자료의 종합으로 볼 수 없습니다."
    )
    assert views._display_report_content(result) == body
    assert views.display_report_content(result) == body
    assert result["coverage"]["uncited"] == 2
    # Genuine analytical prose containing the same words is preserved.
    result["content"] = "비교 근거가 부족한 사업은 추후 실적 확인이 필요합니다."
    assert views._display_report_content(result) == result["content"]


def test_uncited_partial_documents_are_visible_and_excluded_from_followup(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views, "_current_fingerprint", lambda _result: "fixed-scope")
    monkeypatch.setattr(views, "_verified_pdf", lambda _document: (None, "없음"))
    cited = _document(status="prepared", synthesis_status="cited")
    uncited = _document(report_uid="b" * 64, status="prepared", synthesis_status="uncited",
                        reason="최종 종합에서 검증된 원문 인용 근거로 채택되지 않았습니다.")
    result = _raw_result(status="partial", documents=[cited, uncited])
    result["selected_sources"] = [result["selected_sources"][0]]
    result["coverage"] = {"prepared": 2, "cited": 1, "uncited": 1}
    views._render_result(result)
    assert any("최종 분석 미반영" in label for label, _ in fake_st.expanders)
    assert any("대상 리포트 중 1건은 최종 분석에 반영되지 않았습니다." in message
               for kind, message in fake_st.messages if kind == "caption")
    assert not any("근거 부록 · 전체 대상 리포트" in message for _, message in fake_st.messages)
    assert not any(
        "1건의 문서가 최종 인용 근거로 채택되지 않아 부분 종합했습니다" in message
        for kind, message in fake_st.messages
        if kind == "warning"
    )
    assert views._followup_metadata(result)["document_uids"] == [cited["report_uid"]]


def test_uncited_partial_retry_describes_regeneration_and_waits_for_click(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    result = _raw_result(status="partial")
    result["coverage"] = {"prepared": 2, "cited": 1, "uncited": 1}
    job = {"id": "partial-uncited", "status": "partial", "result": result}
    retried = []
    monkeypatch.setattr(views.synthesis_jobs, "retry_job", lambda *_args: retried.append(_args) or "new-job")
    views._render_manual_retry(job, "owner", partial=True)
    assert not retried and any(label == "부분 종합 다시 생성" for label, _ in fake_st.buttons)
    fake_st.pressed.add("부분 종합 다시 생성")
    views._render_manual_retry(job, "owner", partial=True)
    assert retried == [("partial-uncited", "owner")]


def test_raw_body_evidence_deduplicates_same_origin_and_preserves_first_order():
    uid = "a" * 64
    result = _raw_result(evidence=[
        {"claim_id": "summary-1", "report_uid": uid, "parent_uid": "p1",
         "start": 10, "end": 20, "quote": "첫 원문"},
        {"claim_id": "flow-1", "report_uid": uid, "parent_uid": "p1",
         "start": 10, "end": 20, "quote": "첫 원문"},
        {"claim_id": "conclusion-1", "report_uid": uid, "parent_uid": "p2",
         "start": 30, "end": 40, "quote": "두 번째 원문"},
    ])

    evidence = views._result_evidence_by_document(result)[uid]

    assert [item["claim_id"] for item in evidence] == ["summary-1", "conclusion-1"]


def test_raw_body_partial_retry_copy_describes_synthesis_batch_reuse(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    job = {"id": "raw-partial", "status": "partial", "result": _raw_result(status="partial")}
    monkeypatch.setattr(views.synthesis_jobs, "retry_job", lambda *_args: "unexpected")

    views._render_manual_retry(job, "owner", partial=True)

    assert any("완료된 종합 묶음은 재사용" in message for _, message in fake_st.messages)
    assert any(label == "실패한 종합 다시 시도" for label, _ in fake_st.buttons)


def test_old_writer_result_prompts_manual_regeneration_without_starting_job(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views.report_synthesis, "REPORT_PROMPT_VERSION", "rdb-report-3")
    monkeypatch.setattr(views, "_current_fingerprint", lambda _result: "fixed-scope")
    monkeypatch.setattr(views, "_verified_pdf", lambda _document: (None, "없음"))
    started = []
    monkeypatch.setattr(
        views.synthesis_jobs,
        "_start_corpus",
        lambda *args, **kwargs: started.append((args, kwargs)),
    )

    views._render_result({**_result(), "prompt_version": "rdb-report-2"})

    assert started == []
    assert any(
        "이전 작성 방식" in message and "새 리포트를 생성" in message
        for _, message in fake_st.messages
    )


def test_result_reuses_preview_fingerprint_only_for_matching_scope(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views, "_verified_pdf", lambda _document: (None, "없음"))
    lookups = []
    monkeypatch.setattr(views, "_current_fingerprint", lambda result: lookups.append(result) or "fixed-scope")
    result = _result()
    preview = {key: result[key] for key in ("company", "start_date", "end_date", "fingerprint")}
    views._render_result(result, current_corpus=preview)
    assert not lookups
    preview["company"] = "다른 회사"
    views._render_result(result, current_corpus=preview)
    assert lookups == [result]


def test_document_analysis_renders_user_claims_without_internal_dict(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views, "_verified_pdf", lambda _document: (None, "없음"))
    result = _result(
        documents=[
            _document(
                analysis={
                    "cache_key": "internal-secret",
                    "claims": [
                        {"topic": "수요", "claim": "수요가 늘었습니다.", "quote": "수요 증가가 확인된다."}
                    ],
                    "failed_chunks": 1,
                    "errors": [{"category": "evidence_mismatch", "elapsed_ms": 20676,
                                "error_type": "internal-exception", "private_payload": "hidden-request"},
                               {"category": "connection", "elapsed_ms": 1200}],
                }
            )
        ]
    )

    views._render_document_list(result, source_changed=False)

    rendered = "\n".join(fake_st.markdowns)
    assert "수요가 늘었습니다." in rendered
    assert "> 수요 증가가 확인된다." in rendered
    assert "internal-secret" not in rendered
    assert any("본문 구간 1개" in message for _, message in fake_st.messages)
    assert any("인용이 원문과 일치하지 않음 · 20.7초" in message for _, message in fake_st.messages)
    assert any("모델 연결 오류 · 1.2초" in message for _, message in fake_st.messages)
    assert "hidden-request" not in str(fake_st.messages)
    assert "internal-exception" not in str(fake_st.messages)


def test_partial_job_can_retry_without_starting_a_duplicate(monkeypatch):
    fake_st = _FakeStreamlit(pressed={"미완료 문서 다시 시도"})
    fake_st.session_state[views._JOB_KEY] = "partial-1"
    monkeypatch.setattr(views, "st", fake_st)
    job = {"id": "partial-1", "status": "partial", "result": _result(status="partial", failed=1)}
    monkeypatch.setattr(views.synthesis_jobs, "get_job", lambda *_args: job)
    monkeypatch.setattr(views.synthesis_jobs, "list_jobs", lambda *_args, **_kwargs: [job])
    monkeypatch.setattr(views, "_current_fingerprint", lambda _result: "fixed-scope")
    monkeypatch.setattr(views, "_verified_pdf", lambda _document: (None, "없음"))
    retried = []
    monkeypatch.setattr(
        views.synthesis_jobs,
        "retry_job",
        lambda job_id, owner_id: retried.append((job_id, owner_id)) or "retry-2",
    )

    views._render_job("owner-1")

    assert retried == [("partial-1", "owner-1")]
    assert fake_st.session_state[views._JOB_KEY] == "retry-2"


def test_pdf_open_requires_canonical_in_root_and_matching_hash(tmp_path, monkeypatch):
    pdf_root = tmp_path / "downloaded"
    pdf_root.mkdir()
    payload = b"%PDF safe"
    pdf = pdf_root / "one.pdf"
    pdf.write_bytes(payload)
    monkeypatch.setattr(views.config_module, "REPORT_PDF_DIR", str(pdf_root))
    monkeypatch.setattr(views.config_module, "DATA_ROOT", str(tmp_path))
    document = _document(
        canonical_relative_path="downloaded/one.pdf",
        source_sha256=hashlib.sha256(payload).hexdigest(),
    )

    assert views._verified_pdf(document) == (pdf, "")
    assert views._safe_pdf_path({**document, "canonical_relative_path": "../one.pdf"}) is None
    changed, message = views._verified_pdf({**document, "source_sha256": "0" * 64})
    assert changed is None
    assert "변경" in message


@pytest.mark.parametrize("open_error", [None, "PDF를 열 수 없습니다."])
def test_appendix_open_uses_verified_original_path_without_download(tmp_path, monkeypatch, open_error):
    root = tmp_path / "downloaded"
    nested = root / "broker"
    nested.mkdir(parents=True)
    pdf = nested / "one.pdf"
    payload = b"%PDF original"
    pdf.write_bytes(payload)
    monkeypatch.setattr(views.config_module, "REPORT_PDF_DIR", str(root))
    monkeypatch.setattr(views.config_module, "DATA_ROOT", str(tmp_path))
    fake_st = _FakeStreamlit(pressed=["원문 PDF 열기"])
    monkeypatch.setattr(views, "st", fake_st)
    result = _raw_result()
    result["documents"][0].update(canonical_relative_path="downloaded/broker/one.pdf",
                                 source_sha256=hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(views, "_current_fingerprint", lambda _: result["fingerprint"])
    opened = []
    monkeypatch.setattr(views.chat_views, "_open_pdf_path",
                        lambda path: opened.append(path) or (open_error is None, open_error))
    views._render_result(result)
    assert opened == [pdf]
    assert not fake_st.downloads
    if open_error:
        assert ("warning", open_error) in fake_st.messages
    # A changed or missing file must never reach the OS viewer.
    for changed in (b"%PDF changed", None):
        if changed is None:
            pdf.unlink()
        else:
            pdf.write_bytes(changed)
        opened.clear()
        views._render_result(result)
        assert not opened


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_shared_pdf_opener_uses_system_viewer(monkeypatch, tmp_path, platform):
    chat = views.chat_views
    path = tmp_path / "원문 파일.pdf"
    calls = []
    monkeypatch.setattr(chat.sys, "platform", platform)
    monkeypatch.setattr(chat.os, "startfile", lambda target: calls.append(target), raising=False)
    monkeypatch.setattr(chat.subprocess, "Popen", lambda args: calls.append(args))
    assert chat._open_pdf_path(path) == (True, None)
    expected = str(path) if platform == "win32" else ["open" if platform == "darwin" else "xdg-open", str(path)]
    assert calls == [expected]


@pytest.mark.parametrize("remaining", [False, True])
def test_selected_job_deleted_by_another_tab_clears_its_scope(monkeypatch, remaining):
    fake_st = _FakeStreamlit()
    stale_keys = (views._JOB_KEY, views._SCOPE_KEY, views._COMPANY_KEY, views._PRESET_KEY,
                  views._DATE_RANGE_KEY, views._PREVIEW_KEY, views._PREVIEW_ERROR_KEY)
    fake_st.session_state.update({key: "deleted job" for key in stale_keys})
    fake_st.session_state.current_thread_id = "followup"
    monkeypatch.setattr(views, "st", fake_st)
    monkeypatch.setattr(views.synthesis_jobs, "get_job", lambda *_: None)
    replacement = {"id": "remaining-job", "company": "Beta", "start_date": "2026-08-01",
                   "end_date": "2026-08-31", "status": "completed"}
    monkeypatch.setattr(views.synthesis_jobs, "list_jobs",
                        lambda *_args, **_kwargs: [replacement] if remaining else [])
    assert views._latest_owned_job("owner") == (replacement if remaining else None)
    assert all(key not in fake_st.session_state for key in stale_keys if key != views._JOB_KEY)
    assert fake_st.session_state.get(views._JOB_KEY) == (replacement["id"] if remaining else None)
    assert fake_st.session_state.current_thread_id == "followup"
    assert fake_st.reruns == [{"scope": "app"}]


def test_followup_button_persists_exact_scope_and_switches_to_chat(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(views, "st", fake_st)
    created = []
    appended = []
    monkeypatch.setattr(
        views.conversation_store,
        "create_thread",
        lambda name: created.append(name) or "thread-1",
    )
    monkeypatch.setattr(
        views.conversation_store,
        "append_message",
        lambda *args: appended.append(args),
    )
    result = _result()

    views._open_followup_chat(result)

    assert created == ["삼성전자 리포트 종합"]
    metadata = appended[0][3]
    assert metadata["selected_sources"] == result["selected_sources"]
    assert metadata["citation_contract"] == result["citation_contract"]
    assert metadata["search_scope"]["search_filters"] == {
        "target_name": "삼성전자",
        "report_type": "company",
        "report_date_start": "2026-09-01",
        "report_date_end": "2026-09-30",
        "file_names": ["one.pdf"],
    }
    assert metadata["document_uids"] == ["a" * 64]
    assert fake_st.session_state.current_thread_id == "thread-1"
    assert fake_st.session_state.active_user_page == "Chat"
