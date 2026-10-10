import json
import logging
import threading
from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage

from src.core import report_synthesis as service
from src.utils.citations import validate_citation_contract


def document(index=1, *, body=None, broker=None):
    """Public fixture imported by test_synthesis_app_flow."""
    return {
        "report_uid": f"doc-{index}",
        "source_sha256": f"source-{index}",
        "retrieval_metadata_sha256": f"meta-{index}",
        "canonical_relative_path": f"downloaded/report-{index}.pdf",
        "file_name": "same-name.pdf",
        "target_name": "테스트회사",
        "report_type": "company",
        "report_date": f"2026-09-{(index - 1) % 28 + 1:02d}",
        "broker": broker or f"증권사 {index}",
        "title": f"기업 리포트 {index}",
        "parents": [{
            "parent_uid": f"parent-{index}", "parent_order": 0,
            "content": body or f"기업 수익성이 개선된 근거를 설명하는 문장 {index}입니다.",
            "content_sha256": f"content-{index}",
        }],
    }


def corpus(documents, *, excluded=None):
    """Public fixture imported by test_synthesis_app_flow."""
    return {
        "company": "테스트회사", "start_date": "2026-09-01", "end_date": "2026-09-30",
        "revision": {"base": "fixed"}, "profile_id": "profile", "fingerprint": "fixed-corpus",
        "documents": documents, "excluded": excluded or [], "warnings": ["테스트 경고"],
    }


class Model:
    """Deterministic provider for the raw-body generation contract."""

    model_name = "fake-model"

    def __init__(self):
        self.calls = []
        self.invalid_quote = False
        self.invalid_citation = False
        self.omit_document = False
        self.omit_endpoint_date = None
        self.section = "summary"
        self._lock = threading.Lock()

    @staticmethod
    def _references(payload):
        cards = {source_id: card for card in payload["cards"] for source_id in card["source_ids"]}
        references, seen_documents = [], set()
        for source_id, source in payload["sources"].items():
            if source["document_id"] in seen_documents:
                continue
            seen_documents.add(source["document_id"])
            card = cards[source_id]
            if card["kind"] == "raw":
                quote = "".join(item["quote"] for item in card["quotes"])[:40]
            else:
                quote = next(item["quote"] for item in card["references"] if item["source_id"] == source_id)
            references.append({"source_id": source_id, "quote": quote})
        return references

    def invoke(self, messages):
        payload = json.loads(messages[-1].content)
        assert payload["stage"] == "synthesize"
        assert payload["mode"] in {"report", "reduce"}
        with self._lock:
            self.calls.append(payload)
        references = self._references(payload)
        if self.omit_document:
            references = references[:1]
        if self.invalid_citation and references:
            references[0]["source_id"] = "unknown"
        if self.invalid_quote and references:
            references[0]["quote"] = "원문에 존재하지 않는 조작된 인용입니다."
        statements = [{
            "section": self.section,
            "text": "원문 근거를 연결해 해당 기간의 기업 흐름을 분석했습니다.",
            "references": deepcopy(references),
        }]
        if payload["mode"] == "report":
            if self.section != "summary":
                statements.append({"section": "summary", "text": "기간의 핵심 판단입니다.",
                                   "references": deepcopy(references)})
            dated = sorted(references, key=lambda item: payload["sources"].get(item["source_id"], {}).get(
                "report_date", ""))
            endpoints = dated[:1]
            if dated and dated[-1]["source_id"] != dated[0]["source_id"]:
                endpoints.append(dated[-1])
            if self.omit_endpoint_date:
                endpoints = [item for item in endpoints if payload["sources"][item["source_id"]][
                    "report_date"] != self.omit_endpoint_date]
            if self.section != "period_flow":
                statements.append({
                    "section": "period_flow",
                    "text": "초기와 말기의 근거를 연결해 기업의 기간 내 흐름을 해석했습니다.",
                    "references": deepcopy(endpoints),
                })
            statements.append({"section": "conclusion", "text": "원문 근거에 따른 종합 결론입니다.",
                               "references": deepcopy(references[:1])})
        return {"statements": statements}


class Cache:
    def __init__(self):
        self.entries, self.gets, self.puts = {}, [], []
        self.raise_on_get = self.raise_on_put = False

    def get_batch(self, key):
        self.gets.append(key)
        if self.raise_on_get:
            raise RuntimeError("private cache get failure")
        return deepcopy(self.entries.get(key))

    def put_batch(self, key, output):
        self.puts.append(key)
        if self.raise_on_put:
            raise RuntimeError("private cache put failure")
        self.entries[key] = deepcopy(output)


def calls(model, mode=None):
    values = [item for item in model.calls if item.get("stage") == "synthesize"]
    return [item for item in values if item["mode"] == mode] if mode else values


def assert_raw_result(result, count):
    assert result["processing_mode"] == "raw_body"
    assert result["prompt_version"] == "rdb-report-14"
    assert result["schema_version"] == 2
    assert result["counts"]["completed"] == count
    assert all(item["status"] == "prepared" for item in result["documents"][:count])
    assert all("analysis" not in item and "parents" not in item for item in result["documents"])


def test_many_documents_use_one_direct_writer_call_and_preserve_citations():
    model, progress = Model(), []
    result = service.synthesize_corpus(corpus([document(i) for i in range(1, 26)]), model=model,
                                       on_progress=progress.append)
    assert result["status"] == "completed"
    assert result["counts"] == {"total": 25, "completed": 25, "failed": 0, "excluded": 0, "brokers": 25}
    assert [call["mode"] for call in model.calls] == ["report"]
    assert {item["report_uid"] for item in model.calls[0]["sources"].values()} == {
        f"doc-{i}" for i in range(1, 26)}
    assert "[25]" in result["content"]
    assert validate_citation_contract(result["selected_sources"], result["citation_contract"])["status"] == "valid"
    assert result["revision"] == {"base": "fixed"} and result["warnings"] == ["테스트 경고"]
    assert progress[-1]["phase"] == "synthesizing"
    assert_raw_result(result, 25)


def test_long_body_is_fully_submitted_as_raw_chunks_and_offsets_are_exact():
    body = "앞부분의 실적 근거입니다. " + "가" * 12000 + " 뒷부분의 중요한 위험 근거입니다."
    model = Model()
    result = service.synthesize_corpus(corpus([document(body=body)]), model=model)
    raw_cards = [card for call in model.calls for card in call["cards"] if card["kind"] == "raw"]
    texts = ["".join(item["quote"] for item in card["quotes"]) for card in raw_cards]
    assert "".join(texts) == body
    assert max(map(len, texts)) <= service.CHUNK_CHARS
    assert result["documents"][0]["preparation"] == {"total_segments": len(raw_cards), "body_chars": len(body)}
    for evidence in result["evidence"]:
        assert body[evidence["start"]:evidence["end"]] == evidence["quote"]
    assert_raw_result(result, 1)


