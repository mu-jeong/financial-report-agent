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
DEFAULT_ISSUE_REPORT_INGEST_URL = (
    "https://rjjnhvoontxpimhiabou.supabase.co/functions/v1/"
    "issue-report-ingest"
)
DEFAULT_ISSUE_REPORT_PUBLISHABLE_KEY = (
    "sb_publishable_O5bQ-b9VvY1fcwDz0IQlPg_prsFM87B"
)


def _default_save_dir() -> str:
    return str(BASE_DIR / "data" / "downloaded")


def _default_data_root() -> str:
    return str(BASE_DIR / "data")


def _default_rerank_cache_dir() -> str:
    return str(Path(_default_data_root()) / "cache" / "flashrank")


def _default_conversation_db_path() -> str:
    return str(BASE_DIR / "data" / "conversations.db")


def _default_company_industry_data_path() -> str:
    return ""


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
    env_aliases: tuple[str, ...] = ()
    use_default_when_blank: bool = True

    def default_value(self) -> Any:
        return self.default() if callable(self.default) else self.default


    def example_value(self) -> str:
        value = self.env_example if self.env_example is not None else self.default_value()
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return ""
        return str(value)


@dataclass(frozen=True)
class RetrievalPathSettings:
    """Canonical runtime paths derived from one retrieval authority root."""

    data_root: Path
    rerank_cache_dir: Path


def _configured_path(name: str, environ: dict[str, str]) -> Path | None:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return None
    return Path(raw.strip()).expanduser().resolve(strict=False)


