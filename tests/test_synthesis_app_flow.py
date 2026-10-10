"""Exercise real Streamlit widgets and the background RDB synthesis pipeline."""

from __future__ import annotations

from datetime import date

import pytest
from streamlit.elements.lib import policies as widget_policies
from streamlit.testing.v1 import AppTest

from apps.gui import synthesis_jobs, synthesis_views
from src.configs import config
from src.core import conversation_store
from tests.retrieval.native_build_fixtures import _native_seed


def test_followup_from_synthesis_with_monitoring_widget_returns_to_chat(tmp_path):
    harness = tmp_path / "followup_monitoring_navigation.py"
    harness.write_text("""
import streamlit as st
from unittest.mock import patch
from apps.gui import sidebar_views, synthesis_views
from tests.test_synthesis_views import _result

st.session_state.setdefault('active_user_page', '기업 리포트 종합')
st.session_state.setdefault('active_monitoring_page', 'Monitoring')
st.session_state.setdefault('current_thread_id', 'thread-1')
threads = [{'id': 'thread-1', 'name': '대화', 'pinned': False}]
status = {'db': {'total_reports': 0, 'embedded_reports': 0}}
with patch.object(sidebar_views, 'load_threads', return_value=threads), \
     patch.object(sidebar_views, '_render_thread_row'), \
     patch.object(sidebar_views, '_render_synthesis_history'), \
     patch.object(sidebar_views.status_cache, 'get_data_status', return_value=status), \
     patch.object(sidebar_views.data_views, 'render_report_calendar'), \
     patch.object(sidebar_views.data_views, 'render_data_update_controls'):
    with st.sidebar:
        sidebar_views.render_sidebar('thread-1', monitoring_enabled=True, operator_monitoring_enabled=True)

if st.session_state.active_user_page == '기업 리포트 종합':
    if st.button('후속 질문 시작'):
        with patch.object(synthesis_views.conversation_store, 'create_thread', return_value='followup'), \
             patch.object(synthesis_views.conversation_store, 'append_message'):
            synthesis_views._open_followup_chat(_result())
else:
    st.write('CHAT PAGE')
""", encoding="utf-8")
    app = AppTest.from_file(str(harness)).run(timeout=30)
    assert not app.exception
    next(button for button in app.button if button.label == "후속 질문 시작").click().run(timeout=30)
    assert not app.exception
    assert app.session_state["active_user_page"] == "Chat"
    assert app.session_state["active_monitoring_page"] == "Chat"
    assert app.session_state["current_thread_id"] == "followup"



def test_report_financial_ranges_render_literally_without_changing_canonical_result(tmp_path):
    harness = tmp_path / "financial_range_display.py"
    harness.write_text("""
import streamlit as st
from unittest.mock import patch
from apps.gui import synthesis_views
from src.core import report_synthesis
from tests.test_report_synthesis import Model, corpus, document

class RangeWriter(Model):
    def invoke(self, messages):
        output = super().invoke(messages)
        for item in output['statements']:
            item['text'] = '고객사 10~11개, 로열티 4~5%, 침투율 30~40%, 손익 -5~-1%다.'
        return output

source = corpus([document(1, body='원문 범위 4~5%와 30~40%를 설명합니다.'), document(2)])
result = report_synthesis.synthesize_corpus(source, model=RangeWriter())
st.session_state['canonical_content'] = result['content']
st.session_state['canonical_quotes'] = [item['quote'] for item in result['evidence']]
with patch.object(synthesis_views, '_current_fingerprint', return_value=result['fingerprint']), \\
     patch.object(synthesis_views, '_verified_pdf', return_value=(None, '검증용 원문')):
    synthesis_views._render_result(result)
""", encoding="utf-8")
    app = AppTest.from_file(str(harness)).run(timeout=30)
    assert not app.exception
    canonical = app.session_state['canonical_content']
    assert '10~11개' in canonical and r'10\~11개' not in canonical
    rendered = '\n'.join(item.value for item in app.markdown)
    for marker in [r'10\~11개', r'4\~5%', r'30\~40%', r'-5\~-1%']:
        assert marker in rendered
    assert any('4~5%' in quote and r'4\~5%' not in quote
               for quote in app.session_state['canonical_quotes'])
    assert '#synthesis-result-source-1' in rendered