def test_every_parent_is_submitted_without_per_document_llm_calls():
    report = document()
    report["parents"].append({"parent_uid": "parent-second", "parent_order": 1,
                              "content": "두 번째 원문 근거 문장입니다.", "content_sha256": "second"})
    model = Model()
    result = service.synthesize_corpus(corpus([report]), model=model)
    assert ["".join(item["quote"] for item in card["quotes"]) for card in model.calls[0]["cards"]] == [
        report["parents"][0]["content"], report["parents"][1]["content"]]
    assert len(model.calls) == 1
    assert result["documents"][0]["preparation"]["total_segments"] == 2


def test_malformed_local_document_fails_but_valid_documents_are_composed():
    broken = document(2)
    broken["parents"][0]["content"] = None
    result = service.synthesize_corpus(corpus([document(1), broken], excluded=[{
        "report_uid": "pending", "reason": "준비 중"}]), model=Model())
    assert result["status"] == "partial"
    assert result["counts"] == {"total": 3, "completed": 1, "failed": 1, "excluded": 1, "brokers": 1}
    assert [item["status"] for item in result["documents"]] == ["prepared", "failed", "excluded"]
    assert {item["report_uid"] for item in result["selected_sources"]} == {"doc-1"}


@pytest.mark.parametrize("body", ["", "   ", None])
def test_no_usable_body_fails_without_provider(body, monkeypatch):
    report, constructed = document(), []
    report["parents"][0]["content"] = body
    monkeypatch.setattr("src.llms.factory.build_chat_model", lambda **kwargs: constructed.append(kwargs))
    result = service.synthesize_corpus(corpus([report]))
    assert result["status"] == "failed" and result["counts"]["failed"] == 1
    assert result["documents"][0]["status"] == "failed"
    assert result["diagnostic"]["category"] == "input_validation"
    assert constructed == []


def test_unknown_source_reference_fails_closed_after_preparation():
    model = Model()
    model.invalid_citation = True
    result = service.synthesize_corpus(corpus([document(1), document(2)]), model=model)
    assert result["status"] == "failed" and result["counts"]["completed"] == 2
    assert result["content"] == "" and result["selected_sources"] == []
    assert result["citation_contract"] is None and len(model.calls) == 1


def test_unverifiable_quote_fails_once_without_retry_or_data_exposure(caplog):
    model = Model()
    model.invalid_quote = True
    with caplog.at_level(logging.INFO, logger=service.__name__):
        result = service.synthesize_corpus(corpus([document()]), model=model)
    assert result["status"] == "failed" and len(model.calls) == 1
    assert result["diagnostic"]["category"] == "evidence_mismatch"
    assert "조작된 인용" not in caplog.text and document()["parents"][0]["content"] not in caplog.text


@pytest.mark.parametrize("text", [
    "PBR < 1배 구간에서 ROE > 10%를 회복했다.",
    "<초기 상황>에서 <후기 전망>으로 이어지는 변화다.",
    "[2026]년 추정치와 [005930]의 기업 상황을 비교했다.",
    "수치 배열의 표기 ](는 원문에 존재하는 일반 텍스트다.",
    "  " + "가" * 1400 + "\n",
], ids=["inequalities", "korean_labels", "year_and_ticker", "plain_delimiters", "trimmed_limit"])
def test_plain_financial_text_is_preserved_without_false_markup_detection(text):
    assert service._text(text, 1400) == text.strip()


@pytest.mark.parametrize("text,reason", [
    (None, "text_shape"), (" \n ", "text_empty"), ("가" * 1401, "text_length"),
    ("근거 [1]", "citation_marker"), ("근거 [200]", "citation_marker"),
    ("[회사](https://example.com)", "generated_link"),
    ("<script>본문</script>", "generated_markup"), ("<a href='https://example.com'>회사</a>", "generated_markup"),
    ("<https://example.com>", "generated_link"), ("<mailto:test@example.com>", "generated_link"),
    ("<test@example.com>", "generated_link"),
], ids=["type", "blank", "long", "citation_1", "citation_200", "markdown_link", "script", "html_link", "autolink", "mailto", "email"])
def test_actual_markup_and_invalid_text_keep_body_free_failure_reasons(text, reason):
    with pytest.raises(service.SynthesisOutputError) as failure:
        service._text(text, 1400)
    assert failure.value.reason == reason
    details = service._failure_details(failure.value)
    assert details == {"category": "output_validation", "error_type": "SynthesisOutputError", "reason": reason}


def test_financial_angle_notation_survives_real_synthesis_validation_and_cache():
    class FinancialWriter(Model):
        def invoke(self, messages):
            output = super().invoke(messages)
            for statement in output["statements"]:
                statement["text"] = "<기업의 변화> PBR < 1배에서 ROE > 10%를 회복했다. [2026]년 전망이다."
            return output
    model, cache = FinancialWriter(), Cache()
    source = corpus([document(1), document(2), document(3)])
    first = service.synthesize_corpus(source, model=model, cache=cache)
    second = service.synthesize_corpus(source, model=model, cache=cache)
    assert first["status"] == second["status"] == "completed"
    assert first["content"] == second["content"] and first["evidence"] == second["evidence"]
    assert "PBR < 1배에서 ROE > 10%" in first["content"] and len(model.calls) == 1


def test_text_validation_failure_logs_the_reason_without_the_response_body(caplog):
    class MarkupWriter(Model):
        def invoke(self, messages):
            output = super().invoke(messages)
            output["statements"][0]["text"] = "<script>private-response-body</script>"
            return output
    model, cache = MarkupWriter(), Cache()
    with caplog.at_level(logging.INFO, logger=service.__name__):
        result = service.synthesize_corpus(corpus([document()]), model=model, cache=cache)
    assert result["status"] == "failed" and result["diagnostic"]["reason"] == "generated_markup"
    assert '"reason": "generated_markup"' in caplog.text
    assert "private-response-body" not in caplog.text + json.dumps(result)
    assert "HTML" in result["error"] and not cache.entries and len(model.calls) == 1


