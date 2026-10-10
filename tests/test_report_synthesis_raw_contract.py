"""Acceptance checks for preparing bodies locally and composing once."""

import json

from src.core import report_synthesis as service


class DirectWriter:
    model_name = "direct-test"

    def __init__(self, progress):
        self.calls = []
        self.progress = progress

    def invoke(self, messages):
        payload = json.loads(messages[-1].content)
        self.calls.append(payload)
        assert self.progress[-1]["phase"] == "synthesizing"
        assert payload["stage"] == "synthesize"
        references = [
            {"source_id": identifier, "quote_id": card["quotes"][0]["quote_id"]}
            for card in payload["cards"] for identifier in card["source_ids"]
        ]
        return {"statements": [
            {"section": section, "text": "Grounded company progression.", "references": references}
            for section in ("summary", "period_flow", "conclusion")
        ]}


def test_full_bodies_go_directly_to_one_writer_after_local_preparation():
    progress = []
    model = DirectWriter(progress)
    body = "Original beginning and revenue evidence. " + "x" * 9000 + " Final risk evidence."
    scope = {
        "company": "Example", "start_date": "2026-01-01", "end_date": "2026-02-28",
        "documents": [{
            "report_uid": "doc1", "broker": "Broker", "report_date": "2026-01-01",
            "parents": [{"parent_uid": "parent1", "parent_order": 0, "content": body}],
        }],
    }
    result = service.synthesize_corpus(scope, model=model, on_progress=progress.append)
    assert result["status"] == "completed"
    assert len(model.calls) == 1
    assert "".join(quote["quote"] for card in model.calls[0]["cards"] for quote in card["quotes"]) == body
    assert result["processing_mode"] == "raw_body"
    assert result["documents"][0]["status"] == "prepared"
    assert all(event["phase"] in {"preparing", "synthesizing"} for event in progress)
    for item in result["evidence"]:
        assert body[item["start"]:item["end"]] == item["quote"]


def test_unreadable_document_never_reaches_a_provider():
    model = DirectWriter([])
    result = service.synthesize_corpus({"documents": [{
        "report_uid": "broken", "parents": [{
            "parent_uid": "bad", "parent_order": 0, "content": None,
        }],
    }]}, model=model)
    assert result["status"] == "failed"
    assert result["counts"]["failed"] == 1
    assert not model.calls


def test_blank_quote_cannot_count_as_evidence_even_when_present_in_body():
    class BlankQuoteWriter(DirectWriter):
        def invoke(self, messages):
            output = super().invoke(messages)
            for statement in output["statements"]:
                for reference in statement["references"]:
                    reference["quote"] = " " * 8
            return output

    progress = []
    model = BlankQuoteWriter(progress)
    result = service.synthesize_corpus({"documents": [{
        "report_uid": "doc", "broker": "Broker", "report_date": "2026-01-01",
        "parents": [{"parent_uid": "parent", "parent_order": 0,
                     "content": "Meaningful company evidence." + " " * 8}],
    }]}, model=model, on_progress=progress.append)
    assert result["status"] == "failed"
    assert result["diagnostic"]["category"] == "evidence_mismatch"
    assert len(model.calls) == 1