def test_confirmed_scope_survives_leaving_and_reopening_synthesis(tmp_path, monkeypatch):
    monkeypatch.setattr(synthesis_views.report_corpus, "list_companies", lambda: ["A", "B"])
    monkeypatch.setattr(synthesis_jobs, "list_jobs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(synthesis_views.report_corpus, "load_report_corpus", lambda company, start, end: {
        "company": company, "start_date": start, "end_date": end,
        "documents": [], "excluded": [], "warnings": [], "fingerprint": "confirmed",
    })
    harness = tmp_path / "return_to_synthesis.py"
    harness.write_text("""
import streamlit as st
from apps.gui.synthesis_views import render_synthesis_page
if st.checkbox("Show synthesis", value=True):
    render_synthesis_page()
""", encoding="utf-8")
    app = AppTest.from_file(str(harness)).run(timeout=30)
    app.selectbox[0].set_value("B")
    app.radio[0].set_value("직접 선택")
    selected = (date(2026, 1, 1), date(2026, 1, 31))
    app.date_input[0].set_value(selected)
    next(button for button in app.button if button.label == "종합 생성").click().run(timeout=30)
    app.checkbox[0].set_value(False).run(timeout=30)
    monkeypatch.setattr(widget_policies, "_shown_default_value_warning", False)
    app.checkbox[0].set_value(True).run(timeout=30)
    assert not app.exception
    assert not any("Session State API" in warning.value for warning in app.warning)
    assert app.selectbox[0].value == "B"
    assert app.radio[0].value == "직접 선택"
    assert app.date_input[0].value == selected


def test_recovered_job_restores_scope_instead_of_defaulting_to_recent_month(tmp_path, monkeypatch):
    monkeypatch.setattr(widget_policies, "_shown_default_value_warning", False)
    job = {"id": "job", "status": "failed", "company": "B",
           "start_date": "2026-01-01", "end_date": "2026-01-31", "error": "interrupted"}
    monkeypatch.setattr(synthesis_views.report_corpus, "list_companies", lambda: ["A", "B"])
    monkeypatch.setattr(synthesis_views, "_latest_owned_job", lambda _: job)
    harness = tmp_path / "recovered_scope.py"
    harness.write_text("from apps.gui.synthesis_views import render_synthesis_page\nrender_synthesis_page()\n", encoding="utf-8")
    app = AppTest.from_file(str(harness)).run(timeout=30)
    assert not app.exception
    assert not any("Session State API" in warning.value for warning in app.warning)
    assert app.selectbox[0].value == "B"
    assert app.radio[0].value == "직접 선택"
    assert app.date_input[0].value == (date(2026, 1, 1), date(2026, 1, 31))


@pytest.mark.parametrize("selected", [None, (date(2026, 1, 1), date(2026, 1, 31)),
                                     (date(2026, 1, 1),), ()])
def test_date_widget_uses_session_state_without_duplicate_default(tmp_path, monkeypatch, selected):
    monkeypatch.setattr(widget_policies, "_shown_default_value_warning", False)
    monkeypatch.setattr(synthesis_views.report_corpus, "list_companies", lambda: ["A"])
    monkeypatch.setattr(synthesis_jobs, "list_jobs", lambda *_args, **_kwargs: [])
    harness = tmp_path / "date_state.py"
    harness.write_text("from apps.gui.synthesis_views import render_synthesis_page\nrender_synthesis_page()\n",
                       encoding="utf-8")
    app = AppTest.from_file(str(harness))
    if selected is not None:
        app.session_state["synthesis_date_range"] = selected
    app.run(timeout=30)
    expected = selected if selected is not None else synthesis_views._preset_range("최근 1개월")
    assert not app.exception
    assert not any("Session State API" in warning.value for warning in app.warning)
    assert app.date_input[0].value == expected
    assert app.date_input[0].proto.is_range
    app.run(timeout=30)
    assert not app.exception
    assert app.date_input[0].value == expected
    assert not any("Session State API" in warning.value for warning in app.warning)