def test_long_valid_analysis_is_preserved_and_rendered_as_readable_paragraphs():
    long_text = ("기업 상황과 전망 조건을 원문 근거에 따라 분석한 문장입니다. " * 65).strip()
    assert 1400 < len(long_text) < 33600
    class LongWriter(Model):
        def invoke(self, messages):
            output = super().invoke(messages)
            output["statements"][0]["text"] = long_text
            return output
    model, cache = LongWriter(), Cache()
    source = corpus([document(1), document(2), document(3)])
    result = service.synthesize_corpus(source, model=model, cache=cache)
    assert result["status"] == "completed"
    assert result["statements"][0]["text"] == long_text
    summary_evidence = [item for item in result["evidence"] if item["topic"] == service._SECTIONS["summary"]]
    assert summary_evidence and all(item["claim"] == long_text for item in summary_evidence)
    summary = result["content"].split("### 핵심 판단\n\n")[1].split("\n\n### 기간 내 기업 흐름")[0]
    paragraphs = [part.removesuffix(" [1] [2] [3]") for part in summary.split("\n\n")]
    assert len(paragraphs) >= 2 and all(len(part) <= 1400 for part in paragraphs)
    assert "".join(paragraphs) == long_text
    restored = service.synthesize_corpus(source, model=model, cache=cache)
    assert restored["content"] == result["content"] and len(model.calls) == 1


def test_total_report_writing_budget_still_rejects_excessive_output():
    class OversizedWriter(Model):
        def invoke(self, messages):
            output = super().invoke(messages)
            for item in output["statements"]:
                item["text"] = "가" * 12000
            return output
    model, cache = OversizedWriter(), Cache()
    result = service.synthesize_corpus(corpus([document()]), model=model, cache=cache)
    assert result["status"] == "failed" and result["diagnostic"]["reason"] == "writing_budget"
    assert not result["content"] and not cache.entries and len(model.calls) == 1


@pytest.mark.parametrize("extra,status", [(0, "completed"), (1, "failed")])
def test_total_report_text_budget_boundary(extra, status):
    class BoundaryWriter(Model):
        def invoke(self, messages):
            output = super().invoke(messages)
            for item in output["statements"]:
                item["text"] = "가" * 11200
            output["statements"][0]["text"] += "나" * extra
            return output
    cache = Cache()
    result = service.synthesize_corpus(corpus([document()]), model=BoundaryWriter(), cache=cache)
    assert result["status"] == status
    if status == "completed":
        assert sum(len(item["text"]) for item in result["statements"]) == 33600 and cache.entries
    else:
        assert result["diagnostic"]["reason"] == "writing_budget" and not cache.entries


def test_readable_paragraph_splitting_preserves_boundaries_and_all_characters():
    text = "가" * 750 + ".\n" + "나" * 900 + "다" * 1800
    paragraphs = list(service._paragraphs(text))
    assert paragraphs[0] == "가" * 750 + ".\n"
    assert "".join(paragraphs) == text
    assert all(0 < len(part) <= 1400 for part in paragraphs)


def test_reduction_keeps_its_total_writing_budget_for_long_logical_statements():
    _, passages, cards = service._prepare(document())
    payload, aliases = service._payload(cards, passages, {}, final=False)
    from langchain_core.messages import HumanMessage
    output = Model().invoke([HumanMessage(content=json.dumps(payload))])
    output["statements"][0]["text"] = "가" * 2001
    verified = service._validate_statements(output, passages, aliases, final=False, writing_budget_chars=2100)
    assert verified[0]["text"] == output["statements"][0]["text"]
    with pytest.raises(service.SynthesisOutputError) as failure:
        service._validate_statements(output, passages, aliases, final=False, writing_budget_chars=2000)
    assert failure.value.reason == "text_length"


@pytest.mark.parametrize("kind,category", [
    ("timeout", "timeout"), ("connection", "connection"), ("json", "json_format"),
    ("schema", "output_validation"), ("http", "rate_limit"), ("server", "provider_http")])
def test_provider_failures_are_distinguished_and_never_retried(kind, category, caplog):
    class FailureModel(Model):
        attempts = 0

        def invoke(self, messages):
            self.attempts += 1
            if kind == "timeout":
                raise TimeoutError("secret key and request body")
            if kind == "connection":
                raise ConnectionError("secret key and request body")
            if kind == "json":
                return "secret key and request body"
            if kind == "schema":
                return {"statements": "secret key and request body"}
            error = RuntimeError("secret key and request body")
            error.status_code = 429 if kind == "http" else 503
            raise error

    model = FailureModel()
    with caplog.at_level(logging.INFO, logger=service.__name__):
        result = service.synthesize_corpus(corpus([document()]), model=model)
    assert result["status"] == "failed" and model.attempts == 1
    assert result["diagnostic"]["category"] == category
    assert "secret key" not in json.dumps(result) and "secret key" not in caplog.text


def test_wrapped_timeout_is_classified_and_cycles_terminate():
    cause, outer = TimeoutError("private"), RuntimeError("private")
    outer.__cause__, cause.__context__ = cause, outer
    assert service._failure_details(outer) == {"category": "timeout", "error_type": "RuntimeError"}


@pytest.mark.parametrize("wrapper", ["fence", "inline_fence", "upper_fence", "bom", "text_blocks"])
def test_complete_json_response_wrappers_are_read_without_another_call(wrapper):
    class WrappedModel(Model):
        def invoke(self, messages):
            text = json.dumps(super().invoke(messages), ensure_ascii=False)
            content = {
                "fence": f"```\n{text}\n```",
                "inline_fence": f"```json{text}```",
                "upper_fence": f"```JSON\n{text}\n```",
                "bom": "\ufeff" + text,
                "text_blocks": [{"type": "text", "text": text[:20]}, {"type": "text", "text": text[20:]}],
            }[wrapper]
            return AIMessage(content=content, response_metadata={"finish_reason": "stop"})

    model = WrappedModel()
    assert service.synthesize_corpus(corpus([document()]), model=model)["status"] == "completed"
    assert len(model.calls) == 1


