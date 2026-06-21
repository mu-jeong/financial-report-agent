"""Central configuration specifications for Finance LLM.

This module is the single source of truth for configurable parameters: names,
defaults, types, and .env.example rendering. It intentionally uses only the
Python standard library so Quick Start can import it before dependencies are
installed.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable
import os

BASE_DIR = Path(__file__).resolve().parent.parent.parent
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


def _default_save_dir() -> str:
    return str(BASE_DIR / "data" / "downloaded")


def _default_db_path() -> str:
    return str(BASE_DIR / "data" / "reports.db")


def _default_faiss_dir() -> str:
    return str(BASE_DIR / "data" / "vector_db")


def _default_conversation_db_path() -> str:
    return str(BASE_DIR / "data" / "conversations.db")


def _default_log_file() -> str:
    return str(BASE_DIR / "logs" / "finance_llm.log")


def _today() -> str:
    return date.today().isoformat()


DefaultValue = Any | Callable[[], Any]
Parser = Callable[[str], Any]


@dataclass(frozen=True)
class ConfigSpec:
    name: str
    default: DefaultValue
    parser: Parser
    description: str
    section: str
    env_example: str | None = None
    include_in_env_example: bool = True

    def default_value(self) -> Any:
        return self.default() if callable(self.default) else self.default

    def example_value(self) -> str:
        value = self.env_example if self.env_example is not None else self.default_value()
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return ""
        return str(value)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def as_optional_str(value: str) -> str | None:
    value = value.strip()
    return value or None


def as_str(value: str) -> str:
    return value.strip()


def as_lower_str(value: str) -> str:
    return value.strip().lower()


def as_int(value: str) -> int:
    return int(value.strip())


def as_float(value: str) -> float:
    return float(value.strip())


def as_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ValueError(f"Expected boolean value, got {value!r}")


CONFIG_SPECS: "OrderedDict[str, ConfigSpec]" = OrderedDict(
    (
        (
            "OPENROUTER_API_KEY",
            ConfigSpec(
                name="OPENROUTER_API_KEY",
                default=None,
                parser=as_optional_str,
                description="OpenRouter API key. Required for model and embedding calls.",
                section="OpenRouter",
                env_example="your_openrouter_api_key_here",
            ),
        ),
        (
            "OPENROUTER_APP_URL",
            ConfigSpec(
                name="OPENROUTER_APP_URL",
                default="",
                parser=as_str,
                description="Optional app/site URL sent to OpenRouter rankings metadata.",
                section="OpenRouter",
            ),
        ),
        (
            "OPENROUTER_APP_TITLE",
            ConfigSpec(
                name="OPENROUTER_APP_TITLE",
                default="finance_llm",
                parser=as_str,
                description="Optional app title sent to OpenRouter rankings metadata.",
                section="OpenRouter",
            ),
        ),
        (
            "OPENROUTER_DATA_COLLECTION",
            ConfigSpec(
                name="OPENROUTER_DATA_COLLECTION",
                default="deny",
                parser=as_lower_str,
                description="deny routes only to providers that do not collect user data; allow permits broader routing.",
                section="OpenRouter",
            ),
        ),
        (
            "GENERATION_MODEL",
            ConfigSpec(
                name="GENERATION_MODEL",
                default="deepseek/deepseek-v4-flash",
                parser=as_str,
                description="Model used for answer generation, query rewriting, and SQL generation.",
                section="Models",
            ),
        ),
        (
            "EMBEDDING_MODEL",
            ConfigSpec(
                name="EMBEDDING_MODEL",
                default="baai/bge-m3",
                parser=as_str,
                description="Embedding model used for FAISS indexing and search queries.",
                section="Models",
            ),
        ),
        (
            "USE_RERANKER",
            ConfigSpec(
                name="USE_RERANKER",
                default=False,
                parser=as_bool,
                description="Enable optional reranking. false is cheaper for Quick Start.",
                section="Rerank",
            ),
        ),
        (
            "RERANK_PROVIDER",
            ConfigSpec(
                name="RERANK_PROVIDER",
                default="openrouter",
                parser=as_lower_str,
                description="Rerank provider: openrouter or explicit local flashrank adapter.",
                section="Rerank",
            ),
        ),
        (
            "RERANK_MODEL",
            ConfigSpec(
                name="RERANK_MODEL",
                default="cohere/rerank-v3.5",
                parser=as_str,
                description="OpenRouter rerank model when reranking is enabled.",
                section="Rerank",
            ),
        ),
        (
            "RERANK_TIMEOUT",
            ConfigSpec(
                name="RERANK_TIMEOUT",
                default=60.0,
                parser=as_float,
                description="Rerank request timeout in seconds.",
                section="Rerank",
            ),
        ),
        (
            "RERANK_CANDIDATE_MULTIPLIER",
            ConfigSpec(
                name="RERANK_CANDIDATE_MULTIPLIER",
                default=3,
                parser=as_int,
                description="Fetch SEARCH_TOP_K times this many candidates before reranking.",
                section="Rerank",
            ),
        ),
        (
            "RECENCY_WEIGHT",
            ConfigSpec(
                name="RECENCY_WEIGHT",
                default=0.15,
                parser=as_float,
                description="Search score boost for newer reports.",
                section="Search",
            ),
        ),
        (
            "SEARCH_TOP_K",
            ConfigSpec(
                name="SEARCH_TOP_K",
                default=20,
                parser=as_int,
                description="Number of vector search results returned to the answer pipeline.",
                section="Search",
            ),
        ),
        (
            "TEST_LIMIT",
            ConfigSpec(
                name="TEST_LIMIT",
                default=10,
                parser=as_int,
                description="Default embedding run limit. 0 processes every pending file.",
                section="Embedding",
            ),
        ),
        (
            "PARENT_CHUNK_SIZE",
            ConfigSpec(
                name="PARENT_CHUNK_SIZE",
                default=2000,
                parser=as_int,
                description="Parent chunk size for parent-child retrieval.",
                section="Embedding",
            ),
        ),
        (
            "CHILD_CHUNK_SIZE",
            ConfigSpec(
                name="CHILD_CHUNK_SIZE",
                default=500,
                parser=as_int,
                description="Child chunk size used for retrieval.",
                section="Embedding",
            ),
        ),
        (
            "CHUNK_SIZE",
            ConfigSpec(
                name="CHUNK_SIZE",
                default=1500,
                parser=as_int,
                description="Fallback/general text splitter chunk size.",
                section="Embedding",
            ),
        ),
        (
            "CHUNK_OVERLAP",
            ConfigSpec(
                name="CHUNK_OVERLAP",
                default=150,
                parser=as_int,
                description="Fallback/general text splitter overlap size.",
                section="Embedding",
            ),
        ),
        (
            "EXTRACTION_ENGINE",
            ConfigSpec(
                name="EXTRACTION_ENGINE",
                default="pymupdf",
                parser=as_lower_str,
                description="PDF extraction engine: pymupdf, marker, opendataloader, docling, or pdf-to-markdown.",
                section="Embedding",
            ),
        ),
        (
            "USE_PARENT_CHILD",
            ConfigSpec(
                name="USE_PARENT_CHILD",
                default=True,
                parser=as_bool,
                description="Enable parent-child chunking.",
                section="Embedding",
            ),
        ),
        (
            "CRAWLER_MODE",
            ConfigSpec(
                name="CRAWLER_MODE",
                default="LATEST",
                parser=as_str,
                description="LATEST uses the crawler's KST current date; SPECIFIC_DATE uses CRAWLER_TARGET_DATE.",
                section="Crawler",
            ),
        ),
        (
            "CRAWLER_CATEGORIES",
            ConfigSpec(
                name="CRAWLER_CATEGORIES",
                default="company",
                parser=as_str,
                description="Report categories: company, industry, economy, comma-separated list, or all.",
                section="Crawler",
            ),
        ),
        (
            "CRAWLER_TARGET_DATE",
            ConfigSpec(
                name="CRAWLER_TARGET_DATE",
                default=_today,
                parser=as_str,
                description="Search end date. Quick Start refreshes this to the execution date every run.",
                section="Crawler",
                env_example="",
            ),
        ),
        (
            "CRAWLER_TARGET_COUNT",
            ConfigSpec(
                name="CRAWLER_TARGET_COUNT",
                default=0,
                parser=as_int,
                description="0 means no count limit; positive values stop after that many processed reports.",
                section="Crawler",
            ),
        ),
        (
            "CRAWLER_LOOKBACK_DAYS",
            ConfigSpec(
                name="CRAWLER_LOOKBACK_DAYS",
                default=7,
                parser=as_int,
                description="Collect target date plus this many previous days. 7 means an 8-day inclusive window.",
                section="Crawler",
            ),
        ),
        (
            "CRAWLER_MAX_LOOKBACK_DAYS",
            ConfigSpec(
                name="CRAWLER_MAX_LOOKBACK_DAYS",
                default=7,
                parser=as_int,
                description="Safety lookback window for count-based collection.",
                section="Crawler",
            ),
        ),
        (
            "REPORT_PDF_DIR",
            ConfigSpec(
                name="REPORT_PDF_DIR",
                default=_default_save_dir,
                parser=as_str,
                description="Directory used by the GUI to open referenced PDF files. Leave blank to use data/downloaded.",
                section="Paths",
                env_example="",
            ),
        ),
        (
            "MONITORING_MODE",
            ConfigSpec(
                name="MONITORING_MODE",
                default=False,
                parser=as_bool,
                description="Enable the Streamlit Monitoring Mode UI for performance metric review.",
                section="Monitoring",
            ),
        ),
    )
)


def get_config_value(name: str, environ: dict[str, str] | None = None) -> Any:
    """Return a typed config value from environment or its spec default."""
    if name not in CONFIG_SPECS:
        raise KeyError(f"Unknown config key: {name}")

    spec = CONFIG_SPECS[name]
    env = os.environ if environ is None else environ
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return spec.default_value()
    return spec.parser(raw)


def quickstart_env_updates(run_date: date | None = None) -> dict[str, str]:
    """Environment overrides applied by one-click Quick Start runs."""
    target_date = (run_date or date.today()).isoformat()
    return {
        "CRAWLER_MODE": "LATEST",
        "CRAWLER_TARGET_DATE": target_date,
        "CRAWLER_LOOKBACK_DAYS": str(get_config_value("CRAWLER_LOOKBACK_DAYS")),
        "CRAWLER_TARGET_COUNT": str(get_config_value("CRAWLER_TARGET_COUNT")),
        "CRAWLER_MAX_LOOKBACK_DAYS": str(get_config_value("CRAWLER_MAX_LOOKBACK_DAYS")),
    }


def iter_env_example_sections() -> list[tuple[str, list[ConfigSpec]]]:
    sections: "OrderedDict[str, list[ConfigSpec]]" = OrderedDict()
    for spec in CONFIG_SPECS.values():
        if not spec.include_in_env_example:
            continue
        sections.setdefault(spec.section, []).append(spec)
    return list(sections.items())


def render_env_example() -> str:
    """Render .env.example from CONFIG_SPECS."""
    lines: list[str] = [
        "# Finance LLM environment template",
        "# Generated from src/configs/settings.py. Do not edit defaults in multiple places.",
        "# Copy this file to .env, then fill OPENROUTER_API_KEY.",
        "",
    ]

    for section, specs in iter_env_example_sections():
        lines.append(f"# --- {section} ---")
        for spec in specs:
            lines.append(f"# {spec.description}")
            lines.append(f"{spec.name}={spec.example_value()}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# Non-env path constants are kept here too so path defaults live in one module.
SAVE_DIR_DEFAULT = _default_save_dir
DB_PATH_DEFAULT = _default_db_path
FAISS_DIR_DEFAULT = _default_faiss_dir
CONVERSATION_DB_PATH_DEFAULT = _default_conversation_db_path
LOG_FILE_DEFAULT = _default_log_file
