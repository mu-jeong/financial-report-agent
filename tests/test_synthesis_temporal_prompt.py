"""Temporal input coverage and stage-specific provider reasoning contracts."""

from copy import deepcopy

import pytest

from src.core import report_synthesis as service
from tests.test_report_synthesis import Cache, Model, corpus, document


def _input(dates, *, final=True):
    passages, cards = {}, []
    for index, report_date in enumerate(dates, 1):
        item = document(index)
        item["report_date"] = report_date
        _, prepared, inputs = service._prepare(item)
        passages.update(prepared)
        cards.extend(inputs)
    return service._payload(cards, passages, {}, final=final)[0]


@pytest.mark.parametrize("dates,labels", [
    (["2026-01-10"], ["단일 시점"]),
    (["2026-01-10", "2026-08-20"], ["초기", "후기"]),
    (["2026-01-10", "2026-03-15", "2026-08-20"], ["초기", "중기", "후기"]),
])
def test_temporal_plan_uses_only_observed_dates_and_does_not_invent_middle(dates, labels):
    payload = _input(list(reversed(dates)))
    plan = payload["requirements"]["period_flow_plan"]
    assert plan["date_basis"] == "report_publication_date"
    assert plan["grouping_basis"] == "observed_publication_dates"
    assert [period["label"] for period in plan["periods"]] == labels
    assert [day for period in plan["periods"] for day in period["report_dates"]] == dates
    assert {identifier for period in plan["periods"] for identifier in period["source_ids"]} == set(payload["sources"])
    for period in plan["periods"]:
        assert period["first_report_date"] == period["report_dates"][0]
        assert period["last_report_date"] == period["report_dates"][-1]
        assert all(payload["sources"][identifier]["report_date"] in period["report_dates"]
                   for identifier in period["source_ids"])


@pytest.mark.parametrize("dates,phase_ids", [
    (["2026-01-10"], ["snapshot"]),
    (["2026-01-10", "2026-08-20"], ["opening", "closing"]),
    (["2026-01-10", "2026-03-15", "2026-08-20"], ["opening", "middle", "closing"]),
])
def test_temporal_plan_exposes_stable_phase_identifiers(dates, phase_ids):
    payload = _input(dates)
    assert [period["id"] for period in payload["requirements"]["period_flow_plan"]["periods"]] == phase_ids


def test_many_same_day_reports_do_not_shift_temporal_groups():
    payload = _input(["2026-01-10"] * 20 + ["2026-02-01", "2026-03-01", "2026-06-01", "2026-08-20"])
    periods = payload["requirements"]["period_flow_plan"]["periods"]
    assert [period["report_dates"] for period in periods] == [
        ["2026-01-10", "2026-02-01"], ["2026-03-01", "2026-06-01"], ["2026-08-20"]]
    assert [period["document_count"] for period in periods] == [21, 2, 1]


def test_reduction_keeps_publication_dates_and_final_notes_rebuild_the_plan():
    passages, cards = {}, []
    for index in range(1, 4):
        _, prepared, inputs = service._prepare(document(index))
        passages.update(prepared)
        cards.extend(inputs)
    source_ids = list(passages)
    notes = [{"kind": "notes", "text": "Verified progression", "source_ids": source_ids,
              "references": [{"source_id": identifier, "quote": passages[identifier]["text"],
                              "start": 0, "end": len(passages[identifier]["text"])} for identifier in source_ids]}]
    payload, _ = service._payload(notes, passages, {}, final=True)
    assert [period["report_dates"] for period in payload["requirements"]["period_flow_plan"]["periods"]] == [
        ["2026-09-01"], ["2026-09-02"], ["2026-09-03"]]
    assert "requirements" not in service._payload(cards, passages, {}, final=False)[0]


@pytest.mark.parametrize("model_name", ["deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro"])
def test_deepseek_reasoning_is_disabled_on_the_sdk_wire(monkeypatch, model_name):
    from openrouter import components
    from src.llms import factory
    monkeypatch.setattr(factory, "GENERATION_MODEL", model_name)
    monkeypatch.setattr(factory, "OPENROUTER_API_KEY", "test-key")
    model = service._build_model()
    request = components.ChatRequest(
        model=model.model_name, messages=[{"role": "user", "content": "Return JSON."}],
        reasoning=model.reasoning, max_tokens=model.max_tokens,
        response_format=model.model_kwargs["response_format"], provider=model.openrouter_provider,
    ).model_dump(by_alias=True)
    assert request["reasoning"] == {"effort": "none"}
    assert request["max_tokens"] == 12000
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["provider"]["require_parameters"] is True
    assert model.max_retries == 0


@pytest.mark.parametrize("change", ["reasoning", "max_tokens"])
def test_reasoning_profile_changes_do_not_reuse_previous_final_output(change):
    model, cache, source = Model(), Cache(), corpus([document()])
    model.reasoning = {"effort": "none"}
    model.max_tokens = 12000
    first = service.synthesize_corpus(source, model=model, cache=cache)
    assert first["status"] == "completed"
    model.calls.clear()
    if change == "reasoning":
        model.reasoning = {"effort": "high"}
    else:
        model.max_tokens = 60000
    second = service.synthesize_corpus(deepcopy(source), model=model, cache=cache)
    assert second["status"] == "completed"
    assert len(model.calls) == 1
    assert len(cache.entries) == 2