@pytest.mark.parametrize("finish,empty,category,reason", [
    ("stop", True, "empty_response", "reasoning_only"),
    ("length", True, "response_truncated", "output_token_limit"),
    ("length", False, "response_truncated", "output_token_limit"),
    ("error", False, "response_interrupted", "provider_finish_error"),
])
def test_empty_and_unfinished_responses_are_never_parsed_or_cached(finish, empty, category, reason, caplog):
    class IncompleteModel(Model):
        def invoke(self, messages):
            output = super().invoke(messages)
            return AIMessage(
                content="" if empty else json.dumps(output),
                response_metadata={"finish_reason": finish},
                usage_metadata={"input_tokens": 100, "output_tokens": 80, "total_tokens": 180,
                                "output_token_details": {"reasoning": 80}},
                additional_kwargs={"reasoning_content": "private chain and key"},
            )

    model, cache = IncompleteModel(), Cache()
    with caplog.at_level(logging.INFO, logger=service.__name__):
        result = service.synthesize_corpus(corpus([document()]), model=model, cache=cache)
    assert result["status"] == "failed" and result["content"] == "" and not cache.entries
    assert result["diagnostic"]["category"] == category
    assert result["diagnostic"]["reason"] == reason
    assert result["diagnostic"]["finish_reason"] == finish
    assert result["diagnostic"]["output_tokens"] == 80
    assert len(model.calls) == 1
    assert "private chain" not in caplog.text and "private chain" not in json.dumps(result)


def test_broken_json_records_safe_parse_location_without_repairing_it():
    class PartialModel(Model):
        def invoke(self, messages):
            text = json.dumps(super().invoke(messages))
            return AIMessage(content=text[:-10], response_metadata={"finish_reason": "stop"})

    model, cache = PartialModel(), Cache()
    result = service.synthesize_corpus(corpus([document()]), model=model, cache=cache)
    assert result["status"] == "failed" and not cache.entries and len(model.calls) == 1
    assert result["diagnostic"]["category"] == "json_format"
    assert result["diagnostic"]["reason"] == "invalid_json"
    assert result["diagnostic"]["response_chars"] > 0 and result["diagnostic"]["json_column"] > 0


def test_observed_metrics_log_input_kind_without_content(monkeypatch, caplog):
    model = Model()
    monkeypatch.setattr("src.llms.factory.build_chat_model", lambda **kwargs: model)
    monkeypatch.setattr("src.llms.generation_observability.invoke_chat_with_observability",
                        lambda instance, messages: (instance.invoke(messages), {
                            "input_tokens": 100, "output_tokens": 200, "reasoning_tokens": 150,
                            "private_provider_payload": "secret"}))
    with caplog.at_level(logging.INFO, logger=service.__name__):
        assert service.synthesize_corpus(corpus([document()]))["status"] == "completed"
    assert "input_tokens=100 output_tokens=200 reasoning_tokens=150" in caplog.text
    assert "input_kind=raw" in caplog.text and "secret" not in caplog.text


def test_consensus_and_change_guards_use_brokers_and_dates():
    common = Model()
    common.section = "common"
    result = service.synthesize_corpus(corpus([document(1, broker="동일"), document(2, broker="동일")]), model=common)
    assert result["status"] == "partial"
    assert result["validation_warnings"] == [{"stage": "report", "section": "common", "reason": "broker_comparison"}]
    assert all(item["section"] != "common" for item in result["statements"])
    cross = Model()
    cross.section = "changes"
    result = service.synthesize_corpus(corpus([document(1), document(2)]), model=cross)
    assert result["status"] == "partial"
    assert result["validation_warnings"] == [{"stage": "report", "section": "changes", "reason": "broker_changes"}]
    assert all(item["section"] != "changes" for item in result["statements"])
    same = Model()
    same.section = "changes"
    result = service.synthesize_corpus(corpus([document(1, broker="동일"), document(2, broker="동일")]), model=same)
    assert result["status"] == "completed" and "증권사 전망의 변화" in result["content"]


def test_unsupported_optional_comparison_is_not_cached_or_relabelled():
    model, cache = Model(), Cache()
    model.section = "changes"
    scope = corpus([document(1), document(2)])
    result = service.synthesize_corpus(scope, model=model, cache=cache)
    assert result["status"] == "partial" and result["coverage"]["uncited"] == 0
    assert {item["section"] for item in result["statements"]} == {"summary", "period_flow", "conclusion"}
    assert all(item["topic"] != service._SECTIONS["changes"] for item in result["evidence"])
    assert "비교 근거" not in result["content"] and not cache.entries and len(model.calls) == 1
    assert result["validation_warnings"]
    model.section = "summary"
    assert service.synthesize_corpus(scope, model=model, cache=cache)["status"] == "completed"
    assert len(model.calls) == 2 and cache.entries


def test_optional_comparison_with_invalid_quote_still_fails_closed():
    model = Model()
    model.section = "changes"
    model.invalid_quote = True
    result = service.synthesize_corpus(corpus([document(1), document(2)]), model=model)
    assert result["status"] == "failed" and result["diagnostic"]["category"] == "evidence_mismatch"


def test_optional_omissions_from_reduction_reach_the_final_result(monkeypatch):
    monkeypatch.setattr(service, "SYNTHESIS_INPUT_BYTES", 12000)
    class UnsupportedReduce(Model):
        def invoke(self, messages):
            payload = json.loads(messages[-1].content)
            output = super().invoke(messages)
            if payload["mode"] == "reduce":
                output["statements"].append({"section": "changes", "text": "근거 부족인 변화 문단입니다.",
                    "references": [deepcopy(output["statements"][0]["references"][0])]})
            return output
    model = UnsupportedReduce()
    result = service.synthesize_corpus(corpus([document(i, body=f"report {i} " + "x" * 2500) for i in range(1, 13)]), model=model)
    assert result["status"] == "partial" and result["coverage"]["uncited"] == 0
    assert result["validation_warnings"] and all(item["stage"] == "reduce" and item["reason"] == "broker_changes" for item in result["validation_warnings"])
    assert "근거 부족인 변화 문단" not in result["content"]
    second = service.synthesize_corpus(corpus([document(i, body=f"report {i} " + "x" * 2500) for i in range(1, 13)]), model=model)
    assert second["validation_warnings"] == result["validation_warnings"]
    assert second["warnings"] == ["테스트 경고"]
    clean = service.synthesize_corpus(corpus([document()]), model=Model())
    assert clean["status"] == "completed" and clean["validation_warnings"] == []


def test_all_optional_statements_without_comparison_support_fail_safely():
    class OnlyUnsupported(Model):
        def invoke(self, messages):
            output = super().invoke(messages)
            first = output["statements"][0]
            first["section"] = "changes"
            return {"statements": [first]}
    result = service.synthesize_corpus(corpus([document()]), model=OnlyUnsupported())
    assert result["status"] == "failed" and result["diagnostic"]["reason"] == "no_verified_statements"
    assert not result["content"] and not result["evidence"]


