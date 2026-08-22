"""
PDF embedding pipeline.

Flow:
1. Extract PDF text or Markdown with the configured extraction engine.
2. Split extracted content into LangChain documents.
3. Embed chunks with the configured OpenRouter embedding model.
4. Publish chunks through the Native V2 continuous-update service.
"""

import argparse
import os
import sys
from pathlib import Path

# Make the project root importable when the file is executed directly.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# Flush progress logs promptly during long embedding jobs.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

from src.configs import config
from src.retrieval.bootstrap import reconcile_and_inspect_runtime
from src.retrieval.runtime_guard import guard_before_retrieval_write
from src.retrieval.update_lock import RetrievalUpdateLock

logger = config.get_logger(__name__)


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


def build_embeddings_fn():
    """Initialize the configured embeddings model."""
    return build_embeddings_model()


def run_pipeline(
    *,
    retry_extraction_failures: bool = False,
) -> int:
    """Run one Native V2 update while holding the retrieval update lock."""

    reconcile_and_inspect_runtime(config.DATA_ROOT)
    with RetrievalUpdateLock(config.DATA_ROOT):
        return _run_pipeline_locked(
            retry_extraction_failures=retry_extraction_failures,
        )


def _run_pipeline_locked(
    *,
    retry_extraction_failures: bool = False,
) -> int:
    """Run the embedding pipeline for pending reports. Return process-style exit code."""
    runtime = guard_before_retrieval_write(
        config.DATA_ROOT,
        allow_degraded_forward_recovery=True,
        allow_empty_preflight=True,
    )
    if not runtime.is_native:
        raise RuntimeError("Native V2 retrieval runtime is required for embedding updates")

    from src.retrieval.build_service import NativeSourceExtractionError
    from src.retrieval.continuous_update import (
        DEFAULT_CONTINUOUS_REPORT_BATCH_SIZE,
        execute_continuous_update,
    )

    extraction_engine = unembedded_extraction_engine()
    fallback_engine = extraction_fallback_engine()
    allow_extraction_fallback = extraction_engine == config.EXTRACTION_ENGINE
    print("=" * 60)
    print("  Finance LLM Native V2 Incremental Update")
    print("=" * 60)
    if getattr(runtime, "is_empty", False) and not _has_source_pdfs(config.SAVE_DIR):
        logger.info(
            "Native V2 is initialized and no source PDFs exist; no update is required."
        )
        return 0
    try:
        embeddings = build_embeddings_fn()
        attempted_report_uids: set[str] = set()
        completed_batch_count = 0

        def report_delta(result) -> None:
            nonlocal completed_batch_count
            completed_batch_count += 1
            attempted_report_uids.update(result.attempted_report_uids)
            logger.info(
                "Native V2 delta publication complete: batch=%s "
                "delta_generation=%s batch_attempted=%s processed=%s "
                "published=%s failed=%s deferred=%s",
                completed_batch_count,
                result.sequence,
                len(result.attempted_report_uids),
                len(attempted_report_uids),
                len(result.published_report_uids),
                len(result.failed_report_uids),
                result.deferred_report_count,
            )

        completed = execute_continuous_update(
            config.DATA_ROOT,
            config.SAVE_DIR,
            embeddings=embeddings,
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
            retry_extraction_failures=retry_extraction_failures,
            batch_size=DEFAULT_CONTINUOUS_REPORT_BATCH_SIZE,
            progress_callback=report_delta,
        )
    except NativeSourceExtractionError as exc:
        logger.error(
            "Native V2 extraction failure escaped per-document recording: %s: %s",
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
    result = completed.candidate_result
    outcome = completed.publication_outcome
    logger.info(
        "Native V2 final compaction complete: generation=%s epoch=%s "
        "reports=%s chunks=%s",
        outcome.publication_generation,
        outcome.write_epoch,
        result.report_count,
        result.chunk_count,
    )
    if getattr(outcome, "cleanup_pending", False):
        logger.warning(
            "Native V2 publication succeeded; artifact cleanup remains "
            "pending and will be retried: %s",
            getattr(outcome, "cleanup_error", None),
        )
    logger.info(
        "Native V2 update complete: deltas=%s compactions=1 generation=%s "
        "epoch=%s reports=%s chunks=%s failed=%s",
        completed_batch_count,
        outcome.publication_generation,
        outcome.write_epoch,
        result.report_count,
        result.chunk_count,
        len(completed.failed_report_uids),
    )
    return 0


def _has_source_pdfs(source_directory: str | os.PathLike) -> bool:
    root = Path(source_directory)
    if not root.is_dir() or root.is_symlink():
        return False
    return any(
        path.is_file() and not path.is_symlink() and path.suffix.lower() == ".pdf"
        for path in root.rglob("*")
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Finance LLM PDF embedding pipeline")
    parser.add_argument(
        "--retry-extraction-failures",
        action="store_true",
        help=(
            "Retry PDFs recorded as source-extraction-failed in the active "
            "native V2 manifest."
        ),
    )
    args = parser.parse_args(argv)
    raise SystemExit(
        run_pipeline(
            retry_extraction_failures=args.retry_extraction_failures,
        )
    )


if __name__ == "__main__":
    main()