def test_endpoint_selections_do_not_attribute_middle_paragraph_to_other_periods():
    payload = _input(["2026-01-10", "2026-03-15", "2026-08-20"])
    periods = payload["requirements"]["period_flow_plan"]["periods"]
    quotes = service._offered_quotes(payload)
    selected = [next(key for key, choice in quotes.items() if choice["source_id"] in period["source_ids"])
                for period in periods]
    output = {"summary": [{"text": "Summary", "references": [{"quote_id": selected[0]}]}],
              "conclusion": [{"text": "Conclusion", "references": [{"quote_id": selected[-1]}]}],
              "additional_statements": [], "period_flow": [
                  {"text": period["label"], "references": [{"quote_id": quote}],
                   "opening_quote_id": selected[0], "closing_quote_id": selected[-1]}
                  for period, quote in zip(periods, selected)]}
    normalized = service._normalize_output(output, payload)
    flow = [item for item in normalized["statements"] if item["section"] == "period_flow"]
    assert [{ref["source_id"] for ref in item["references"]} for item in flow] == [
        set(period["source_ids"]) for period in periods]
    # Even an endpoint not attached to this paragraph must be a supplied quote.
    output["period_flow"][1]["closing_quote_id"] = "invented"
    with pytest.raises(service.SynthesisEvidenceError):
        service._normalize_output(output, payload)
    output["period_flow"][1]["closing_quote_id"] = selected[1]
    with pytest.raises(service.SynthesisOutputError, match="기간 경계"):
        service._normalize_output(output, payload)


def _structured_period_output(payload):
    periods = payload["requirements"]["period_flow_plan"]["periods"]
    quotes = service._offered_quotes(payload)
    selected = {
        period["id"]: next(
            quote_id for quote_id, choice in quotes.items()
            if choice["source_id"] in period["source_ids"]
        )
        for period in periods
    }
    first_quote = selected[periods[0]["id"]]
    last_quote = selected[periods[-1]["id"]]
    flow = {}
    for period in periods:
        phase_id = period["id"]
        phase = {
            "text": f"{period['label']}의 변화",
            "references": [{"quote_id": selected[phase_id]}],
            "phase_quote_id": selected[phase_id],
        }
        if phase_id in {"snapshot", "opening"}:
            phase["opening_quote_id"] = first_quote
        if phase_id in {"snapshot", "closing"}:
            phase["closing_quote_id"] = last_quote
        flow[phase_id] = phase
    return {
        "summary": [{"text": "Summary", "references": [{"quote_id": first_quote}]}],
        "period_flow": flow,
        "conclusion": [{"text": "Conclusion", "references": [{"quote_id": last_quote}]}],
        "additional_statements": [],
    }


def test_structured_period_flow_gets_local_labels_and_phase_scoped_evidence():
    payload = _input(["2026-01-10", "2026-03-15", "2026-08-20"])
    periods = payload["requirements"]["period_flow_plan"]["periods"]
    normalized = service._normalize_output(_structured_period_output(payload), payload)
    flow = [item for item in normalized["statements"] if item["section"] == "period_flow"]
    assert [item["text"] for item in flow] == [
        f"{period['label']}(발행일 "
        f"{period['first_report_date'] if period['first_report_date'] == period['last_report_date'] else period['first_report_date'] + '~' + period['last_report_date']}): "
        f"{period['label']}의 변화"
        for period in periods
    ]
    assert [{ref["source_id"] for ref in item["references"]} for item in flow] == [
        set(period["source_ids"]) for period in periods]


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_structured_period_flow_rejects_missing_or_extra_phase_keys(mutation):
    payload = _input(["2026-01-10", "2026-03-15", "2026-08-20"])
    output = _structured_period_output(payload)
    if mutation == "missing":
        del output["period_flow"]["middle"]
    else:
        output["period_flow"]["unexpected"] = deepcopy(output["period_flow"]["middle"])
    with pytest.raises(service.SynthesisOutputError):
        service._normalize_output(output, payload)


def test_structured_period_flow_rejects_phase_anchor_from_another_period():
    payload = _input(["2026-01-10", "2026-03-15", "2026-08-20"])
    output = _structured_period_output(payload)
    output["period_flow"]["middle"]["phase_quote_id"] = output["period_flow"]["opening"]["phase_quote_id"]
    with pytest.raises(service.SynthesisOutputError):
        service._normalize_output(output, payload)


@pytest.mark.parametrize("text,reason", [
    ("", "text_empty"),
    (" \n\t ", "text_empty"),
    ("<b></b><br><p> </p>", "text_empty"),
    ("x" * 801, "text_length"),
])
def test_local_period_label_cannot_mask_invalid_phase_body(text, reason):
    payload = _input(["2026-01-10", "2026-03-15", "2026-08-20"])
    output = _structured_period_output(payload)
    output["period_flow"]["middle"]["text"] = text
    with pytest.raises(service.SynthesisOutputError) as failure:
        service._normalize_output(output, payload)
    assert failure.value.reason == reason


def test_structured_period_flow_keeps_exact_raw_quote_spans_after_validation():
    passages, cards = {}, []
    originals = {}
    for index, report_date in enumerate(("2026-01-10", "2026-03-15", "2026-08-20"), 1):
        item = document(index, body=f"Exact raw evidence for phase {index}. " * 12)
        item["report_date"] = report_date
        originals[item["report_uid"]] = item["parents"][0]["content"]
        _, prepared, inputs = service._prepare(item)
        passages.update(prepared)
        cards.extend(inputs)
    payload, aliases = service._payload(cards, passages, {}, final=True)
    normalized = service._normalize_output(_structured_period_output(payload), payload)
    verified = service._validate_statements(
        normalized, passages, aliases, final=True, quotes=service._offered_quotes(payload))
    flow = [item for item in verified if item["section"] == "period_flow"]
    assert len(flow) == 3
    for statement in flow:
        for evidence in statement["evidence"]:
            original = originals[evidence["report_uid"]]
            assert original[evidence["start"]:evidence["end"]] == evidence["quote"]
