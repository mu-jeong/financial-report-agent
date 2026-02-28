import sys
import os
import uuid
import streamlit as st

# 모듈 경로 추가 (finance_llm 패키지 접근)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.graphs.main_graph import graph_app
from src.configs.config import SEARCH_TOP_K

# 1. 페이지 초기 설정
st.set_page_config(
    page_title="Finance LLM RAG",
    page_icon="📈",
    layout="wide"
)

# 2. Session State 초기화 (데이터 영속성 보장)
if "threads" not in st.session_state:
    # 세션 딕셔너리 구조: { "thread_id": {"name": "대화방 이름", "messages": [{"role": "user/assistant", "content": "내용"}]} }
    default_id = str(uuid.uuid4())
    st.session_state.threads = {
        default_id: {"name": "새로운 대화", "messages": []}
    }
    st.session_state.current_thread_id = default_id

current_id = st.session_state.current_thread_id
current_thread = st.session_state.threads[current_id]

# 3. 사이드바 (Sidebar) - 다중 대화(Thread) 목록 관리 UI
with st.sidebar:
    st.title("📈 Finance LLM")
    st.markdown("증권사 분석 리포트 AI 어시스턴트")
    st.divider()
    
    # 3-1. 새 대화 시작 버튼
    if st.button("➕ 새 대화 시작", use_container_width=True):
        new_id = str(uuid.uuid4())
        st.session_state.threads[new_id] = {"name": f"대화 {len(st.session_state.threads) + 1}", "messages": []}
        st.session_state.current_thread_id = new_id
        st.rerun()  # UI 갱신을 위해 앱 재실행
    
    st.subheader("💬 대화 목록")
    # 3-2. 생성된 쓰레드(대화방)를 버튼으로 출력하여 이동 가능하게 구현
    for t_id, t_info in list(st.session_state.threads.items()):
        # 현재 활성화된 방을 시각적으로 구분
        btn_label = f"🟢 {t_info['name']}" if t_id == current_id else f"⚪ {t_info['name']}"
        
        # 버튼을 누르면 해당 방의 ID로 current_thread_id 교체
        if st.button(btn_label, key=f"btn_{t_id}", use_container_width=True):
            if t_id != current_id:
                st.session_state.current_thread_id = t_id
                st.rerun()
                
    st.divider()
    
    # 3-3. 현재 쓰레드(메모리) 초기화 기능
    if st.button("🗑️ 현재 대화 비우기", use_container_width=True):
        st.session_state.threads[current_id]["messages"] = [] # 프론트엔드 기록 삭제
        # LangGraph 백엔드 메모리(History)도 비우도록 thread_id 자체를 갱신
        new_id = str(uuid.uuid4())
        st.session_state.threads[new_id] = {"name": st.session_state.threads[current_id]["name"], "messages": []}
        del st.session_state.threads[current_id]
        st.session_state.current_thread_id = new_id
        st.rerun()

# 4. 메인 채팅 화면 영역
st.header(f"{current_thread['name']}")

# 4-1. 방에 돌아올 때마다 과거 대화 내용(messages 필드)을 순회하며 화면에 렌더링
for msg in current_thread["messages"]:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# 5. 사용자 채팅 입력부
if user_query := st.chat_input("질문을 입력해주세요... (ex: 최근 발행된 현대차 리포트 요약해줘)"):
    
    # "새로운 대화"라는 이름이라면, 사용자의 첫 질문 내용으로 방 제목 자동 변경
    if current_thread["name"] in ["새로운 대화"] or current_thread["name"].startswith("대화 "):
        st.session_state.threads[current_id]["name"] = user_query[:15] + "..."
    
    # 사용자의 질문을 화면에 표시하고 기록에 추가
    with st.chat_message("user"):
        st.markdown(user_query)
    current_thread["messages"].append({"role": "user", "content": user_query})

    # AI의 답변 처리 영역
    with st.chat_message("assistant"):
        message_placeholder = st.empty()
        
        # LangGraph 호출 및 결과 출력 (비동기 스트리밍 대신 invoke 사용 예시)
        with st.spinner("AI가 리포트 내용을 검색하고 분석 중입니다... 🔍"):
            # LangGraph의 config에 현재 thread_id를 넣어 맥락(Memory)을 유지시킴
            config = {"configurable": {"thread_id": current_id}}
            
            try:
                # 파이프라인(VectorDB 분기 or RDB 분기) 호출
                final_state = graph_app.invoke({"question": user_query}, config=config)
                full_response = final_state.get("generation", "응답을 생성하지 못했습니다.")
                
                # 라우팅 경로가 벡터 검색(VectorDB)이었고, 참조 문항이 반환되었다면 마크다운으로 깔끔하게 덧붙임
                if final_state.get("route") == "vectordb" and final_state.get("rerank_info"):
                    full_response += "\n\n---\n**📚 참고한 문서 (Source Context)**\n"
                    for info in final_state["rerank_info"]:
                        full_response += f"1. `{info['target_name']}` ({info['report_date']}) - {info['file_name']}\n"
                
                # DB 접근 가드레일 등에서 에러가 난 경우
                elif "Error" in full_response or "차단" in full_response:
                    full_response = f"⚠️ {full_response}"
                    
            except Exception as e:
                full_response = f"🚨 오류가 발생했습니다: {str(e)}"
        
        # AI 응답 출력 및 기록 저장
        message_placeholder.markdown(full_response)
        current_thread["messages"].append({"role": "assistant", "content": full_response})
