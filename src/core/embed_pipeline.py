"""
embed_pipeline.py
-----------------
PDF → PyMuPDF BBox 텍스트 추출 → LangChain 청킹 → Gemini 임베딩 → FAISS 저장

사용된 LangChain 컴포넌트:
    ┌─────────────────────────────────────────────────────────────┐
    │  RecursiveCharacterTextSplitter  텍스트 청킹                │
    │  GoogleGenerativeAIEmbeddings    임베딩 (gemini-embedding-001)│
    │  FAISS (langchain_community)     벡터 스토어 (영구 저장)    │
    │  Document (langchain_core)       문서 표준 포맷             │
    └─────────────────────────────────────────────────────────────┘

[텍스트 추출 방식]
  PyMuPDF(fitz) BBox 기반 표 제외:
  - page.find_tables()로 표 영역 BBox 수집
  - get_text("blocks")로 텍스트 블록 추출 후 표 BBox와 50% 이상 겹치는 블록 제거
  - 테스트 결과: 표 노이즈 0.0%, 가장 빠른 처리 속도

향후 LangGraph 파이프라인 구성 시 각 함수가 그대로 노드로 전환됩니다:

    graph.add_node("extract", node_extract_pdf)
    graph.add_node("split",   node_split_documents)
    graph.add_node("store",   node_embed_and_store)
    graph.add_node("mark",    node_mark_complete)

    graph.add_edge("extract", "split")
    graph.add_edge("split",   "store")
    graph.add_edge("store",   "mark")
"""

import os
import re
import sys
import time
import fitz          # PyMuPDF — BBox 기반 표 제외 텍스트 추출

# 프로젝트 루트 경로를 참조할 수 있도록 설정
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


# stdout 라인 버퍼링 해제
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)

from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_text_splitters import RecursiveCharacterTextSplitter, MarkdownHeaderTextSplitter
import pymupdf4llm

from src.core.db_manager import init_db, sync_from_directory, fetch_unembedded, mark_embedded
from src.configs import config
from src.utils.text_filters import is_sidebar_block, is_noise_line, strip_compliance

logger = config.get_logger(__name__)

# ── 환경설정 ─────────────────────────────────────────────────────────────────────

if not config.GEMINI_API_KEY or config.GEMINI_API_KEY == "your_api_key_here":
    logger.error("[오류] .env 파일에 GEMINI_API_KEY를 설정해주세요.")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════════
# 텍스트 정제 헬퍼
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph 노드 함수
# 각 함수의 입력·출력이 state dict 기반이므로 LangGraph 노드로 바로 전환 가능
# ═══════════════════════════════════════════════════════════════════════════════

def node_extract_pdf(state: dict) -> dict:
    """
    [LangGraph 노드: extract]
    PyMuPDF4LLM을 사용하여 PDF를 구조화된 Markdown으로 추출한 뒤 
    기존 필터(사이드바, 라인 노이즈, 준법고지)를 적용합니다.

    입력 state 키: file_name, report_date, target_name, title
    출력 state 키: + raw_text
    """
    file_name = state["file_name"]
    pdf_path  = os.path.join(config.SAVE_DIR, file_name)

    logger.info(f"  [1/3] PDF 텍스트 추출 중 (Markdown 방식)...")

    # ① PyMuPDF4LLM 기반 마크다운 추출
    try:
        md_text = pymupdf4llm.to_markdown(pdf_path, write_images=False)
    except Exception as e:
        logger.warning(f"  ⚠️ PyMuPDF4LLM 추출 실패: {e}")
        logger.warning(f"  🔄 일반 텍스트 추출 방식(fitz)으로 폴백합니다.")
        
        try:
            doc = fitz.open(pdf_path)
            md_text = ""
            for page in doc:
                # 일반 텍스트 추출 후 페이지 구분자 추가
                md_text += page.get_text("text") + "\n\n"
            doc.close()
            
            if not md_text.strip():
                raise ValueError("일반 추출로도 텍스트를 가져올 수 없습니다.")
                
        except Exception as fallback_e:
            logger.error(f"  ❌ 폴백 추출도 실패: {fallback_e}")
            raise ValueError(f"PDF에서 텍스트를 추출할 수 없습니다: {file_name}")

    if not md_text.strip():
        raise ValueError(f"PDF에서 텍스트가 비어있습니다: {file_name}")

    # ② 준법 고지(Compliance) 섹션 이후 제거
    md_text = strip_compliance(md_text)

    # ③ 블록 및 라인 단위 필터링 적용
    # 마크다운 블록(\n\n) 단위로 사이드바 여부 확인 및 라인 단위 노이즈 제거
    blocks = md_text.split("\n\n")
    clean_blocks = []
    
    for blk in blocks:
        # 블록 단위 필터 (STOCK DATA 등)
        if is_sidebar_block(blk):
            continue
            
        # 라인 단위 필터
        lines = blk.split("\n")
        filtered_lines = [
            line for line in lines 
            if not is_noise_line(line)
        ]
        clean_blk = "\n".join(filtered_lines).strip()
        if clean_blk:
            clean_blocks.append(clean_blk)

    raw_text = "\n\n".join(clean_blocks)

    if not raw_text.strip():
        raise ValueError(f"정제 후 남은 내용이 없습니다: {file_name}")

    logger.info(f"  [1/3] 완료 — {len(raw_text):,}자 추출 (Markdown 정제 완료)")
    return {**state, "raw_text": raw_text}


