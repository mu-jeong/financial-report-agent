import os
from langchain_google_genai import GoogleGenerativeAIEmbeddings, ChatGoogleGenerativeAI
from langchain_community.vectorstores import FAISS
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_core.output_parsers import StrOutputParser

from src.configs.config import GEMINI_API_KEY, EMBEDDING_MODEL, GENERATION_MODEL, FAISS_DIR, SEARCH_TOP_K, USE_RERANKER, get_logger
from src.configs.prompts import VECTORDB_PROMPT
from src.graphs.state import State
from src.utils.ranker import get_ranker

logger = get_logger(__name__)

def build_embeddings_fn() -> GoogleGenerativeAIEmbeddings:
    return GoogleGenerativeAIEmbeddings(
        model=EMBEDDING_MODEL,
        google_api_key=GEMINI_API_KEY,
        task_type="retrieval_query",
    )

def vectordb_node(state: State) -> dict:
    query = state.get("rewritten_query", state["question"])
    if not os.path.exists(FAISS_DIR):
        msg = "죄송합니다. faiss_db/ 폴더가 없습니다. 먼저 embed_pipeline.py를 실행하여 리포트를 학습시켜주세요."
        logger.warning(msg)
        return {"generation": msg, "chat_history": [("사용자", state["question"]), ("AI", msg)]}

    embeddings_fn = build_embeddings_fn()
    faiss_store = FAISS.load_local(
        FAISS_DIR, embeddings_fn,
        allow_dangerous_deserialization=True
    )

    docs_with_scores = faiss_store.similarity_search_with_score(query, k=SEARCH_TOP_K)

    if not docs_with_scores:
        msg = "죄송합니다. 제공된 리포트에서는 관련 문서를 찾을 수 없습니다."
        logger.info(msg)
        return {"generation": msg, "chat_history": [("사용자", state["question"]), ("AI", msg)]}

    passages = []
    
    if USE_RERANKER:
        for rank, (doc, score) in enumerate(docs_with_scores):
            meta = doc.metadata
            passages.append({
                "id": rank,
                "text": doc.page_content,
                "meta": meta
            })
            
        ranker, req_cls = get_ranker()
        rerank_request = req_cls(query=query, passages=passages)
        rerank_results = ranker.rerank(rerank_request)
        
        # 상위 3개만 선별
        top_passages = rerank_results[:3]
    else:
        for rank, (doc, score) in enumerate(docs_with_scores[:SEARCH_TOP_K]):
            meta = doc.metadata
            passages.append({
                "id": rank,
                "text": doc.page_content,
                "score": score,
                "meta": meta
            })
        top_passages = passages
        
    context_text = ""
    for rank, result in enumerate(top_passages, 1):
        meta = result['meta']
        source_info = f"[{rank}] {meta.get('target_name', '알수없음')} ({meta.get('report_date', '날짜없음')}) - {meta.get('title', '제목없음')}"
        context_text += f"\n--- 문서 {rank} ---\n[출처: {source_info}]\n{result['text']}\n"

    llm = ChatGoogleGenerativeAI(
        model=GENERATION_MODEL,
        google_api_key=GEMINI_API_KEY,
        temperature=0.2,
    )

    prompt = PromptTemplate.from_template(VECTORDB_PROMPT)

    chain = (
        {"context": lambda x: context_text, "question": RunnablePassthrough()}
        | prompt
        | llm
        | StrOutputParser()
    )

    answer = ""
    for chunk in chain.stream(query):
        answer += chunk
        print(chunk, end="", flush=True) 
    print()
    
    rerank_info = []
    for rank, result in enumerate(top_passages, 1):
        meta = result['meta']
        score = float(result.get('score', 0.0))  # float32 -> float (JSON 직렬화 오류 방지)
        rerank_info.append({
            "rank": rank,
            "target_name": meta.get('target_name', '-'),
            "report_date": meta.get('report_date', '-'),
            "broker": meta.get('broker', '-'),
            "file_name": meta.get('file_name', '-'),
            "score": score
        })
        
    return {
        "generation": answer, 
        "faiss_context": context_text, 
        "rerank_info": rerank_info,
        "chat_history": [("사용자", state["question"]), ("AI", answer)]
    }
