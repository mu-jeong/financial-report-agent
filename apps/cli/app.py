import sys
import os
import argparse

# 모듈 경로 추가 (finance_llm 패키지 접근)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.configs.config import SEARCH_TOP_K
from src.core.conversation_store import append_message, delete_thread, ensure_thread, get_chat_history
from src.core.status import format_status_text

def run_search(query: str, thread_id: str = "default_thread", chat_history: list | None = None) -> dict:
    """
    주어진 질문(query)에 대해 LangGraph 기반 RAG 파이프라인을 실행합니다.
    - thread_id: 대화 맥락을 유지하기 위한 세션 식별자
    """
    from src.graphs.main_graph import graph_app

    config = {"configurable": {"thread_id": thread_id}}
    return graph_app.invoke({"question": query, "chat_history": chat_history or []}, config=config)

def run_cli():
    current_thread_id = "cli_default"
    ensure_thread(current_thread_id, "CLI 기본 대화")
    print("\n============================================================")
    print("  📈 Finance LLM Query Assistant (with LangGraph Router)")
    print("  (종료: q/quit | 메모리 초기화: c/clear | 데이터 상태: status)")
    print("============================================================")
    print(format_status_text())
    
    while True:
        try:
            user_query = input("\n💡 질문을 입력하세요: ").strip()
            if not user_query:
                continue
            if user_query.lower() in ['q', 'quit', 'exit']:
                print("\n이용해 주셔서 감사합니다. 종료합니다.")
                break
            if user_query.lower() in ['c', 'clear']:
                delete_thread(current_thread_id)
                ensure_thread(current_thread_id, "CLI 기본 대화")
                print("\n🔄 대화 메모리가 초기화되었습니다.")
                continue
            if user_query.lower() in ['s', 'status']:
                print("\n" + format_status_text())
                continue
            
            # 1. 그래프 실행 (진행 상태 표시)
            print("\n🤖 답변을 생성하고 있습니다...", end="", flush=True)
            prior_history = get_chat_history(current_thread_id)
            append_message(current_thread_id, "user", user_query)
            final_state = run_search(user_query, thread_id=current_thread_id, chat_history=prior_history)
            print("\r" + " " * 30 + "\r", end="", flush=True) # 진행 상태 메시지 지우기
            
            # 2. 검색 쿼리 재작성 결과 출력 (디버깅/사용자 확인용)
            rewritten = final_state.get("rewritten_query")
            if rewritten and rewritten != user_query:
                print(f"🔍 검색어 재구성: {rewritten}\n")

            # 3. 최종 답변 출력
            print("=" * 60)
            answer = final_state.get("generation")
            if answer:
                print(answer)
                append_message(
                    current_thread_id,
                    "assistant",
                    answer,
                    {"rerank_info": final_state.get("rerank_info", [])},
                )
            else:
                # 만약 Tool 호출 등으로 인해 generation이 직접 반환되지 않고 messages에 있을 경우
                messages = final_state.get("messages", [])
                if messages:
                    last_msg = messages[-1]
                    if hasattr(last_msg, "content") and last_msg.content:
                        print(last_msg.content)
                        append_message(current_thread_id, "assistant", str(last_msg.content))
                    else:
                        print("\n(답변을 생성하는 중에 도구가 호출되었거나 응답이 비어있습니다.)")
                else:
                    print("\n(답변이 생성되지 않았습니다. 시스템 로그를 확인해주세요.)")

            # 4. VectorDB 라우팅이었을 경우 참고 문서 출력
            if final_state.get("route") == "vectordb" and final_state.get("rerank_info"):
                print("\n" + "-" * 60)
                print("  📚 참고한 문서들 (Top " + str(SEARCH_TOP_K) + ")")
                print("-" * 60)
                for info in final_state["rerank_info"]:
                    score = info.get("score")
                    score_text = f" | score={score:.4f}" if isinstance(score, float) else ""
                    print(f"  [{info['rank']}] {info['target_name']} ({info['report_date']}){score_text}")
                    print(f"       증권사: {info.get('broker', '-')}")
                    print(f"       파일명: {info['file_name']}")
                    
            print("=" * 60)
            
        except KeyboardInterrupt:
            print("\n\n프로그램을 종료합니다.")
            break

def main():
    parser = argparse.ArgumentParser(description="Finance LLM CLI")
    parser.add_argument(
        "--status",
        action="store_true",
        help="LLM 그래프를 실행하지 않고 로컬 데이터/인덱스 상태만 출력합니다.",
    )
    args = parser.parse_args()

    if args.status:
        print(format_status_text())
        return

    run_cli()

if __name__ == "__main__":
    main()
