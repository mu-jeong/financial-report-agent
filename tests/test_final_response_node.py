from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import src.graphs.main_graph as main_graph


class FakeChatModel:
    def __init__(self, *args, **kwargs):
        pass

    def invoke(self, messages):
        assert isinstance(messages[0], HumanMessage)
        assert isinstance(messages[-1], ToolMessage)
        return AIMessage(content="도구 결과를 반영한 최종 답변")


def test_final_response_node_returns_only_new_message_delta(monkeypatch):
    monkeypatch.setattr(
        "src.llms.factory.build_chat_model",
        lambda temperature=0.2, **kwargs: FakeChatModel(),
    )
    existing_messages = [
        HumanMessage(content="원 질문과 검색 문맥"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_stock_price",
                    "args": {"company_name": "삼성전자"},
                    "id": "call-1",
                }
            ],
        ),
        ToolMessage(content="최근 주가 데이터", tool_call_id="call-1"),
    ]

    result = main_graph.final_response_node(
        {
            "question": "삼성전자 리포트와 현재 주가를 알려줘",
            "messages": existing_messages,
        }
    )

    assert result["generation"] == "도구 결과를 반영한 최종 답변"
    assert result["messages"] == [AIMessage(content="도구 결과를 반영한 최종 답변")]
    assert result["chat_history"] == [
        ("사용자", "삼성전자 리포트와 현재 주가를 알려줘"),
        ("AI", "도구 결과를 반영한 최종 답변"),
    ]


def test_final_response_node_uses_existing_generation_without_messages():
    result = main_graph.final_response_node(
        {
            "question": "저장된 리포트 수는?",
            "generation": "총 10건입니다.",
            "messages": [HumanMessage(content="기존 메시지")],
        }
    )

    assert result == {
        "generation": "총 10건입니다.",
        "chat_history": [("사용자", "저장된 리포트 수는?"), ("AI", "총 10건입니다.")],
    }