def test_real_widgets_generate_rdb_result_and_open_scoped_chat(tmp_path, monkeypatch):
    root, sources = _native_seed(tmp_path, seed_matches_current_source=True)
    monkeypatch.setattr(config, "DATA_ROOT", str(root))
    monkeypatch.setattr(config, "REPORT_PDF_DIR", str(sources))
    monkeypatch.setattr(conversation_store, "CONVERSATION_DB_PATH", str(tmp_path / "conversations.sqlite3"))
    harness = tmp_path / "synthesis_harness.py"
    harness.write_text("""
import streamlit as st
from unittest.mock import patch
from apps.gui import synthesis_jobs, synthesis_views
from src.core import report_synthesis
from tests.test_report_synthesis import Model

# Save the real function before patching the module shared by both imports.
original = report_synthesis.synthesize_corpus
def generate(corpus, *, on_progress=None, cache=None):
    return original(corpus, on_progress=on_progress, cache=cache, model=Model())
with patch.object(synthesis_jobs.report_synthesis, 'synthesize_corpus', side_effect=generate):
    synthesis_views.render_synthesis_page()
""", encoding="utf-8")
    app = AppTest.from_file(str(harness)).run(timeout=30)
    assert not app.exception
    assert app.header[0].value == "기업 리포트 종합"
    assert not next(button for button in app.button if button.label == "종합 생성").disabled
    app.radio[0].set_value("직접 선택")
    app.date_input[0].set_value((date(2026, 1, 1), date(2026, 1, 31)))
    generate_button = next(button for button in app.button if button.label == "종합 생성")
    assert not generate_button.disabled
    generate_button.click().run(timeout=30)
    assert not app.exception
    job_id = app.session_state["synthesis_current_job_id"]
    owner = app.session_state["synthesis_owner_id"]
    with synthesis_jobs._LOCK:
        thread = synthesis_jobs._THREADS.get(job_id)
    if thread is not None:
        thread.join(15)
        assert not thread.is_alive()
    app.run(timeout=30)
    assert not app.exception
    result = synthesis_jobs.get_job(job_id, owner)["result"]
    assert result["status"] == "completed"
    assert result["counts"]["completed"] == 1
    assert result["selected_sources"][0]["target_name"] == "A"
    assert "단일 리포트를 근거" in result["content"]
    assert "종합 결론" in result["content"]
    assert any("종합 결론" in item.value for item in app.markdown)
    followup = next(button for button in app.button if button.label == "이 범위에서 질문하기")
    assert not followup.disabled
    followup.click().run(timeout=30)
    assert not app.exception
    assert app.session_state["active_user_page"] == "Chat"
    messages = conversation_store.list_messages(app.session_state["current_thread_id"])
    assert messages[-1]["metadata"]["search_scope"]["search_filters"]["report_date_start"] == "2026-01-01"
    assert messages[-1]["metadata"]["document_uids"] == [result["selected_sources"][0]["report_uid"]]


