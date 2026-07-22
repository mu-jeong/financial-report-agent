"""
PDF embedding pipeline.

Flow:
1. Extract PDF text or Markdown with the configured extraction engine.
2. Split extracted content into LangChain documents.
3. Embed chunks with the configured OpenRouter embedding model.
4. Store vectors in FAISS and mark source reports as embedded in SQLite.
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

# Make the project root importable when the file is executed directly.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# Flush progress logs promptly during long embedding jobs.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

from langchain_community.vectorstores import FAISS
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from src.configs import config
from src.core.db_manager import (
    fetch_unembedded,
    init_db,
    insert_parent_chunks,
    mark_embedded,
    mark_embedding_failed,
    sync_from_directory,
)
from src.retrieval.runtime_guard import guard_before_retrieval_write
from src.retrieval.update_lock import RetrievalUpdateLock

logger = config.get_logger(__name__)


class PdfExtractionError(ValueError):
    """Raised when the declared primary and fallback PDF extractors fail."""


def extract_pdf_text(*args, **kwargs):
    """Load the extractor only after the central runtime guard has run."""

    from src.core.pdf_extraction import extract_pdf_text as implementation

    return implementation(*args, **kwargs)


def build_embeddings_model(*args, **kwargs):
    """Load the embedding provider only after the central runtime guard has run."""

    from src.llms.embeddings import build_embeddings_model as implementation

    return implementation(*args, **kwargs)


def unembedded_extraction_engine() -> str:
    """Return the PDF extractor for pending/unembedded embedding jobs."""
    override = str(getattr(config, "UNEMBEDDED_EXTRACTION_ENGINE", "") or "").strip()
    return override or config.EXTRACTION_ENGINE


def extraction_fallback_engine() -> str:
    """Return the fallback used only after the primary PDF extractor fails."""

    return str(getattr(config, "EXTRACTION_FALLBACK_ENGINE", "") or "").strip()


def _validate_extraction_policy(
    primary_engine: str,
    fallback_engine: str,
    *,
    allow_fallback: bool,
) -> None:
    """Reject invalid global extractor configuration before parsing a PDF."""

    from src.core.pdf_extraction import normalize_engine

    normalize_engine(primary_engine)
    if allow_fallback and fallback_engine:
        normalize_engine(fallback_engine)


def sync_report_pdf_dir_env(pdf_dir: str | os.PathLike = config.SAVE_DIR) -> None:
    """Write the absolute PDF directory to .env for GUI open-file support."""
    env_path = config.BASE_DIR / ".env"
    report_pdf_dir = Path(pdf_dir).resolve().as_posix()
    env_line = f"REPORT_PDF_DIR={report_pdf_dir}"

    if env_path.exists():
        content = env_path.read_text(encoding="utf-8-sig")
        lines = content.splitlines()
        updated = False
        for index, line in enumerate(lines):
            if line.strip().startswith("REPORT_PDF_DIR="):
                lines[index] = env_line
                updated = True
                break
        if not updated:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(env_line)
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        env_path.write_text(env_line + "\n", encoding="utf-8")

    os.environ["REPORT_PDF_DIR"] = report_pdf_dir


def node_extract_pdf(state: dict) -> dict:
    """Extract text from a PDF and attach it to the graph state."""
    file_name = state["file_name"]
    pdf_path = os.path.join(config.SAVE_DIR, file_name)
    extraction_engine = state.get("extraction_engine") or unembedded_extraction_engine()
    fallback_engine = extraction_fallback_engine()

    allow_fallback = extraction_engine == config.EXTRACTION_ENGINE
    _validate_extraction_policy(
        extraction_engine,
        fallback_engine,
        allow_fallback=allow_fallback,
    )
    try:
        result = extract_pdf_text(
            pdf_path,
            extraction_engine,
            clean=True,
            allow_fallback=allow_fallback,
            fallback_engine=fallback_engine,
        )
    except Exception as exc:
        logger.error(f"  PDF extraction failed: {exc}")
        raise PdfExtractionError(
            f"Could not extract text from PDF {file_name}: {type(exc).__name__}: {exc}"
        ) from exc

    raw_text = result.text
    if result.used_engine != result.requested_engine:
        logger.info(f"  Extraction fallback used: {result.used_engine}")

    if not raw_text.strip():
        raise ValueError(f"Extracted text is empty: {file_name}")

    logger.info(f"  [1/3] Extracted {len(raw_text):,} characters")
    return {**state, "raw_text": raw_text, "extraction_engine": extraction_engine}


def node_split_documents(state: dict) -> dict:
    """Split extracted Markdown/text into retrieval chunks."""
    headers_to_split_on = [
        ("#", "Header 1"),
        ("##", "Header 2"),
        ("###", "Header 3"),
    ]

    markdown_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=headers_to_split_on,
        strip_headers=False,
    )
    header_splits = markdown_splitter.split_text(state["raw_text"])

    parent_docs = []
    child_docs = []

    if config.USE_PARENT_CHILD:
        logger.info("  [2/3] Splitting with parent-child chunking...")

        parent_splitter = RecursiveCharacterTextSplitter(
            chunk_size=config.PARENT_CHUNK_SIZE,
            chunk_overlap=int(config.PARENT_CHUNK_SIZE * 0.1),
            separators=["\n\n", "\n", ". ", " ", ""],
        )
        child_splitter = RecursiveCharacterTextSplitter(
            chunk_size=config.CHILD_CHUNK_SIZE,
            chunk_overlap=int(config.CHILD_CHUNK_SIZE * 0.1),
            separators=["\n\n", "\n", ". ", " ", ""],
        )

        parents = parent_splitter.split_documents(header_splits)

        for p_doc in parents:
            p_id = str(uuid.uuid4())
            p_doc.metadata.update(
                {
                    "parent_id": p_id,
                    "file_name": state["file_name"],
                    "target_name": state["target_name"],
                    "title": state["title"],
                    "report_date": state["report_date"],
                    "report_type": state.get("report_type", "company"),
                    "broker": state.get("broker", "unknown"),
                }
            )
            parent_docs.append(p_doc)

            children = child_splitter.split_documents([p_doc])
            for c_idx, c_doc in enumerate(children):
                c_doc.metadata.update(
                    {
                        "parent_id": p_id,
                        "child_index": c_idx,
                        "file_name": state["file_name"],
                        "target_name": state["target_name"],
                        "title": state["title"],
                        "report_date": state["report_date"],
                        "report_type": state.get("report_type", "company"),
                        "broker": state.get("broker", "unknown"),
                    }
                )
                header_context = f"[Company: {state['target_name']}, Title: {state['title']}]\n"
                c_doc.page_content = header_context + c_doc.page_content
                child_docs.append(c_doc)

        logger.info(
            f"  [2/3] Created {len(parent_docs)} parent chunks and "
            f"{len(child_docs)} child chunks"
        )
        return {**state, "documents": child_docs, "parent_documents": parent_docs}

    chunk_overlap = int(config.CHUNK_SIZE * 0.1)
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.CHUNK_SIZE,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )

    docs = text_splitter.split_documents(header_splits)

    for i, doc in enumerate(docs):
        doc.metadata.update(
            {
                "file_name": state["file_name"],
                "target_name": state["target_name"],
                "title": state["title"],
                "report_date": state["report_date"],
                "report_type": state.get("report_type", "company"),
                "broker": state.get("broker", "unknown"),
                "chunk_index": i,
            }
        )
        header_context = f"[Company: {state['target_name']}, Title: {state['title']}]\n"
        doc.page_content = header_context + doc.page_content

    logger.info(f"  [2/3] Created {len(docs)} chunks")
    return {**state, "documents": docs}


def node_embed_and_store(state: dict, embeddings_fn) -> dict:
    """Embed chunks, persist parent chunks if needed, and update the FAISS index."""
    logger.info("  [3/3] Updating FAISS index")

    docs = state["documents"]
    texts = [doc.page_content for doc in docs]
    metadatas = [doc.metadata for doc in docs]

    if config.USE_PARENT_CHILD and "parent_documents" in state:
        logger.info(f"  [3/3] Saving {len(state['parent_documents'])} parent chunks to SQLite...")
        parent_data = []
        for p_doc in state["parent_documents"]:
            parent_data.append(
                {
                    "id": p_doc.metadata["parent_id"],
                    "content": p_doc.page_content,
                    "file_name": p_doc.metadata["file_name"],
                    "metadata": json.dumps(p_doc.metadata, ensure_ascii=False),
                }
            )
        insert_parent_chunks(parent_data)

    logger.info(f"  [3/3] Embedding {len(docs)} chunks...")
    vectors = [[float(x) for x in vector] for vector in embeddings_fn.embed_documents(texts)]
    text_embeddings = list(zip(texts, vectors))

    faiss_index_file = os.path.join(config.FAISS_DIR, "index.faiss")
    if os.path.exists(faiss_index_file):
        logger.info("  [3/3] Loading existing FAISS index...")
        faiss_store = FAISS.load_local(
            config.FAISS_DIR,
            embeddings_fn,
            allow_dangerous_deserialization=True,
        )
        faiss_store.add_embeddings(text_embeddings, metadatas=metadatas)
    else:
        logger.info("  [3/3] Creating new FAISS index...")
        os.makedirs(config.FAISS_DIR, exist_ok=True)
        faiss_store = FAISS.from_embeddings(
            text_embeddings,
            embeddings_fn,
            metadatas=metadatas,
        )

    faiss_store.save_local(config.FAISS_DIR)
    logger.info(f"  [3/3] Saved {len(docs)} chunks to {config.FAISS_DIR}/")
    return {**state, "stored_count": len(docs)}


def node_mark_complete(state: dict) -> dict:
    """Mark a source report as embedded in SQLite."""
    mark_embedded(
        state["file_name"],
        extraction_engine=state.get("extraction_engine"),
    )
    logger.info("  SQLite updated: is_embedded=1")
    return state


def build_embeddings_fn():
    """Initialize the configured embeddings model."""
    return build_embeddings_model()


def run_pipeline(
    test_limit: int = config.TEST_LIMIT,
    *,
    continue_on_extraction_error: bool = False,
) -> int:
    """Run one supported update while holding the V1/V2 cutover fence."""

    with RetrievalUpdateLock(Path(config.DB_PATH).parent):
        return _run_pipeline_locked(
            test_limit,
            continue_on_extraction_error=continue_on_extraction_error,
        )


def _run_pipeline_locked(
    test_limit: int = config.TEST_LIMIT,
    *,
    continue_on_extraction_error: bool = False,
) -> int:
    """Run the embedding pipeline for pending reports. Return process-style exit code."""
    runtime = guard_before_retrieval_write(
        config.DB_PATH,
        allow_degraded_forward_recovery=True,
    )
    if runtime.is_native:
        from src.retrieval.build_service import (
            NativeSourceExtractionError,
            execute_incremental_update,
        )

        extraction_engine = unembedded_extraction_engine()
        fallback_engine = extraction_fallback_engine()
        allow_extraction_fallback = extraction_engine == config.EXTRACTION_ENGINE
        print("=" * 60)
        print("  Finance LLM Native V2 Incremental Update")
        print("=" * 60)
        if test_limit and test_limit > 0:
            logger.info("Native V2 ignores --limit and scans the complete source inventory.")
        try:
            completed = execute_incremental_update(
                config.DB_PATH,
                config.SAVE_DIR,
                data_root=runtime.paths.data_root,
                embeddings=build_embeddings_fn(),
                model=config.EMBEDDING_MODEL,
                extractor_name=extraction_engine,
                fallback_extractor_name=fallback_engine,
                allow_extraction_fallback=allow_extraction_fallback,
                use_parent_child=config.USE_PARENT_CHILD,
                single_chunk_size=config.CHUNK_SIZE,
                parent_chunk_size=config.PARENT_CHUNK_SIZE,
                child_chunk_size=config.CHILD_CHUNK_SIZE,
                metric="l2",
                normalization="none",
            )
        except NativeSourceExtractionError as exc:
            if continue_on_extraction_error:
                logger.warning(
                    "Native V2 PDF parsing failed; keeping the current snapshot and "
                    "continuing Quick Start: %s",
                    exc,
                )
                return 0
            logger.error(
                "Native V2 incremental update failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return 1
        except Exception as exc:
            logger.error(f"Native V2 incremental update failed: {type(exc).__name__}: {exc}")
            return 1
        if completed is None:
            logger.info("Native V2 is already current; no PDFs required processing.")
            return 0
        result, outcome = completed
        logger.info(
            "Native V2 publication complete: generation=%s epoch=%s reports=%s chunks=%s",
            outcome.publication_generation,
            outcome.write_epoch,
            result.report_count,
            result.chunk_count,
        )
        return 0
    print("=" * 60)
    print("  Finance LLM Embedding Pipeline")
    print("=" * 60)

    init_db()
    sync_report_pdf_dir_env(config.SAVE_DIR)
    sync_from_directory(config.SAVE_DIR)

    pending = fetch_unembedded()
    if not pending:
        logger.info("\nAll files are already embedded.")
        return

    targets = pending[:test_limit] if test_limit and test_limit > 0 else pending
    print(f"\nPending targets: {len(targets)} / total pending: {len(pending)}\n")

    embeddings_fn = build_embeddings_fn()

    success, failed, extraction_failed = 0, 0, 0

    for idx, row in enumerate(targets, 1):
        file_name = row["file_name"]
        print(f"\n[{idx}/{len(targets)}] {row['target_name']} - {row['title'][:40]}")

        if not os.path.exists(os.path.join(config.SAVE_DIR, file_name)):
            logger.warning("  File is missing; skipping.\n")
            mark_embedding_failed(
                file_name,
                "FileNotFoundError: PDF file is missing",
                extraction_engine=unembedded_extraction_engine(),
            )
            failed += 1
            continue

        state: dict = {
            "file_name": file_name,
            "target_name": row["target_name"],
            "title": row["title"],
            "report_date": row["report_date"],
            "report_type": row["report_type"],
            "broker": row["broker"],
            "extraction_engine": unembedded_extraction_engine(),
        }

        try:
            state = node_extract_pdf(state)
            state = node_split_documents(state)
            state = node_embed_and_store(state, embeddings_fn)
            state = node_mark_complete(state)
            success += 1
        except KeyboardInterrupt:
            logger.warning("[Interrupted] Stopping at user request.")
            break
        except Exception as exc:
            error_message = f"{type(exc).__name__}: {exc}"
            logger.error(f"  Error ({error_message})")
            mark_embedding_failed(
                file_name,
                error_message,
                extraction_engine=state.get("extraction_engine") or unembedded_extraction_engine(),
            )
            failed += 1
            if isinstance(exc, PdfExtractionError):
                extraction_failed += 1

        if idx < len(targets):
            print()
            time.sleep(2)

    print("\n" + "=" * 60)
    print(f"  Done: {success} succeeded / {failed} failed")
    faiss_size = (
        sum(
            os.path.getsize(os.path.join(config.FAISS_DIR, file_name))
            for file_name in os.listdir(config.FAISS_DIR)
            if os.path.isfile(os.path.join(config.FAISS_DIR, file_name))
        )
        if os.path.exists(config.FAISS_DIR)
        else 0
    )
    print(f"  FAISS index size: {faiss_size / 1024:.1f} KB")
    print(f"  Saved at: {os.path.abspath(config.FAISS_DIR)}/")
    print("=" * 60)
    if continue_on_extraction_error and extraction_failed:
        logger.warning(
            "Quick Start is continuing after %s PDF parsing failure(s).",
            extraction_failed,
        )
    fatal_failures = failed - extraction_failed
    if fatal_failures or (extraction_failed and not continue_on_extraction_error):
        return 1
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Finance LLM PDF embedding pipeline")
    parser.add_argument(
        "--limit",
        type=int,
        default=config.TEST_LIMIT,
        help=(
            "Maximum number of pending files to process in this run. "
            "Use 0 to process every pending file. Default: config.TEST_LIMIT."
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process every pending file. Equivalent to --limit 0.",
    )
    parser.add_argument(
        "--continue-on-extraction-error",
        action="store_true",
        help=(
            "Keep the current searchable data and return success when individual "
            "PDF parsing fails. Intended for Quick Start."
        ),
    )
    args = parser.parse_args(argv)
    raise SystemExit(
        run_pipeline(
            test_limit=0 if args.all else args.limit,
            continue_on_extraction_error=args.continue_on_extraction_error,
        )
    )


if __name__ == "__main__":
    main()
