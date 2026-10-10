"""Formatting tolerance must preserve claims, evidence, and fail-closed validation."""
import json

import pytest

from src.core import report_synthesis as service
from tests.test_report_synthesis import Cache, Model, corpus, document
from src.utils.citations import validate_citation_contract


@pytest.mark.parametrize("text,expected", [
    ("<b>매출 개선</b>과 <strong>위험</strong>", "매출 개선과 위험"),
    ("<em>전망</em>은 <i>조건부</i>다.", "전망은 조건부다."),
    ("첫 문장<br>둘째 문장<BR />셋째 문장", "첫 문장\n둘째 문장\n셋째 문장"),
    ("<p>초기 상황</p><p>후기 전망</p>", "초기 상황\n\n후기 전망"),
    ("<div><span>실적</span>이 개선됐다.</div>", "실적이 개선됐다."),
    ("10<sup>2</sup>와 H<sub>2</sub>의 <u>변화</u>", "10^(2)와 H_(2)의 변화"),
    ("<p><strong>PBR < 1배</strong>와 ROE > 10%</p>", "PBR < 1배와 ROE > 10%"),
])
def test_benign_analysis_formatting_becomes_plain_text(text, expected):
    assert service._text(text, 1400) == expected
    assert service._text(expected, 1400) == expected


@pytest.mark.parametrize("text,reason", [
    ("<b onclick='alert(1)'>분석</b>", "generated_markup"),
    ("<span style='display:none'>분석</span>", "generated_markup"),
    ("<a href='https://example.com'>분석</a>", "generated_markup"),
    ("<script>비공개</script>", "generated_markup"),
    ("<iframe src='https://example.com'></iframe>", "generated_markup"),
    ("<img src=x onerror=alert(1)>", "generated_markup"),
    ("<!-- 숨겨진 분석 -->본문", "generated_markup"),
    ("<unknown>분석</unknown>", "generated_markup"),
    ("<style>분석</style>", "generated_markup"),
    ("<svg>분석</svg>", "generated_markup"),
    ("<!DOCTYPE html>분석", "generated_markup"),
    ("<?xml version='1.0'?>분석", "generated_markup"),
    ("<b>분석", "generated_markup"),
    ("<b><i>분석</b></i>", "generated_markup"),
    ("분석</b>", "generated_markup"),
    ("<br></br>분석", "generated_markup"),
    ("<scr<b>ipt</b>>분석</scr<b>ipt</b>>", "generated_markup"),
    ("근거 <b>[</b>1]", "citation_marker"),
    ("<<b>https</b>://example.com>", "generated_link"),
    ("<b", "generated_markup"),
    ("</b", "generated_markup"),
    ("<script", "generated_markup"),
    ("<!-- hidden", "generated_markup"),
    ("<!DOCTYPE html", "generated_markup"),
    ("<?xml", "generated_markup"),
    ("<svg/onload=alert(1)>분석", "generated_markup"),
    ("<img/src=x/onerror=alert(1)>", "generated_markup"),
    ("<x=foo onclick=alert(1)>분석", "generated_markup"),
    ("<x<b>=foo</b> onclick=alert(1)>분석", "generated_markup"),
    ("<sup></sup>", "text_empty"),
    ("<sub> </sub>", "text_empty"),
    ("<sup><b></b></sup>", "text_empty"),
    ("<b>근거 [1]</b>", "citation_marker"),
    ("<p>[회사](https://example.com)</p>", "generated_link"),
    ("<b></b><br><p> </p>", "text_empty"),
])
def test_plain_text_conversion_does_not_bypass_validation(text, reason):
    with pytest.raises(service.SynthesisOutputError) as failure:
        service._text(text, 1400)
    assert failure.value.reason == reason


def test_normalized_length_and_literal_entities():
    assert service._text("<b>" + "가" * 1400 + "</b>", 1400) == "가" * 1400
    with pytest.raises(service.SynthesisOutputError) as failure:
        service._text("<b>" + "가" * 1401 + "</b>", 1400)
    assert failure.value.reason == "text_length"
    literal = "&lt;script&gt;본문&lt;/script&gt;와 PBR &lt; 1"
    assert service._text(literal, 1400) == literal


def test_formatted_analysis_is_verified_and_cached_without_changing_source_quotes():
    class FormattedWriter(Model):
        def invoke(self, messages):
            output = super().invoke(messages)
            for statement in output["statements"]:
                statement["text"] = "<p><strong>기업의 수익성</strong>이 개선됐다.</p><p>위험은 남아 있다.</p>"
            return output

    model, cache = FormattedWriter(), Cache()
    source = corpus([document(1, body="<b>원문 서식</b>도 그대로 인용해야 합니다."), document(2)])
    first = service.synthesize_corpus(source, model=model, cache=cache)
    second = service.synthesize_corpus(source, model=model, cache=cache)
    assert first["status"] == second["status"] == "completed"
    assert first["content"] == second["content"] and first["evidence"] == second["evidence"]
    assert all(item["text"] == "기업의 수익성이 개선됐다.\n\n위험은 남아 있다."
               for item in first["statements"])
    assert validate_citation_contract(first["selected_sources"], first["citation_contract"])["status"] == "valid"
    originals = {parent["parent_uid"]: parent["content"]
                 for item in source["documents"] for parent in item["parents"]}
    for item in first["evidence"]:
        assert originals[item["parent_uid"]][item["start"]:item["end"]] == item["quote"]
    assert "<b>원문 서식</b>" in first["evidence"][0]["quote"]
    assert "<strong>" not in json.dumps(cache.entries, ensure_ascii=False)
    assert len(model.calls) == 1


def test_manual_retry_after_markup_failure_reuses_verified_reductions(monkeypatch):
    monkeypatch.setattr(service, "SYNTHESIS_INPUT_BYTES", 12_000)
    monkeypatch.setattr(service, "CHUNK_CHARS", 4_000)

    class FailFinalOnce(Model):
        failed = False

        def invoke(self, messages):
            output = super().invoke(messages)
            final = json.loads(messages[-1].content)["mode"] == "report"
            for statement in output["statements"]:
                statement["text"] = "<p><strong>기업 상황</strong>을 원문에 따라 분석했습니다.</p>"
            if final and not self.failed:
                self.failed = True
                output["statements"][0]["text"] = "<script>private</script>"
            return output

    source = corpus([document(i, body=f"report {i} " + "x" * 2500) for i in range(1, 13)])
    cache, model = Cache(), FailFinalOnce()
    first = service.synthesize_corpus(source, model=model, cache=cache)
    assert first["status"] == "failed" and first["diagnostic"]["reason"] == "generated_markup"
    reductions = [call for call in model.calls if call["mode"] == "reduce"]
    assert len(reductions) >= 2 and cache.entries
    model.calls.clear()
    second = service.synthesize_corpus(source, model=model, cache=cache)
    assert second["status"] == "completed"
    assert [call["mode"] for call in model.calls] == ["report"]