def test_sidebar_navigation_can_return_to_chat_after_widget_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(conversation_store, "CONVERSATION_DB_PATH", str(tmp_path / "conversations.sqlite3"))
    harness = tmp_path / "sidebar_harness.py"
    harness.write_text("""
import streamlit as st
from unittest.mock import patch
from apps.gui import sidebar_views
from src.core import conversation_store
threads=sidebar_views.load_threads()
sidebar_views.ensure_current_thread(threads)
with patch.object(sidebar_views.status_cache, 'get_data_status', return_value={
    'db': {'total_reports': 0, 'embedded_reports': 0}}), \
    patch.object(sidebar_views.data_views, 'render_report_calendar'), \
    patch.object(sidebar_views.data_views, 'render_data_update_controls'):
    sidebar_views.render_sidebar(st.session_state.current_thread_id, monitoring_enabled=True)
st.write(st.session_state.get('active_user_page'))
""", encoding="utf-8")
    app = AppTest.from_file(str(harness)).run(timeout=30)
    assert not app.exception
    assert "Monitoring" not in app.radio[1].options  # Production operator gate stays closed.
    app.radio[0].set_value("기업 리포트 종합").run(timeout=30)
    assert not app.exception and app.session_state["active_user_page"] == "기업 리포트 종합"
    next(button for button in app.button if button.label == "새 대화 시작").click().run(timeout=30)
    assert not app.exception
    assert app.session_state["active_user_page"] == "Chat"
    assert app.session_state["active_monitoring_page"] == "Chat"


def test_scope_form_applies_all_edits_only_when_generation_is_submitted(tmp_path, monkeypatch):
    queries = []
    monkeypatch.setattr(synthesis_views.report_corpus, "list_companies", lambda: ["A", "B"])
    monkeypatch.setattr(synthesis_jobs, "list_jobs", lambda *_args, **_kwargs: [])

    def load(company, start, end):
        queries.append((company, start, end))
        return {"company": company, "start_date": start, "end_date": end,
                "documents": [], "excluded": [], "warnings": [], "fingerprint": "confirmed"}

    monkeypatch.setattr(synthesis_views.report_corpus, "load_report_corpus", load)
    harness = tmp_path / "scope_form_harness.py"
    harness.write_text("from apps.gui.synthesis_views import render_synthesis_page\nrender_synthesis_page()\n", encoding="utf-8")
    app = AppTest.from_file(str(harness)).run(timeout=30)
    assert not app.exception
    assert not queries
    form = app.get("form")[0]
    assert not form.proto.form.enter_to_submit
    assert app.selectbox[0].proto.form_id == form.proto.form.form_id
    assert app.radio[0].proto.form_id == form.proto.form.form_id
    assert app.date_input[0].proto.form_id == form.proto.form.form_id

    app.selectbox[0].set_value("B")
    app.radio[0].set_value("직접 선택")
    app.date_input[0].set_value((date(2026, 1, 1), date(2026, 1, 31)))
    app.run(timeout=30)
    assert not app.exception and not queries
    next(button for button in app.button if button.label == "종합 생성").click().run(timeout=30)
    assert not app.exception
    assert queries == [("B", "2026-01-01", "2026-01-31")]

    app.selectbox[0].set_value("A")
    app.date_input[0].set_value((date(2026, 2, 1), date(2026, 2, 28)))
    app.run(timeout=30)
    assert not app.exception
    assert queries == [("B", "2026-01-01", "2026-01-31")]
    assert app.session_state[synthesis_views._PREVIEW_KEY]["company"] == "B"
    assert any("생성 요청 조건: B" in caption.value for caption in app.caption)
    next(button for button in app.button if button.label == "종합 생성").click().run(timeout=30)
    assert queries[-1] == ("A", "2026-02-01", "2026-02-28")

    app.date_input[0].set_value((date(2026, 3, 1),))
    next(button for button in app.button if button.label == "종합 생성").click().run(timeout=30)
    assert not app.exception
    assert len(queries) == 2
    assert not next(button for button in app.button if button.label == "종합 생성").disabled
    assert any("시작일과 종료일을 모두" in warning.value for warning in app.warning)