@pytest.mark.parametrize("date", ["2026-09-01", "2026-09-03"])
def test_period_flow_requires_both_publication_date_endpoints(date):
    model = Model()
    model.omit_endpoint_date = date
    result = service.synthesize_corpus(corpus([document(1), document(2), document(3)]), model=model)
    assert result["status"] == "failed" and result["counts"]["completed"] == 3
    assert result["diagnostic"]["reason"] == "endpoint_dates"
    assert "최초·최종 발행일" in result["error"]


def test_writer_receives_explicit_document_and_endpoint_coverage_requirements():
    model = Model()
    source = corpus([document(1), document(2), document(3)])
    assert service.synthesize_corpus(source, model=model)["status"] == "completed"
    payload = model.calls[-1]
    requirements = payload["requirements"]
    assert requirements["available_document_ids"] == ["d1", "d2", "d3"]
    assert requirements["required_sections"] == ["summary", "period_flow", "conclusion"]
    endpoints = requirements["period_flow_endpoint_sources"]
    assert set(endpoints) == {"2026-09-01", "2026-09-03"}
    for date, aliases in endpoints.items():
        assert aliases and all(payload["sources"][alias]["report_date"] == date for alias in aliases)


def test_uncited_documents_are_explicitly_omitted_from_a_partial_report():
    model = Model()
    model.omit_document = True
    cache = Cache()
    scope = corpus([document(1), document(2)])
    result = service.synthesize_corpus(scope, model=model, cache=cache)
    assert result["status"] == "partial" and result["counts"]["completed"] == 2
    assert result["coverage"] == {"prepared": 2, "cited": 1, "uncited": 1}
    assert {item["report_uid"] for item in result["selected_sources"]} == {"doc-1"}
    assert {item["report_uid"] for item in result["evidence"]} == {"doc-1"}
    assert result["documents"][1]["synthesis_status"] == "uncited"
    assert "인용" in result["documents"][1]["reason"]
    assert "인용 누락" not in result["content"] and "부분 종합:" not in result["content"]
    assert "동일 증권사의 전망 변화를 판단할 근거가 부족합니다." not in result["content"]
    assert not cache.entries and len(model.calls) == 1
    restored = service.synthesize_corpus(scope, model=model, cache=cache)
    assert restored["status"] == "partial" and restored["coverage"] == result["coverage"]
    assert restored["evidence"] == result["evidence"] and len(model.calls) == 2


def test_invalid_quotes_still_block_partial_reports():
    model, cache = Model(), Cache()
    model.omit_document = model.invalid_quote = True
    result = service.synthesize_corpus(corpus([document(1), document(2)]), model=model, cache=cache)
    assert result["status"] == "failed" and not result["content"] and not cache.entries
    assert result["diagnostic"]["category"] == "evidence_mismatch"


def test_manual_retry_can_fill_omitted_documents_instead_of_reusing_partial_output():
    model, cache = Model(), Cache()
    model.omit_document = True
    scope = corpus([document(1), document(2)])
    assert service.synthesize_corpus(scope, model=model, cache=cache)["status"] == "partial"
    model.omit_document = False
    second = service.synthesize_corpus(scope, model=model, cache=cache)
    assert second["status"] == "completed" and second["coverage"]["uncited"] == 0
    assert len(model.calls) == 2 and cache.entries
    assert service.synthesize_corpus(scope, model=model, cache=cache)["status"] == "completed"
    assert len(model.calls) == 2


def test_incomplete_cached_output_is_a_miss_even_when_its_quotes_are_valid():
    class StalePartialCache(Cache):
        def get_batch(self, key):
            single = document(1)
            _, passages, cards = service._prepare(single)
            payload, _ = service._payload(cards, passages, {}, final=True)
            from langchain_core.messages import HumanMessage
            return Model().invoke([HumanMessage(content=json.dumps(payload))])
    model, cache = Model(), StalePartialCache()
    result = service.synthesize_corpus(corpus([document(1), document(2)]), model=model, cache=cache)
    assert result["status"] == "completed" and result["coverage"]["cited"] == 2
    assert len(model.calls) == 1 and cache.entries


def test_reduction_omissions_are_reported_against_the_original_frozen_scope(monkeypatch):
    monkeypatch.setattr(service, "SYNTHESIS_INPUT_BYTES", 12_000)
    model = Model()
    model.omit_document = True
    reports = [document(i, body=f"report {i} " + "x" * 2_500) for i in range(1, 13)]
    result = service.synthesize_corpus(corpus(reports), model=model)
    assert result["status"] == "partial" and calls(model, "reduce")
    assert result["coverage"] == {"prepared": 12, "cited": 1, "uncited": 11}
    assert len(result["documents"]) == 12
    assert sum(item.get("synthesis_status") == "uncited" for item in result["documents"]) == 11


def test_same_day_reports_explain_temporal_limit():
    first, second = document(1), document(2)
    second["report_date"] = first["report_date"]
    content = service.synthesize_corpus(corpus([first, second]), model=Model())["content"]
    assert "발행 시점이 하나" in content and "기간 전후의 변화" in content


def test_public_evidence_points_to_original_text_not_document_analysis():
    result = service.synthesize_corpus(corpus([document()]), model=Model())
    assert set(result["evidence"][0]) == {
        "claim_id", "report_uid", "broker", "report_date", "parent_uid", "parent_order",
        "start", "end", "quote", "claim", "topic"}
    assert result["evidence"][0]["quote"] in document()["parents"][0]["content"]
    assert result["statements"][0]["claim_ids"] and result["statements"][0]["provenance"]
    assert "references" not in result["statements"][0]


def test_same_quote_in_different_paragraphs_keeps_distinct_claim_identity():
    result = service.synthesize_corpus(corpus([document()]), model=Model())
    evidence = {item["claim_id"]: item for item in result["evidence"]}

    assert len({claim_id for statement in result["statements"] for claim_id in statement["claim_ids"]}) == 3
    for statement in result["statements"]:
        for claim_id in statement["claim_ids"]:
            assert evidence[claim_id]["claim"] == statement["text"]
            assert evidence[claim_id]["topic"] == service._SECTIONS[statement["section"]]