def node_split_documents(state: dict) -> dict:
    """
    [LangGraph 노드: split]
    추출된 마크다운 텍스트를 MarkdownHeaderTextSplitter로 1차 분할한 뒤, 
    RecursiveCharacterTextSplitter로 2차 청킹합니다.
    
    이 방식은 단순히 균등 사이즈로 나누는 것이 아니라, 
    문서의 헤더(#, ## 등) 구조를 보존하여 논리적 일관성을 유지합니다.

    입력 state 키: raw_text + 메타데이터
    출력 state 키: + documents (List[Document])
    """
    # 1. 마크다운 헤더 기반 분할
    headers_to_split_on = [
        ("#", "Header 1"),
        ("##", "Header 2"),
        ("###", "Header 3"),
    ]
    
    # strip_headers=False를 통해 본문 내 헤더를 텍스트로 보존 (검색 풍부도 향상)
    markdown_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=headers_to_split_on,
        strip_headers=False
    )
    
    header_splits = markdown_splitter.split_text(state["raw_text"])

    # 2. 너무 큰 섹션 청킹 (RecursiveCharacterTextSplitter)
    # 헤더 구조 내에서 최대 CHUNK_SIZE를 넘지 않도록 보조적으로 작동
    # 청크 오버랩을 사이즈의 약 10%로 동적 설정
    chunk_overlap = int(config.CHUNK_SIZE * 0.1)
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.CHUNK_SIZE,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""]
    )
    
    docs = text_splitter.split_documents(header_splits)

    # 3. 메타데이터 후처리 및 헤더 텍스트 주입
    for i, doc in enumerate(docs):
        # 문서별 메타데이터 업데이트
        doc.metadata.update({
            "file_name":   state["file_name"],
            "target_name": state["target_name"],
            "title":       state["title"],
            "report_date": state["report_date"],
            "broker":      state.get("broker", "알수없음"),
            "chunk_index": i
        })
        # FAISS 벡터 검색 시 의미론적 맥락을 위해 본문 시작부에 기업명/제목 주입
        header_context = f"[기업: {state['target_name']}, 제목: {state['title']}]\n"
        doc.page_content = header_context + doc.page_content

    logger.info(f"  [2/3] 완료 — {len(docs)}개 청크 생성 (Markdown Header 기반)")
    return {**state, "documents": docs}


def node_embed_and_store(state: dict, embeddings_fn: GoogleGenerativeAIEmbeddings) -> dict:
    """
    [LangGraph 노드: store]
    Document 리스트를 임베딩 후 FAISS에 저장합니다.

    ChromaDB 대신 FAISS 사용 — Windows 환경에서 안정적, LangChain 네이티브 지원.
    FAISS 파일은 faiss_db/ 에 영구 저장됩니다.
    """
    logger.info("  [3/3] FAISS 저장 시작")

    docs      = state["documents"]
    file_name = state["file_name"]
    texts     = [doc.page_content for doc in docs]
    metadatas = [doc.metadata for doc in docs]

    # ① 임베딩 호출 (GoogleGenerativeAIEmbeddings)
    logger.info(f"  [3/3] 임베딩 중... ({len(docs)}개 청크)")
    vectors = [[float(x) for x in v] for v in embeddings_fn.embed_documents(texts)]
    # ② FAISS 로드 또는 새로 생성
    # 폴더가 아니라 실제 index.faiss 파일이 있는지 확인 (폴더만 있고 파일이 없는 경우 방어)
    text_embeddings = list(zip(texts, vectors))   # List[(text, vector)]
    faiss_index_file = os.path.join(config.FAISS_DIR, "index.faiss")
    if os.path.exists(faiss_index_file):
        logger.info(f"  [3/3] 기존 FAISS 인덱스 로드 중...")
        faiss_store = FAISS.load_local(
            config.FAISS_DIR, embeddings_fn,
            allow_dangerous_deserialization=True
        )
        faiss_store.add_embeddings(text_embeddings, metadatas=metadatas)
    else:
        logger.info(f"  [3/3] FAISS 인덱스 신규 생성 중...")
        os.makedirs(config.FAISS_DIR, exist_ok=True)
        faiss_store = FAISS.from_embeddings(
            text_embeddings, embeddings_fn, metadatas=metadatas
        )

    faiss_store.save_local(config.FAISS_DIR)
    logger.info(f"  [3/3] 완료 — {len(docs)}개 청크 저장 ({config.FAISS_DIR}/)")
    return {**state, "stored_count": len(docs)}