def test_running_job_refreshes_once_and_preserves_draft(tmp_path):
    harness = tmp_path / "completed_result_harness.py"
    harness.write_text("""
from datetime import date
import streamlit as st
from unittest.mock import patch
from apps.gui import synthesis_views
from tests.test_synthesis_views import _result

st.session_state["script_runs"] = st.session_state.get("script_runs", 0) + 1
st.session_state.setdefault(synthesis_views._COMPANY_KEY, "B")
st.session_state.setdefault(synthesis_views._PRESET_KEY, "직접 선택")
st.session_state.setdefault(
    synthesis_views._DATE_RANGE_KEY,
    (date(2026, 2, 1), date(2026, 2, 28)),
)
result = _result(status="completed")
states = st.session_state.setdefault("job_states", iter([
    {"id": "completed-job", "status": "running"},
    {"id": "completed-job", "status": "running"},
    {"id": "completed-job", "status": "completed", "result": result},
]))

with patch.object(synthesis_views, "_session_owner_id", return_value="owner"), \
     patch.object(synthesis_views.report_corpus, "list_companies", return_value=["A", "B"]), \
     patch.object(synthesis_views, "_latest_owned_job", side_effect=lambda _owner: next(states, {"id": "terminal", "status": result["status"], "result": result})), \
     patch.object(synthesis_views, "_current_fingerprint", return_value="fixed-scope"), \
     patch.object(synthesis_views, "_verified_pdf", return_value=(None, "원문 없음")):
    synthesis_views.render_synthesis_page()
""", encoding="utf-8")

    app = AppTest.from_file(str(harness)).run(timeout=30)

    assert not app.exception
    assert app.session_state["script_runs"] == 2
    assert app.session_state[synthesis_views._COMPANY_KEY] == "B"
    assert app.session_state[synthesis_views._PRESET_KEY] == "직접 선택"
    assert tuple(app.session_state[synthesis_views._DATE_RANGE_KEY]) == (
        date(2026, 2, 1),
        date(2026, 2, 28),
    )
    assert any("핵심 내용" in item.value for item in app.markdown)
    assert any("근거 부록 · 분석한 문서" in item.label for item in app.expander)
    assert any(button.label == "이 범위에서 질문하기" for button in app.button)
    assert all(button.label != "조회" for button in app.button)
    assert any(button.label == "종합 생성" for button in app.button)
    assert all(button.label != "결과 확인" for button in app.button)


def test_running_job_displays_partial_result_without_confirmation_or_retry(tmp_path):
    harness = tmp_path / "partial_result_harness.py"
    harness.write_text("""
import streamlit as st
from unittest.mock import patch
from apps.gui import synthesis_views
from tests.test_synthesis_views import _document, _result

completed = _document()
failed = _document(
    report_uid="c" * 64,
    canonical_relative_path="downloaded/missing.pdf",
    file_name="missing.pdf",
    title="누락 리포트",
    status="failed",
    analysis=None,
    reason="본문 읽기 실패",
)
result = _result(status="partial", documents=[completed, failed], failed=1)
states = st.session_state.setdefault("job_states", iter([
    {"id": "partial-job", "status": "running"},
    {"id": "partial-job", "status": "running"},
    {"id": "partial-job", "status": "partial", "result": result},
]))
retry_calls = st.session_state.setdefault("retry_calls", [])

with patch.object(synthesis_views, "_session_owner_id", return_value="owner"), \
     patch.object(synthesis_views.report_corpus, "list_companies", return_value=["삼성전자"]), \
     patch.object(synthesis_views, "_latest_owned_job", side_effect=lambda _owner: next(states, {"id": "terminal", "status": result["status"], "result": result})), \
     patch.object(synthesis_views, "_current_fingerprint", return_value="fixed-scope"), \
     patch.object(synthesis_views, "_verified_pdf", return_value=(None, "원문 없음")), \
     patch.object(
         synthesis_views.synthesis_jobs,
         "retry_job",
         side_effect=lambda *args: retry_calls.append(args) or "retry-job",
     ):
    synthesis_views.render_synthesis_page()
""", encoding="utf-8")

    app = AppTest.from_file(str(harness)).run(timeout=30)

    assert not app.exception
    assert any("핵심 내용" in item.value for item in app.markdown)
    assert any("사유: 본문 읽기 실패" in item.value for item in app.caption)
    assert any("완료된 문서만으로 종합" in item.value for item in app.warning)
    assert any(button.label == "미완료 문서 다시 시도" for button in app.button)
    assert all(button.label != "결과 확인" for button in app.button)
    assert app.session_state["retry_calls"] == []