def test_whitespace_normalized_quote_preserves_original_offsets():
    body = "매출 증가의 원인은\n  신규 고객 확보와 판매량 증가입니다."

    class WhitespaceModel(Model):
        def invoke(self, messages):
            output = super().invoke(messages)
            for statement in output["statements"]:
                for reference in statement["references"]:
                    reference["quote"] = "매출 증가의 원인은 신규 고객 확보와 판매량 증가입니다."
            return output

    result = service.synthesize_corpus(corpus([document(body=body)]), model=WhitespaceModel())
    evidence = result["evidence"][0]
    assert result["status"] == "completed" and evidence["quote"] == body[evidence["start"]:evidence["end"]] == body


class QuoteIdModel(Model):
    @staticmethod
    def _references(payload):
        references, seen = [], set()
        for card in payload["cards"]:
            choices = card["quotes"] if card["kind"] == "raw" else card["references"]
            for choice in choices:
                source_id = choice.get("source_id") or card["source_ids"][0]
                document_id = payload["sources"][source_id]["document_id"]
                if document_id not in seen:
                    seen.add(document_id)
                    references.append({"source_id": source_id, "quote_id": choice["quote_id"]})
        return references


def test_quote_ids_restore_exact_original_text_and_offsets():
    body = "매출액은 1,234억원입니다.\n  전망은 +12.5%이며 조건부입니다. " * 35
    model = QuoteIdModel()
    result = service.synthesize_corpus(corpus([document(body=body)]), model=model)
    assert result["status"] == "completed" and len(model.calls) == 1
    raw_cards = model.calls[0]["cards"]
    assert "".join(quote["quote"] for card in raw_cards for quote in card["quotes"]) == body
    assert len({quote["quote_id"] for card in raw_cards for quote in card["quotes"]}) == sum(
        len(card["quotes"]) for card in raw_cards)
    for evidence in result["evidence"]:
        assert body[evidence["start"]:evidence["end"]] == evidence["quote"]


def test_cached_quote_id_preserves_the_selected_occurrence_of_repeated_text():
    class LaterQuoteModel(QuoteIdModel):
        @staticmethod
        def _references(payload):
            card = payload["cards"][0]
            return [{"source_id": card["source_ids"][0], "quote_id": card["quotes"][-1]["quote_id"]}]

    body = "Repeated original quote. " * 20
    model, cache = LaterQuoteModel(), Cache()
    source = corpus([document(body=body)])
    first = service.synthesize_corpus(source, model=model, cache=cache)
    chosen = model.calls[0]["cards"][0]["quotes"][-1]
    assert chosen["start"] > 0 and body.count(chosen["quote"]) > 1
    model.calls.clear()
    second = service.synthesize_corpus(source, model=model, cache=cache)
    assert first["status"] == second["status"] == "completed" and not model.calls
    assert first["evidence"] == second["evidence"]
    assert all(item["start"] == chosen["start"] for item in second["evidence"])


def test_reduction_and_cache_preserve_later_quote_occurrences(monkeypatch):
    monkeypatch.setattr(service, "SYNTHESIS_INPUT_BYTES", 12_000)

    class LaterQuoteModel(QuoteIdModel):
        @staticmethod
        def _references(payload):
            references = QuoteIdModel._references(payload)
            raw_cards = {card["source_ids"][0]: card for card in payload["cards"] if card["kind"] == "raw"}
            for reference in references:
                card = raw_cards.get(reference["source_id"])
                if card:
                    reference["quote_id"] = card["quotes"][-1]["quote_id"]
            return references

    body = "Repeated original quote. " * 40
    source = corpus([document(i, body=body) for i in range(1, 9)])
    model, cache = LaterQuoteModel(), Cache()
    first = service.synthesize_corpus(source, model=model, cache=cache)
    assert first["status"] == "completed" and calls(model, "reduce")
    chosen = calls(model, "reduce")[0]["cards"][0]["quotes"][-1]
    assert chosen["start"] > 0 and body.count(chosen["quote"]) > 1
    assert all((item["start"], item["end"]) == (chosen["start"], chosen["end"])
               for item in first["evidence"])
    model.calls.clear()
    second = service.synthesize_corpus(source, model=model, cache=cache)
    assert second["status"] == "completed" and not model.calls
    assert first["evidence"] == second["evidence"]


@pytest.mark.parametrize("mutation", ["unknown", "wrong_source", "changed_quote"])
def test_quote_ids_cannot_bypass_source_or_text_validation(mutation):
    class TamperedModel(QuoteIdModel):
        def invoke(self, messages):
            output = super().invoke(messages)
            reference = output["statements"][0]["references"][0]
            if mutation == "unknown":
                reference["quote_id"] = "unknown"
            elif mutation == "wrong_source":
                reference["source_id"] = output["statements"][0]["references"][1]["source_id"]
            else:
                reference["quote"] = "매출액은 9,999억원으로 조작했습니다."
            return output

    model, cache = TamperedModel(), Cache()
    result = service.synthesize_corpus(corpus([document(1), document(2)]), model=model, cache=cache)
    assert result["status"] == "failed" and result["content"] == "" and not cache.entries
    assert result["diagnostic"]["category"] == "evidence_mismatch"
    assert len(model.calls) == 1


def test_quote_ids_survive_reduction_and_verified_cache_reuse(monkeypatch):
    # Leave room for the chronological plan and prompt; still force reductions.
    monkeypatch.setattr(service, "SYNTHESIS_INPUT_BYTES", 16_000)
    source = corpus([document(i, body=f"Original report {i} " + "x" * 2500) for i in range(1, 13)])
    cache, model = Cache(), QuoteIdModel()
    result = service.synthesize_corpus(source, model=model, cache=cache)
    assert result["status"] == "completed" and calls(model, "reduce")
    assert max(service._input_size("", payload) for payload in model.calls) <= service.SYNTHESIS_INPUT_BYTES
    assert {item["report_uid"] for item in result["evidence"]} == {f"doc-{i}" for i in range(1, 13)}
    model.calls.clear()
    assert service.synthesize_corpus(source, model=model, cache=cache)["status"] == "completed"
    assert not model.calls


def test_evidence_failure_explains_cause_without_exposing_quotes():
    model = Model()
    model.invalid_quote = True
    result = service.synthesize_corpus(corpus([document()]), model=model)
    assert "원문과 일치하지 않는 인용" in result["error"]
    assert result["diagnostic"]["reason"] == "quote_not_found"
    assert result["diagnostic"]["stage"] == "report"
    assert "조작된 인용" not in json.dumps(result, ensure_ascii=False)


