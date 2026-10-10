"""Provider output contracts for missing sections and invented quote identifiers."""
import json
from copy import deepcopy

import pytest

from src.core import report_synthesis as service
from tests.test_report_synthesis import Cache, Model, QuoteIdModel, corpus, document


def _payload(final=True):
    _, passages, cards = service._prepare(document(body="매출 전망과 위험을 설명하는 원문입니다. " * 50))
    payload, _ = service._payload(cards, passages, {}, final=final)
    return payload


def test_final_schema_requires_all_core_sections_and_only_supplied_quotes():
    payload = _payload()
    schema = service._response_schema(payload)
    assert schema["required"] == ["summary", "period_flow", "conclusion", "additional_statements"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["additional_statements"]["maxItems"] == 6
    for section in ("summary", "conclusion"):
        block = schema["properties"][section]
        assert block["type"] == "array" and block["minItems"] == 1
        assert block["maxItems"] == 3
        assert block["items"]["properties"]["text"]["maxLength"] == 800
        assert block["items"]["properties"]["references"]["maxItems"] == 16
        reference = block["items"]["properties"]["references"]["items"]
        assert reference["required"] == ["quote_id"]
        assert reference["additionalProperties"] is False
        assert set(reference["properties"]) == {"quote_id"}
        assert set(reference["properties"]["quote_id"]["enum"]) == {
            quote["quote_id"] for card in payload["cards"] for quote in card["quotes"]}


def test_reduction_schema_keeps_the_analysis_memo_contract():
    schema = service._response_schema(_payload(final=False))
    assert schema["required"] == ["statements"]
    assert schema["properties"]["statements"]["maxItems"] == 8


def test_period_flow_schema_requires_each_planned_phase_and_scoped_anchors():
    passages, cards = {}, []
    for index in (1, 2, 3):
        _, prepared, raw = service._prepare(document(index))
        passages.update(prepared)
        cards.extend(raw)
    payload, _ = service._payload(cards, passages, {}, final=True)
    plan = payload["requirements"]["period_flow_plan"]["periods"]
    assert [period["id"] for period in plan] == ["opening", "middle", "closing"]
    flow = service._response_schema(payload)["properties"]["period_flow"]
    assert flow["type"] == "object"
    assert flow["additionalProperties"] is False
    assert flow["required"] == [period["id"] for period in plan]
    assert set(flow["properties"]) == set(flow["required"])
    choices = service._offered_quotes(payload)
    for period in plan:
        phase = flow["properties"][period["id"]]
        assert phase["additionalProperties"] is False
        assert {"text", "references", "phase_quote_id"}.issubset(phase["required"])
        assert phase["properties"]["references"]["maxItems"] == 13
        assert set(phase["properties"]["phase_quote_id"]["enum"]) == {
            quote_id for quote_id, choice in choices.items()
            if choice["source_id"] in period["source_ids"]
        }
    opening = flow["properties"]["opening"]
    closing = flow["properties"]["closing"]
    assert "opening_quote_id" in opening["required"]
    assert "closing_quote_id" not in opening["required"]
    assert "closing_quote_id" in closing["required"]
    assert "opening_quote_id" not in closing["required"]
    for phase, field, expected_date in (
        (opening, "opening_quote_id", "2026-09-01"),
        (closing, "closing_quote_id", "2026-09-03"),
    ):
        enum = phase["properties"][field]["enum"]
        assert enum and all(
            payload["sources"][choices[key]["source_id"]]["report_date"] == expected_date
            for key in enum
        )


def test_single_period_schema_uses_snapshot_with_both_endpoint_anchors():
    payload = _payload()
    plan = payload["requirements"]["period_flow_plan"]["periods"]
    assert [period["id"] for period in plan] == ["snapshot"]
    flow = service._response_schema(payload)["properties"]["period_flow"]
    snapshot = flow["properties"]["snapshot"]
    assert flow["required"] == ["snapshot"]
    assert {"phase_quote_id", "opening_quote_id", "closing_quote_id"}.issubset(snapshot["required"])


def test_final_schema_falls_back_to_legacy_period_flow_array_without_dated_plan():
    payload = _payload()
    del payload["requirements"]["period_flow_plan"]
    flow = service._response_schema(payload)["properties"]["period_flow"]
    assert flow["type"] == "array"
    assert flow["items"]["properties"]["references"]["maxItems"] == 14


def test_duplicate_planned_phase_identifiers_are_rejected():
    passages, cards = {}, []
    for index in (1, 2, 3):
        _, prepared, raw = service._prepare(document(index))
        passages.update(prepared)
        cards.extend(raw)
    payload, _ = service._payload(cards, passages, {}, final=True)
    payload["requirements"]["period_flow_plan"]["periods"][0]["id"] = "middle"
    with pytest.raises(service.SynthesisOutputError):
        service._response_schema(payload)


def test_selected_endpoint_quotes_are_restored_before_local_temporal_validation():
    class EndpointWriter(GroupedWriter):
        def invoke(self, messages):
            payload = json.loads(messages[-1].content)
            output = json.loads(super().invoke(messages))
            choices = service._offered_quotes(payload)
            identifiers = list(choices)
            flow = output["period_flow"][0]
            flow["references"] = [deepcopy(output["summary"][0]["references"][1])]
            flow["opening_quote_id"] = identifiers[0]
            flow["closing_quote_id"] = identifiers[-1]
            return output
    result = service.synthesize_corpus(corpus([document(1), document(2), document(3)]), model=EndpointWriter())
    assert result["status"] == "completed"
    flow = next(item for item in result["statements"] if item["section"] == "period_flow")
    assert {item["report_uid"] for item in flow["provenance"]} == {"doc-1", "doc-2", "doc-3"}


class GroupedWriter(QuoteIdModel):
    def invoke(self, messages):
        result = super().invoke(messages)
        grouped = {section: [] for section in ("summary", "period_flow", "conclusion", "additional_statements")}
        for statement in result["statements"]:
            for reference in statement["references"]:
                reference.pop("source_id")
            section = statement.pop("section")
            if section in grouped:
                grouped[section].append(statement)
            else:
                grouped["additional_statements"].append({"section": section, **statement})
        return json.dumps(grouped, ensure_ascii=False)


def _strict_period_flow(grouped, payload):
    """Adapt the legacy fake writer to the strict phase-shaped provider contract."""
    template = grouped["period_flow"][0]
    quotes = service._offered_quotes(payload)
    periods = payload["requirements"]["period_flow_plan"]["periods"]
    endpoints = payload["requirements"]["period_flow_endpoint_sources"]
    endpoint_dates = sorted(endpoints)
    opening_quote = next(
        quote_id for quote_id, choice in quotes.items()
        if choice["source_id"] in endpoints[endpoint_dates[0]])
    closing_quote = next(
        quote_id for quote_id, choice in quotes.items()
        if choice["source_id"] in endpoints[endpoint_dates[-1]])
    flow = {}
    for period in periods:
        phase_quote = next(
            quote_id for quote_id, choice in quotes.items()
            if choice["source_id"] in period["source_ids"])
        item = {"text": template["text"], "references": [{"quote_id": phase_quote}],
                "phase_quote_id": phase_quote}
        if period["id"] in {"snapshot", "opening"}:
            item["opening_quote_id"] = opening_quote
        if period["id"] in {"snapshot", "closing"}:
            item["closing_quote_id"] = closing_quote
        flow[period["id"]] = item
    grouped["period_flow"] = flow
    return grouped


def test_grouped_report_restores_exact_provenance_and_round_trips_cache():
    model, cache = GroupedWriter(), Cache()
    source = corpus([document(1), document(2)])
    first = service.synthesize_corpus(source, model=model, cache=cache)
    assert first["status"] == "completed"
    assert {item["report_uid"] for item in first["evidence"]} == {"doc-1", "doc-2"}
    originals = {doc["report_uid"]: doc["parents"][0]["content"] for doc in source["documents"]}
    assert all(originals[item["report_uid"]][item["start"]:item["end"]] == item["quote"] for item in first["evidence"])
    second = service.synthesize_corpus(source, model=model, cache=cache)
    assert second["evidence"] == first["evidence"] and len(model.calls) == 1


@pytest.mark.parametrize("mutation,reason", [("missing_section", "required_sections"),
                                          ("empty_section", "required_sections"),
                                          ("wrong_shape", "statement_shape"),
                                          ("unknown_quote", "quote_id_invalid")])
def test_bad_grouped_responses_fail_without_cache_or_another_call(mutation, reason):
    class BadWriter(GroupedWriter):
        def invoke(self, messages):
            grouped = json.loads(super().invoke(messages))
            if mutation == "missing_section":
                del grouped["conclusion"]
            elif mutation == "empty_section":
                grouped["conclusion"] = []
            elif mutation == "wrong_shape":
                grouped["summary"] = "do not validate this response"
            else:
                grouped["summary"][0]["references"][0]["quote_id"] = "not-supplied"
            return grouped
    model, cache = BadWriter(), Cache()
    result = service.synthesize_corpus(corpus([document()]), model=model, cache=cache)
    assert result["status"] == "failed" and result["diagnostic"]["reason"] == reason
    assert not result["content"] and not cache.entries and len(model.calls) == 1


def test_source_cannot_be_overridden_when_quote_id_determines_provenance():
    class TamperedWriter(GroupedWriter):
        def invoke(self, messages):
            grouped = json.loads(super().invoke(messages))
            grouped["summary"][0]["references"][0]["source_id"] = "p2"
            return grouped
    result = service.synthesize_corpus(corpus([document(1), document(2)]), model=TamperedWriter())
    assert result["status"] == "failed" and result["diagnostic"]["reason"] == "quote_id_invalid"


def test_each_strict_provider_call_receives_its_current_quote_choices(monkeypatch):
    class StrictWriter(GroupedWriter):
        def invoke(self, messages):
            payload = json.loads(messages[-1].content)
            return _strict_period_flow(json.loads(super().invoke(messages)), payload)

    model = StrictWriter()
    model.model_kwargs = {"response_format": {"type": "json_schema"}}
    bindings = []
    model.bind = lambda **kwargs: bindings.append(deepcopy(kwargs)) or model
    monkeypatch.setattr("src.llms.generation_observability.invoke_chat_with_observability",
                        lambda instance, messages: (instance.invoke(messages), {}))
    payload = _payload()
    service._invoke(model, service._SYNTHESIS_PROMPT, payload, observed=True)
    assert len(bindings) == 1
    response_format = bindings[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["schema"] == service._response_schema(payload)


def test_strict_temporal_normalization_rejects_legacy_period_flow_array():
    payload = _payload()
    output = json.loads(GroupedWriter().invoke([service.HumanMessage(content=json.dumps(payload))]))
    assert isinstance(output["period_flow"], list)
    with pytest.raises(service.SynthesisOutputError):
        service._normalize_output(output, payload, strict_temporal=True)


def test_request_byte_budget_includes_the_generated_response_schema():
    payload = _payload()
    serialized = len(service._SYNTHESIS_PROMPT.encode("utf-8")) + len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    assert service._input_size(service._SYNTHESIS_PROMPT, payload) > serialized
    assert service._input_size(service._SYNTHESIS_PROMPT, payload, include_response_schema=False) == serialized


def test_duplicate_quote_ids_with_different_spans_cannot_replace_provenance():
    payload = _payload()
    choices = payload["cards"][0]["quotes"]
    choices[1]["quote_id"] = choices[0]["quote_id"]
    with pytest.raises(service.SynthesisEvidenceError):
        service._response_schema(payload)


@pytest.mark.parametrize("section", ["summary", "period_flow", "conclusion"])
def test_additional_statements_cannot_replace_core_sections(section):
    class DuplicateWriter(GroupedWriter):
        def invoke(self, messages):
            grouped = json.loads(super().invoke(messages))
            grouped["additional_statements"] = [{"section": section, **deepcopy(grouped[section][0])}]
            return grouped
    result = service.synthesize_corpus(corpus([document()]), model=DuplicateWriter())
    assert result["status"] == "failed" and result["diagnostic"]["reason"] == "statement_shape"


@pytest.mark.parametrize("core_count,additional_count,reason", [(7, 0, "required_sections"), (6, 7, "statements_shape")])
def test_normalized_report_respects_section_and_total_limits(core_count, additional_count, reason):
    class OversizedWriter(GroupedWriter):
        def invoke(self, messages):
            grouped = json.loads(super().invoke(messages))
            for section in ("summary", "period_flow", "conclusion"):
                grouped[section] = [deepcopy(grouped[section][0]) for _ in range(core_count)]
            grouped["additional_statements"] = [{"section": "outlook", **deepcopy(grouped["summary"][0])}
                                                 for _ in range(additional_count)]
            return grouped
    result = service.synthesize_corpus(corpus([document()]), model=OversizedWriter())
    assert result["status"] == "failed" and result["diagnostic"]["reason"] == reason


def test_notes_quote_ids_are_numbered_per_source_and_enumerated():
    passages, cards = {}, []
    for index in (1, 2):
        _, prepared, raw = service._prepare(document(index))
        passages.update(prepared)
        cards.extend(raw)
    payload, aliases = service._payload(cards, passages, {}, final=False)
    output = QuoteIdModel().invoke([service.HumanMessage(content=json.dumps(payload))])
    verified = service._validate_statements(output, passages, aliases, final=False,
                                            quotes=service._offered_quotes(payload))
    notes_payload, _ = service._payload([service._notes(verified)], passages, {}, final=True)
    references = notes_payload["cards"][0]["references"]
    assert {reference["quote_id"] for reference in references} == {"p1q1", "p2q1"}
    schema = service._response_schema(notes_payload)
    assert set(schema["properties"]["summary"]["items"]["properties"]["references"]["items"]["properties"]["quote_id"]["enum"]) == {"p1q1", "p2q1"}


def test_hierarchical_grouped_writer_and_cache_preserve_current_quote_ids(monkeypatch):
    monkeypatch.setattr(service, "SYNTHESIS_INPUT_BYTES", 20000)
    class HierarchicalWriter(GroupedWriter):
        def invoke(self, messages):
            payload = json.loads(messages[-1].content)
            if payload["mode"] == "reduce":
                return QuoteIdModel.invoke(self, messages)
            return _strict_period_flow(json.loads(super().invoke(messages)), payload)
    model, cache = HierarchicalWriter(), Cache()
    model.model_kwargs = {"response_format": {"type": "json_schema"}}
    model.bind = lambda **_kwargs: model
    source = corpus([document(index, body=f"Report {index}: " + "전망 근거입니다. " * 250) for index in range(1, 9)])
    result = service.synthesize_corpus(source, model=model, cache=cache)
    assert result["status"] == "completed"
    assert model.calls[-1]["mode"] == "report" and all(card["kind"] == "notes" for card in model.calls[-1]["cards"])
    assert {item["report_uid"] for item in result["evidence"]} == {f"doc-{index}" for index in range(1, 9)}
    assert all(service._input_size(service._SYNTHESIS_PROMPT if call["mode"] == "report" else service._REDUCTION_PROMPT, call)
               <= service.SYNTHESIS_INPUT_BYTES for call in model.calls)
    model.calls.clear()
    restored = service.synthesize_corpus(source, model=model, cache=cache)
    assert restored["evidence"] == result["evidence"] and not model.calls


def test_raw_batches_can_use_a_larger_final_notes_envelope(monkeypatch):
    monkeypatch.setattr(service, "RAW_SYNTHESIS_INPUT_BYTES", 16000)
    monkeypatch.setattr(service, "SYNTHESIS_INPUT_BYTES", 40000)
    class StrictQuoteIdModel(GroupedWriter):
        def invoke(self, messages):
            payload = json.loads(messages[-1].content)
            if payload["mode"] == "reduce":
                return QuoteIdModel.invoke(self, messages)
            return _strict_period_flow(json.loads(super().invoke(messages)), payload)

    model = StrictQuoteIdModel()
    model.model_kwargs = {"response_format": {"type": "json_schema"}}
    model.bind = lambda **_kwargs: model
    source = corpus([document(index, body=f"기업 {index}의 원문입니다. " * 100) for index in range(1, 21)])
    result = service.synthesize_corpus(source, model=model)
    assert result["status"] == "completed"
    raw_calls = [call for call in model.calls if any(card["kind"] == "raw" for card in call["cards"])]
    assert len(raw_calls) >= 2 and all(call["mode"] == "reduce" for call in raw_calls)
    assert all(service._input_size(service._REDUCTION_PROMPT, call) <= 16000 for call in raw_calls)
    notes_calls = [call for call in model.calls if all(card["kind"] == "notes" for card in call["cards"])]
    assert len(notes_calls) == 1 and notes_calls[0]["mode"] == "report"
    assert 16000 < service._input_size(service._SYNTHESIS_PROMPT, notes_calls[0]) <= 40000
    assert result["coverage"] == {"prepared": 20, "cited": 20, "uncited": 0}