def test_running_job_displays_uncited_raw_partial_without_confirmation_or_retry(tmp_path):
    harness = tmp_path / "uncited_partial_result_harness.py"
    harness.write_text("""
import streamlit as st
from unittest.mock import patch
from apps.gui import synthesis_views
from tests.test_synthesis_views import _document, _raw_result

cited = _document(
    status="prepared",
    analysis=None,
    synthesis_status="cited",
    preparation={"total_segments": 2, "body_chars": 1200},
)
uncited = _document(
    report_uid="d" * 64,
    canonical_relative_path="downloaded/uncited.pdf",
    file_name="uncited.pdf",
    title="인용 누락 리포트",
    status="prepared",
    analysis=None,
    synthesis_status="uncited",
    reason="최종 종합에서 검증된 원문 인용 근거로 채택되지 않았습니다.",
    preparation={"total_segments": 1, "body_chars": 800},
)
result = _raw_result(status="partial", documents=[cited, uncited])
result["coverage"] = {"prepared": 2, "cited": 1, "uncited": 1}
states = st.session_state.setdefault("job_states", iter([
    {"id": "uncited-partial-job", "status": "running"},
    {"id": "uncited-partial-job", "status": "running"},
    {"id": "uncited-partial-job", "status": "partial", "result": result},
]))
retry_calls = st.session_state.setdefault("raw_retry_calls", [])

with patch.object(synthesis_views, "_session_owner_id", return_value="owner"), \
     patch.object(synthesis_views.report_corpus, "list_companies", return_value=["삼성전자"]), \
     patch.object(synthesis_views, "_latest_owned_job", side_effect=lambda _owner: next(states, {"id": "terminal", "status": result["status"], "result": result})), \
     patch.object(synthesis_views, "_current_fingerprint", return_value="fixed-scope"), \
     patch.object(synthesis_views, "_verified_pdf", return_value=(None, "원문 없음")), \
     patch.object(
         synthesis_views.synthesis_jobs,
         "retry_job",
         side_effect=lambda *args: retry_calls.append(args) or "retry-job",
     ):
    synthesis_views.render_synthesis_page()
""", encoding="utf-8")

    app = AppTest.from_file(str(harness)).run(timeout=30)

    assert not app.exception
    assert any("핵심 내용" in item.value for item in app.markdown)
    assert any(
        "사유: 최종 종합에서 검증된 원문 인용 근거로 채택되지 않았습니다." in item.value
        for item in app.caption
    )
    assert any(
        "대상 리포트 중 1건은 최종 분석에 반영되지 않았습니다." in item.value
        for item in app.caption
    )
    assert any(button.label == "부분 종합 다시 생성" for button in app.button)
    assert all(button.label != "결과 확인" for button in app.button)
    assert app.session_state["raw_retry_calls"] == []
    appendix = next(item for item in app.expander if item.label == "근거 부록 · 인용한 문서 (1개)")
    other_documents = next(item for item in appendix.expander if item.label == "기타 대상 문서 (1개)")
    assert not other_documents.proto.expanded
    other_rows = [item for item in other_documents.children.values() if item.type == "expander"]
    assert len(other_rows) == 1
    assert "인용 누락 리포트" in other_rows[0].label
    assert not other_rows[0].label.startswith("[")
    assert sum(item.type == "expander" and item.label.startswith("[1] ")
               for item in appendix.children.values()) == 1