def test_empty_corpus_and_workload_limits_precede_provider(monkeypatch):
    constructed = []
    monkeypatch.setattr("src.llms.factory.build_chat_model", lambda **kwargs: constructed.append(kwargs))
    assert service.synthesize_corpus(corpus([]))["status"] == "empty"
    monkeypatch.setattr(service, "MAX_DOCUMENTS", 1)
    with pytest.raises(ValueError, match="한도"):
        service.synthesize_corpus(corpus([document(1), document(2)]))
    assert constructed == []


def test_body_and_chunk_limits_precede_model_use(monkeypatch):
    model = Model()
    monkeypatch.setattr(service, "MAX_BODY_CHARS", 5)
    with pytest.raises(ValueError, match="한도"):
        service.synthesize_corpus(corpus([document(body="123456")]), model=model)
    monkeypatch.setattr(service, "MAX_BODY_CHARS", 100_000)
    monkeypatch.setattr(service, "CHUNK_CHARS", 5)
    monkeypatch.setattr(service, "MAX_CHUNKS", 1)
    with pytest.raises(ValueError, match="한도"):
        service.synthesize_corpus(corpus([document(body="123456")]), model=model)
    assert model.calls == []


def test_exact_scope_final_cache_avoids_provider_call():
    cache, model, source = Cache(), Model(), corpus([document()])
    first = service.synthesize_corpus(source, model=model, cache=cache)
    assert first["status"] == "completed" and len(cache.entries) == 1
    model.calls.clear()
    second = service.synthesize_corpus(source, model=model, cache=cache)
    assert second["content"] == first["content"] and model.calls == []


@pytest.mark.parametrize("change", ["body", "date", "model", "prompt", "profile"])
def test_batch_cache_key_covers_generation_inputs(change, monkeypatch):
    cache, model, source = Cache(), Model(), corpus([document()])
    service.synthesize_corpus(source, model=model, cache=cache)
    changed = deepcopy(source)
    if change == "body":
        changed["documents"][0]["parents"][0]["content"] += " 추가 근거입니다."
    elif change == "date":
        changed["documents"][0]["report_date"] = "2026-09-30"
    elif change == "model":
        model.model_name = "another-model"
    elif change == "prompt":
        monkeypatch.setattr(service, "REPORT_PROMPT_VERSION", "new-writer")
    else:
        changed["profile_id"] = "another-profile"
    model.calls.clear()
    assert service.synthesize_corpus(changed, model=model, cache=cache)["status"] == "completed"
    assert len(model.calls) == 1 and len(cache.entries) >= 2


def test_corrupt_cache_is_revalidated_and_recomputed():
    cache, model, source = Cache(), Model(), corpus([document()])
    service.synthesize_corpus(source, model=model, cache=cache)
    next(iter(cache.entries.values()))["statements"][0]["references"][0]["quote"] = "조작된 캐시 인용입니다."
    model.calls.clear()
    result = service.synthesize_corpus(source, model=model, cache=cache)
    assert result["status"] == "completed" and len(model.calls) == 1
    assert "조작된 캐시" not in json.dumps(result, ensure_ascii=False)


def test_optional_cache_failures_do_not_fail_generation():
    for operation in ("get", "put"):
        cache, model = Cache(), Model()
        setattr(cache, f"raise_on_{operation}", True)
        assert service.synthesize_corpus(corpus([document()]), model=model, cache=cache)["status"] == "completed"
        assert len(model.calls) == 1


def test_large_input_uses_bounded_hierarchical_reduction(monkeypatch):
    small = Model()
    assert service.synthesize_corpus(corpus([document(1), document(2)]), model=small)["status"] == "completed"
    assert [item["mode"] for item in small.calls] == ["report"]
    # Reserve the expanded final instructions while keeping multiple raw batches.
    monkeypatch.setattr(service, "SYNTHESIS_INPUT_BYTES", 16_000)
    monkeypatch.setattr(service, "CHUNK_CHARS", 1_000)
    class RecordingModel(Model):
        def __init__(self):
            super().__init__()
            self.input_bytes = []

        def invoke(self, messages):
            self.input_bytes.append(sum(len(message.content.encode("utf-8")) for message in messages))
            return super().invoke(messages)

    model = RecordingModel()
    reports = [document(i, body=f"보고서 {i} " + "가" * 900) for i in range(1, 13)]
    result = service.synthesize_corpus(corpus(reports), model=model)
    assert result["status"] == "completed" and any(item["mode"] == "reduce" for item in model.calls)
    assert model.calls[-1]["mode"] == "report" and len(model.calls) < 50
    assert max(model.input_bytes) <= service.SYNTHESIS_INPUT_BYTES


def test_reductions_use_at_most_three_concurrent_calls(monkeypatch):
    monkeypatch.setattr(service, "SYNTHESIS_INPUT_BYTES", 12_000)
    monkeypatch.setattr(service, "CHUNK_CHARS", 4_000)
    lock, gate = threading.Lock(), threading.Event()
    state = {"active": 0, "peak": 0, "entered": 0}

    class ConcurrentModel(Model):
        def invoke(self, messages):
            payload = json.loads(messages[-1].content)
            if payload["mode"] == "reduce":
                with lock:
                    state["active"] += 1
                    state["entered"] += 1
                    state["peak"] = max(state["peak"], state["active"])
                    if state["entered"] >= service.DOCUMENT_WORKERS:
                        gate.set()
                gate.wait(timeout=5)
                with lock:
                    state["active"] -= 1
            return super().invoke(messages)

    reports = [document(i, body=f"report {i} " + "x" * 2_500) for i in range(1, 13)]
    assert service.synthesize_corpus(corpus(reports), model=ConcurrentModel())["status"] == "completed"
    assert 2 <= state["peak"] <= service.DOCUMENT_WORKERS == 3


def test_manual_retry_reuses_successful_reductions(monkeypatch):
    monkeypatch.setattr(service, "SYNTHESIS_INPUT_BYTES", 12_000)
    monkeypatch.setattr(service, "CHUNK_CHARS", 4_000)

    class FailOnce(Model):
        failed = False

        def invoke(self, messages):
            payload = json.loads(messages[-1].content)
            with self._lock:
                should_fail = payload["mode"] == "reduce" and not self.failed
                if should_fail:
                    self.failed = True
                    self.calls.append(payload)
            if should_fail:
                raise ConnectionError("private")
            return super().invoke(messages)

    source = corpus([document(i, body=f"report {i} " + "x" * 2_500) for i in range(1, 13)])
    cache, model = Cache(), FailOnce()
    first = service.synthesize_corpus(source, model=model, cache=cache)
    first_reductions = len(calls(model, "reduce"))
    assert first["status"] == "failed" and first_reductions >= 2 and cache.entries
    completed_payloads = {json.dumps(call, sort_keys=True) for call in calls(model, "reduce")[1:]}
    model.calls.clear()
    assert service.synthesize_corpus(source, model=model, cache=cache)["status"] == "completed"
    # Successful siblings are reused; later reduction levels can add new calls.
    retried_payloads = {json.dumps(call, sort_keys=True) for call in calls(model, "reduce")}
    assert completed_payloads and completed_payloads.isdisjoint(retried_payloads)


