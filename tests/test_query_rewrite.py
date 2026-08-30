from src.nodes import query_rewrite


RECENT_HISTORY = [
    (
        "AI",
        "최근 답변에서 올릭스와 파마리서치를 언급했습니다. 올릭스는 로레알 협업, 파마리서치는 리쥬란 성장성이 핵심입니다.",
    )
]


def test_query_rewrite_does_not_inherit_history_for_clear_new_topic(monkeypatch):
    def fail_if_llm_rewrite_is_called(*args, **kwargs):
        raise AssertionError("LLM should not run for an independent date-scoped broad search")

    monkeypatch.setattr(query_rewrite, "build_chat_model", fail_if_llm_rewrite_is_called)

    result = query_rewrite.query_rewrite_node(
        {
            "question": "6월에 발간된 2차전지와 관련된 내용 알려줘",
            "chat_history": RECENT_HISTORY,
        }
    )

    assert result == {
        "rewritten_query": "6월에 발간된 2차전지와 관련된 내용 알려줘",
        "uses_chat_history": False,
        "followup_scope_intent": False,
    }


def test_history_decision_uses_semantic_classifier_for_implicit_followup(monkeypatch):
    calls = []

    def fake_llm_history_decision(question, history_text):
        calls.append((question, history_text))
        return True

    monkeypatch.setattr(query_rewrite, "_llm_history_decision", fake_llm_history_decision)

    assert query_rewrite.should_rewrite_with_history("언급된 회사에 대한 내용을 정리해줘", RECENT_HISTORY)
    assert calls
    assert "올릭스" in calls[0][1]


def test_history_decision_does_not_use_history_without_prior_context(monkeypatch):
    def fail_if_llm_history_decision_is_called(*args, **kwargs):
        raise AssertionError("history classifier should not run without prior history")

    monkeypatch.setattr(query_rewrite, "_llm_history_decision", fail_if_llm_history_decision_is_called)

    assert not query_rewrite.should_rewrite_with_history("언급된 회사에 대한 내용을 정리해줘", [])


def test_query_rewrite_detects_temporal_filter_followups_without_llm(monkeypatch):
    def fail_if_llm_history_decision_is_called(*args, **kwargs):
        raise AssertionError("date-only follow-up should be handled by deterministic guardrail")

    monkeypatch.setattr(query_rewrite, "_llm_history_decision", fail_if_llm_history_decision_is_called)

    assert query_rewrite.should_rewrite_with_history("6월에 발간된 내용만 정리해서 알려줘", RECENT_HISTORY)
    assert query_rewrite.should_rewrite_with_history("이번주 것만 알려줘", RECENT_HISTORY)
    assert query_rewrite.should_rewrite_with_history("올해 것만 알려줘", RECENT_HISTORY)


def test_query_rewrite_marks_prior_scope_followups_without_history():
    result = query_rewrite.query_rewrite_node(
        {
            "question": "주요 내용을 정리해줘",
            "chat_history": [],
        }
    )

    assert result == {
        "rewritten_query": "주요 내용을 정리해줘",
        "uses_chat_history": False,
        "followup_scope_intent": True,
    }


def test_history_decision_detects_prior_scope_followups_without_llm(monkeypatch):
    def fail_if_llm_history_decision_is_called(*args, **kwargs):
        raise AssertionError("scope follow-up should be handled by deterministic guardrail")

    monkeypatch.setattr(query_rewrite, "_llm_history_decision", fail_if_llm_history_decision_is_called)

    assert query_rewrite.should_rewrite_with_history("방금 내용 요약해줘", RECENT_HISTORY)


def test_query_rewrite_topic_detection_ignores_generic_date_filter_terms():
    assert not query_rewrite.has_explicit_search_topic("6월에 발간된 내용만 정리해서 알려줘")
    assert not query_rewrite.has_explicit_search_topic("6월 리포트 목록 알려줘")
    assert not query_rewrite.has_explicit_search_topic("올해 리포트 목록 알려줘")
    assert query_rewrite.has_explicit_search_topic("6월에 발간된 2차전지와 관련된 내용 알려줘")
    assert query_rewrite.has_explicit_search_topic("이번주 삼성전자 리포트 알려줘")


def test_history_decision_keeps_date_scoped_broad_search_independent(monkeypatch):
    def fail_if_llm_history_decision_is_called(*args, **kwargs):
        raise AssertionError("standalone date-scoped broad search should not call classifier")

    monkeypatch.setattr(query_rewrite, "_llm_history_decision", fail_if_llm_history_decision_is_called)

    assert not query_rewrite.should_rewrite_with_history(
        "6월에 발간된 2차전지와 관련된 내용 알려줘",
        RECENT_HISTORY,
    )


def test_scope_followup_marker_does_not_override_explicit_new_topic():
    result = query_rewrite.query_rewrite_node(
        {
            "question": "삼성전자 리포트들 각각 주요 내용 정리해줘",
            "chat_history": [],
        }
    )

    assert result == {
        "rewritten_query": "삼성전자 리포트들 각각 주요 내용 정리해줘",
        "uses_chat_history": False,
        "followup_scope_intent": False,
    }


def test_deictic_scope_marker_still_marks_followup_with_topic_words():
    result = query_rewrite.query_rewrite_node(
        {
            "question": "방금 리포트 리스크 알려줘",
            "chat_history": [],
        }
    )

    assert result["followup_scope_intent"] is True


def test_deictic_period_marker_marks_followup_with_explicit_target():
    result = query_rewrite.query_rewrite_node(
        {
            "question": "해당 기간 내에 발간된 sk하이닉스에 대한 리포트 정리해서 내용을 알려줘",
            "chat_history": [],
        }
    )

    assert result["followup_scope_intent"] is True


def test_query_rewrite_marks_section_deep_dive_as_prior_scope_followup():
    result = query_rewrite.query_rewrite_node(
        {
            "question": "개별종목 리포트에 대해 좀 더 자세히 작성해줘",
            "chat_history": [],
        }
    )

    assert result == {
        "rewritten_query": "개별종목 리포트에 대해 좀 더 자세히 작성해줘",
        "uses_chat_history": False,
        "followup_scope_intent": True,
    }


def test_query_rewrite_marks_ordinal_report_followup():
    for question in ("첫번째 리포트 정리해줘", "첫 번째 리포트 정리해줘"):
        result = query_rewrite.query_rewrite_node(
            {
                "question": question,
                "chat_history": [],
            }
        )

        assert result == {
            "rewritten_query": question,
            "uses_chat_history": False,
            "followup_scope_intent": True,
        }


def test_query_rewrite_does_not_treat_unrelated_ordinal_as_scope_followup():
    assert query_rewrite.is_scope_followup("첫 고객사 매출을 알려줘") is False