def resolve_retrieval_path_settings(
    environ: dict[str, str] | None = None,
) -> RetrievalPathSettings:
    """Resolve Native V2 runtime storage from ``DATA_ROOT``."""

    env = os.environ if environ is None else environ
    data_root = _configured_path("DATA_ROOT", env)
    if data_root is None:
        data_root = Path(_default_data_root()).resolve(strict=False)

    rerank_cache = _configured_path("RERANK_CACHE_DIR", env)
    return RetrievalPathSettings(
        data_root=data_root,
        rerank_cache_dir=rerank_cache or data_root / "cache" / "flashrank",
    )


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
            "SEARCH_CANDIDATE_MULTIPLIER",
            ConfigSpec(
                name="SEARCH_CANDIDATE_MULTIPLIER",
                default=1,
                parser=as_int,
                description="Multiply SEARCH_TOP_K by this value when fetching candidates.",
                section="Search",
            ),
        ),
        (
            "VECTOR_RETRIEVAL_CONCURRENCY",
            ConfigSpec(
                name="VECTOR_RETRIEVAL_CONCURRENCY",
                default=5,
                parser=as_int,
                description=(
                    "Process-wide safety ceiling for concurrent retrieval branches; "
                    "each comparison uses min(target count, this value)."
                ),
                section="Search",
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
            "PDF_EXTRACTION_ENGINE",
            ConfigSpec(
                name="PDF_EXTRACTION_ENGINE",
                default="pymupdf",
                parser=as_lower_str,
                description=(
                    "PDF parsing/extraction engine for normal embedding runs: "
                    "pymupdf, opendataloader, docling, or pdf-to-markdown. "
                    "Legacy env alias: EXTRACTION_ENGINE."
                ),
                section="Embedding",
                env_aliases=("EXTRACTION_ENGINE",),
            ),
        ),
        (
            "PDF_EXTRACTION_FALLBACK_ENGINE",
            ConfigSpec(
                name="PDF_EXTRACTION_FALLBACK_ENGINE",
                default="",
                parser=as_lower_str,
                description=(
                    "Fallback PDF parsing/extraction engine used only when "
                    "PDF_EXTRACTION_ENGINE fails. Set empty to disable fallback. "
                    "Legacy env alias: EXTRACTION_FALLBACK_ENGINE."
                ),
                section="Embedding",
                env_example="opendataloader",
                env_aliases=("EXTRACTION_FALLBACK_ENGINE",),
                use_default_when_blank=False,
            ),
        ),
        (
            "UNEMBEDDED_PDF_EXTRACTION_ENGINE",
            ConfigSpec(
                name="UNEMBEDDED_PDF_EXTRACTION_ENGINE",
                default="",
                parser=as_lower_str,
                env_example="pymupdf",
                description=(
                    "PDF parsing/extraction engine used when embedding pending/unembedded reports. "
                    "Set empty to reuse PDF_EXTRACTION_ENGINE. Legacy env alias: UNEMBEDDED_EXTRACTION_ENGINE."
                ),
                section="Embedding",
                env_aliases=("UNEMBEDDED_EXTRACTION_ENGINE",),
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
                description=(
                    "Optional override for the directory used by the GUI to open "
                    "referenced PDF files. Leave blank to use <PROJECT_ROOT>/data/downloaded."
                ),
                section="Optional path overrides (leave blank to use defaults)",
                env_example="",
            ),
        ),
        (
            "SAVE_DIR",
            ConfigSpec(
                name="SAVE_DIR",
                default=_default_save_dir,
                parser=as_str,
                description=(
                    "Optional override for downloaded report PDFs and the V2 source "
                    "inventory. Leave blank to use <PROJECT_ROOT>/data/downloaded."
                ),
                section="Optional path overrides (leave blank to use defaults)",
                env_example="",
            ),
        ),
        (
            "DATA_ROOT",
            ConfigSpec(
                name="DATA_ROOT",
                default=_default_data_root,
                parser=as_str,
                description=(
                    "Optional override for the canonical Native V2 retrieval root. "
                    "Leave blank to use <PROJECT_ROOT>/data; normal runtime derives "
                    "its catalog and snapshot paths from this directory."
                ),
                section="Optional path overrides (leave blank to use defaults)",
                env_example="",
            ),
        ),
        (
            "RERANK_CACHE_DIR",
            ConfigSpec(
                name="RERANK_CACHE_DIR",
                default=_default_rerank_cache_dir,
                parser=as_str,
                description=(
                    "Optional override for the FlashRank model cache. Leave blank to use "
                    "<DATA_ROOT>/cache/flashrank."
                ),
                section="Optional path overrides (leave blank to use defaults)",
                env_example="",
            ),
        ),
        (
            "CONVERSATION_DB_PATH",
            ConfigSpec(
                name="CONVERSATION_DB_PATH",
                default=_default_conversation_db_path,
                parser=as_str,
                description=(
                    "Optional override for the SQLite conversation database path. "
                    "Leave blank to use <PROJECT_ROOT>/data/conversations.db."
                ),
                section="Optional path overrides (leave blank to use defaults)",
                env_example="",
            ),
        ),
        (
            "COMPANY_INDUSTRY_DATA_PATH",
            ConfigSpec(
                name="COMPANY_INDUSTRY_DATA_PATH",
                default=_default_company_industry_data_path,
                parser=as_optional_str,
                description=(
                    "Optional override for the KRX listed-company industry CSV used "
                    "for sector/company universe lookup. Leave blank to use "
                    "<PROJECT_ROOT>/data/listed_company_industries.csv."
                ),
                section="Optional path overrides (leave blank to use defaults)",
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
        (
            "ISSUE_REPORT_REMOTE_ENABLED",
            ConfigSpec(
                name="ISSUE_REPORT_REMOTE_ENABLED",
                default=True,
                parser=as_bool,
                description=(
                    "Allow user-approved issue reports to be queued for the "
                    "operator-managed remote ingest endpoint."
                ),
                section="Issue reporting",
            ),
        ),
        (
            "ISSUE_REPORT_INGEST_URL",
            ConfigSpec(
                name="ISSUE_REPORT_INGEST_URL",
                default=DEFAULT_ISSUE_REPORT_INGEST_URL,
                parser=as_optional_str,
                description=(
                    "Public Supabase Edge Function URL for central issue-report "
                    "collection. Override only when using another deployment; "
                    "set ISSUE_REPORT_REMOTE_ENABLED=false to disable submission."
                ),
                section="Issue reporting",
            ),
        ),
        (
            "ISSUE_REPORT_PUBLISHABLE_KEY",
            ConfigSpec(
                name="ISSUE_REPORT_PUBLISHABLE_KEY",
                default=DEFAULT_ISSUE_REPORT_PUBLISHABLE_KEY,
                parser=as_optional_str,
                description=(
                    "Public Supabase publishable key accepted by the issue-report "
                    "Edge Function. This is not a secret; override it only when "
                    "using another deployment."
                ),
                section="Issue reporting",
            ),
        ),
        (
            "ISSUE_REPORT_OUTBOX_DIR",
            ConfigSpec(
                name="ISSUE_REPORT_OUTBOX_DIR",
                default=None,
                parser=as_optional_str,
                description=(
                    "Optional retry-only issue-report outbox path. Leave blank "
                    "to use <DATA_ROOT>/issue-report-outbox. Report payloads are "
                    "removed after delivery, rejection, or expiry."
                ),
                section="Issue reporting",
                env_example="",
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
    if raw is None:
        for alias in spec.env_aliases:
            raw = env.get(alias)
            if raw is not None:
                break
    if raw is None or (raw.strip() == "" and spec.use_default_when_blank):
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
DATA_ROOT_DEFAULT = _default_data_root
RERANK_CACHE_DIR_DEFAULT = _default_rerank_cache_dir
CONVERSATION_DB_PATH_DEFAULT = _default_conversation_db_path
LOG_FILE_DEFAULT = _default_log_file