def test_factory_builds_only_final_writer_profile_and_disables_retry(monkeypatch):
    from src.llms import factory
    options = []
    monkeypatch.setattr(factory, "GENERATION_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setattr(factory, "OPENROUTER_DATA_COLLECTION", "deny")
    monkeypatch.setattr(factory, "build_chat_model", lambda **kwargs: options.append(kwargs) or Model())
    service._build_model()
    assert len(options) == 1 and options[0]["reasoning"] == {"effort": "none"}
    assert options[0]["max_tokens"] == service.REPORT_MAX_TOKENS and options[0]["max_retries"] == 0
    assert "timeout" not in options[0]
    response_format = options[0]["model_kwargs"]["response_format"]
    assert response_format["type"] == "json_schema" and response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["required"] == ["statements"]
    assert options[0]["openrouter_provider"] == {"require_parameters": True, "data_collection": "deny"}


def test_deepseek_reasoning_disable_survives_sdk_request_serialization(monkeypatch):
    from openrouter import components
    from src.llms import factory
    monkeypatch.setattr(factory, "GENERATION_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setattr(factory, "OPENROUTER_API_KEY", "test-key")
    model = service._build_model()
    request = components.ChatRequest(
        model=model.model_name, messages=[{"role": "user", "content": "Return JSON."}],
        reasoning=model.reasoning, response_format=model.model_kwargs["response_format"],
        provider=model.openrouter_provider,
    ).model_dump(by_alias=True)
    assert request["reasoning"] == {"effort": "none"}
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["provider"]["require_parameters"] is True


def test_other_model_profiles_keep_their_supported_json_mode(monkeypatch):
    from src.llms import factory
    options = []
    monkeypatch.setattr(factory, "GENERATION_MODEL", "other-model")
    monkeypatch.setattr(factory, "build_chat_model", lambda **kwargs: options.append(kwargs) or Model())
    service._build_model()
    assert options[0]["reasoning"] == {"effort": "none"}
    assert options[0]["model_kwargs"]["response_format"] == {"type": "json_object"}
    assert "openrouter_provider" not in options[0]



def test_production_parallel_reductions_use_isolated_clients_and_keep_final_client(monkeypatch):
    from src.llms import factory

    monkeypatch.setattr(service, "SYNTHESIS_INPUT_BYTES", 12_000)
    monkeypatch.setattr(service, "CHUNK_CHARS", 4_000)
    built, observed = [], []

    class ProviderModel(Model):
        model_name = "production-test-model"

    def build(**kwargs):
        instance = ProviderModel()
        built.append(instance)
        return instance

    def invoke(instance, messages):
        payload = json.loads(messages[-1].content)
        observed.append((id(instance), payload["mode"]))
        return instance.invoke(messages), {}

    monkeypatch.setattr(factory, "GENERATION_MODEL", "production-test-model")
    monkeypatch.setattr(factory, "build_chat_model", build)
    monkeypatch.setattr("src.llms.generation_observability.invoke_chat_with_observability", invoke)
    reports = [document(i, body=f"report {i} " + "x" * 2_500) for i in range(1, 13)]
    result = service.synthesize_corpus(corpus(reports))

    assert result["status"] == "completed"
    root_id = id(built[0])
    reduce_ids = [client_id for client_id, mode in observed if mode == "reduce"]
    assert len(reduce_ids) >= service.DOCUMENT_WORKERS
    assert len(reduce_ids) == len(set(reduce_ids))
    assert root_id not in reduce_ids
    assert [(client_id, mode) for client_id, mode in observed if client_id == root_id] == [(root_id, "report")]


@pytest.mark.parametrize("count", [125, 175, 200])
def test_large_corpora_retain_every_document_across_reduction_levels(count):
    model = Model()
    result = service.synthesize_corpus(corpus([document(i) for i in range(1, count + 1)]), model=model)

    expected = {f"doc-{i}" for i in range(1, count + 1)}
    assert result["status"] == "completed"
    assert result["counts"]["completed"] == count
    assert {item["report_uid"] for item in result["selected_sources"]} == expected
    assert {item["report_uid"] for item in result["evidence"]} == expected
    raw_inputs = {
        source["report_uid"]
        for call in model.calls
        for card in call["cards"] if card["kind"] == "raw"
        for source_id in card["source_ids"]
        for source in [call["sources"][source_id]]
    }
    assert raw_inputs == expected


def test_one_document_still_renders_a_completed_report():
    model = Model()
    result = service.synthesize_corpus(corpus([document()]), model=model)
    final = model.calls[-1]
    assert final["scope"]["company"] == "테스트회사" and final["scope"]["coverage"]["completed"] == 1
    assert "종합 결론" in result["content"] and "기간 내 기업 흐름" in result["content"]
    assert "단일 리포트를 근거" in result["content"]


def test_missing_required_final_section_fails_closed():
    class MissingConclusion(Model):
        def invoke(self, messages):
            output = super().invoke(messages)
            if json.loads(messages[-1].content)["mode"] == "report":
                output["statements"] = [item for item in output["statements"] if item["section"] != "conclusion"]
            return output

    result = service.synthesize_corpus(corpus([document()]), model=MissingConclusion())
    assert result["status"] == "failed" and result["counts"]["completed"] == 1 and result["content"] == ""


def test_default_factory_identity_is_reported_and_invalidates_cache(monkeypatch):
    model, cache = Model(), Cache()
    model.model_name = "actual-provider-model"
    monkeypatch.setattr("src.llms.factory.build_chat_model", lambda **kwargs: model)
    monkeypatch.setattr("src.llms.generation_observability.invoke_chat_with_observability",
                        lambda instance, messages: (instance.invoke(messages), {}))
    assert service.synthesize_corpus(corpus([document()]), cache=cache)["model"] == "actual-provider-model"
    model.model_name = "new-actual-model"
    model.calls.clear()
    assert service.synthesize_corpus(corpus([document()]), cache=cache)["model"] == "new-actual-model"
    assert len(model.calls) == 1
