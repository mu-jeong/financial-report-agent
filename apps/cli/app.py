import sys
import os
import uuid

# 모듈 경로 추가 (finance_llm 패키지 접근)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.graphs.main_graph import graph_app
from src.configs.config import SEARCH_TOP_K

def run_search(query: str, thread_id: str = "default_thread") -> dict:
    """
    주어진 질문(query)에 대해 LangGraph 기반 RAG 파이프라인을 실행합니다.
    - thread_id: 대화 맥락을 유지하기 위한 세션 식별자
    """
    config = {"configurable": {"thread_id": thread_id}}
    return graph_app.invoke({"question": query}, config=config)

def run_cli():
    current_thread_id = str(uuid.uuid4())
    print("\n============================================================")
    print("  📈 Finance LLM Query Assistant (with LangGraph Router)")
    print("  (종료하려면 'q' 또는 'quit' 입력 | 메모리 초기화하려면 'c' 또는 'clear' 입력)")
    print("============================================================")
    
    while True:
        try:
            user_query = input("\n💡 질문을 입력하세요: ").strip()
            if not user_query:
                continue
            if user_query.lower() in ['q', 'quit', 'exit']:
                print("\n이용해 주셔서 감사합니다. 종료합니다.")
                break
            if user_query.lower() in ['c', 'clear']:
                current_thread_id = str(uuid.uuid4())
                print("\n🔄 대화 메모리가 초기화되었습니다.")
                continue
            
            print(f"\n{'='*60}")
            print("\n" + "=" * 60)
            print("  ✨ AI 답변")
            print("=" * 60)
            
            # 스트리밍 출력은 node 함수 내부에서 처리되므로 여기선 결과 반환만 대기
            final_state = run_search(user_query, thread_id=current_thread_id)
            
            # VectorDB 라우팅이었을 경우 참고 문서 출력
            if final_state.get("route") == "vectordb" and final_state.get("rerank_info"):
                print("\n" + "-" * 60)
                print("  📚 참고한 문서들 (Top " + str(SEARCH_TOP_K) + ")")
                print("-" * 60)
                for info in final_state["rerank_info"]:
                    print(f"  [{info['rank']}] {info['target_name']} ({info['report_date']})")
                    print(f"       파일명: {info['file_name']}")
                    # print(f"       Rerank Score: {info['score']:.4f}")
                    
            print("=" * 60)
            
        except KeyboardInterrupt:
            print("\n\n프로그램을 종료합니다.")
            break

if __name__ == "__main__":
    run_cli()