def node_mark_complete(state: dict) -> dict:
    """
    [LangGraph 노드: mark]
    SQLite의 is_embedded 플래그를 1로 업데이트합니다.

    입력 state 키: file_name
    """
    mark_embedded(state["file_name"])
    logger.info(f"  ✅ SQLite 업데이트 완료 (is_embedded=1)")
    return state


# ═══════════════════════════════════════════════════════════════════════════════
# 파이프라인 실행
# ═══════════════════════════════════════════════════════════════════════════════

def build_embeddings_fn() -> GoogleGenerativeAIEmbeddings:
    """Gemini 임베딩 함수 초기화."""
    return GoogleGenerativeAIEmbeddings(
        model=config.EMBEDDING_MODEL,
        google_api_key=config.GEMINI_API_KEY,
        task_type="retrieval_document",
    )


def run_pipeline(test_limit: int = config.TEST_LIMIT) -> None:
    """
    미처리 PDF를 최대 test_limit개 선택하여 전체 파이프라인 실행.
    (test_limit가 0이거나 None이면 전체 미처리 파일 처리)

    순서: extract → split → store → mark
    """
    print("=" * 60)
    print("  Finance LLM — Embedding Pipeline (테스트 모드)")
    print("=" * 60)

    # SQLite 초기화 + downloaded/ 폴더 동기화
    init_db()
    sync_from_directory(config.SAVE_DIR)

    pending = fetch_unembedded()
    if not pending:
        logger.info("\n✅ 모든 파일이 이미 임베딩 완료 상태입니다.")
        return

    # test_limit가 0이거나 None이면 전체 처리, 그 외에는 슬라이싱
    if test_limit and test_limit > 0:
        targets = pending[:test_limit]
    else:
        targets = pending
    print(f"\n📄 처리 대상: {len(targets)}건 (전체 미처리: {len(pending)}건)\n")

    embeddings_fn = build_embeddings_fn()

    success, failed = 0, 0

    for idx, row in enumerate(targets, 1):
        file_name = row["file_name"]
        print(f"\n[{idx}/{len(targets)}] {row['target_name']} — {row['title'][:40]}")

        if not os.path.exists(os.path.join(config.SAVE_DIR, file_name)):
            logger.warning(f"  ⚠️  파일 없음, 건너뜀\n")
            failed += 1
            continue

        # 초기 state 구성 (LangGraph의 GraphState에 해당)
        state: dict = {
            "file_name":   file_name,
            "target_name": row["target_name"],
            "title":       row["title"],
            "report_date": row["report_date"],
            "broker":      row["broker"],
        }

        try:
            # ── 노드 순차 실행 ───────────────────────────────────────────────
            # LangGraph 전환 시 아래 4줄이 그대로 그래프 엣지가 됩니다.
            state = node_extract_pdf(state)
            state = node_split_documents(state)
            state = node_embed_and_store(state, embeddings_fn)
            state = node_mark_complete(state)
            # ─────────────────────────────────────────────────────────────────

            success += 1

        except KeyboardInterrupt:
            logger.warning("[중단] 사용자가 강제로 종료했습니다.")
            break
        except Exception as e:
            # 예상치 못한 일반 프로세스 예외 (BaseException 대신 표준 Exception만 포착)
            logger.error(f"  ❌ 오류 ({type(e).__name__}): {e}")
            failed += 1

        # 파일 간 API 레이트 리밋 방지
        if idx < len(targets):
            print()
            time.sleep(2)

    # 결과 요약
    print("\n" + "=" * 60)
    print(f"  완료: {success}건 성공 / {failed}건 실패")
    faiss_size = sum(
        os.path.getsize(os.path.join(config.FAISS_DIR, f))
        for f in os.listdir(config.FAISS_DIR)
        if os.path.isfile(os.path.join(config.FAISS_DIR, f))
    ) if os.path.exists(config.FAISS_DIR) else 0
    print(f"  FAISS 인덱스 크기: {faiss_size / 1024:.1f} KB")
    print(f"  저장 위치: {os.path.abspath(config.FAISS_DIR)}/")
    print("=" * 60)


if __name__ == "__main__":
    run_pipeline()
