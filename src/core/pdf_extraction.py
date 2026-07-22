from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import fitz

from src.configs import config
from src.utils.text_filters import is_noise_line, is_sidebar_block, strip_compliance

logger = config.get_logger(__name__)

SUPPORTED_EXTRACTION_ENGINES = {
    "pymupdf",
    "marker",
    "opendataloader",
    "docling",
    "pdf-to-markdown",
}
ENGINE_ALIASES = {
    "datalab-marker": "marker",
    "marker-pdf": "marker",
    "pspdfkit": "pdf-to-markdown",
    "nutrient": "pdf-to-markdown",
    "nutrient-pdf-to-markdown": "pdf-to-markdown",
}

_MARKER_MODELS = None
_MARKER_TABLE_PROCESSORS = {
    "TableProcessor",
    "LLMTableProcessor",
    "LLMTableMergeProcessor",
}
_PLAIN_TABLE_HEADING_RE = re.compile(
    r"(?i)\b("
    r"consensus\s*data|financial\s*data|financial\s*summary|earnings\s*forecast|"
    r"stock\s*data|company\s*data|balance\s*sheet|income\s*statement|cash\s*flow"
    r")\b|"
    r"(컨센서스|재무\s*데이터|실적\s*전망|주가\s*정보|주요\s*주주|최대\s*주주|"
    r"재무\s*상태표|손익\s*계산서|현금\s*흐름표)",
)
_PLAIN_TABLE_LABEL_RE = re.compile(
    r"(?i)\b(EPS|BPS|PER|PBR|ROE|ROA|DPS|EBITDA|EV/EBITDA)\b|"
    r"(매출액|영업이익|순이익|지배주주|자산총계|부채총계|자본총계|"
    r"국민연금|국민연금공단|외국인\s*지분율|발행주식수|주요주주|최대주주)",
)
_NUMBER_TOKEN_RE = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?%?$|^[+-]?\d+(?:\.\d+)?%?$")


@dataclass(frozen=True)
class ExtractionResult:
    requested_engine: str
    used_engine: str
    text: str


def get_marker_models():
    """Load Marker models lazily so pymupdf/opendataloader paths stay light."""
    global _MARKER_MODELS
    if _MARKER_MODELS is None:
        try:
            from marker.models import create_model_dict

            logger.info(
                "  [Extraction] Loading Marker models "
                "(first run may download several GB)..."
            )
            _MARKER_MODELS = create_model_dict()
        except Exception as exc:
            logger.error("  Marker model load failed: %s", exc)
            raise
    return _MARKER_MODELS


def extract_pdf_text(
    pdf_path: str | Path,
    engine: str,
    *,
    clean: bool = True,
    allow_fallback: bool = True,
    fallback_engine: str | None = None,
) -> ExtractionResult:
    """
    Extract a PDF into the text contract consumed by the embedding pipeline.

    The returned text is cleaned by default with the same finance-report filters
    used by the production embedding flow. Set clean=False for raw extractor
    comparisons.
    """
    requested_engine = normalize_engine(engine)
    path = Path(pdf_path)

    try:
        text = _extract_pdf_text(path, requested_engine)
        text = drop_markdown_tables(text)
        if clean:
            text = clean_extracted_text(text)
        _raise_if_empty(text, requested_engine)
        used_engine = requested_engine
    except Exception as exc:
        logger.warning("  %s extraction failed: %s", requested_engine, exc)
        configured_fallback = (
            fallback_engine
            if fallback_engine is not None
            else "pymupdf"
        )
        configured_fallback = str(configured_fallback or "").strip()
        if not allow_fallback or not configured_fallback:
            raise

        normalized_fallback = normalize_engine(configured_fallback)
        if normalized_fallback == requested_engine:
            raise

        logger.warning("  Falling back to %s extraction.", normalized_fallback)
        try:
            text = _extract_pdf_text(path, normalized_fallback)
            text = drop_markdown_tables(text)
            if clean:
                text = clean_extracted_text(text)
            used_engine = f"{normalized_fallback}-fallback"
            _raise_if_empty(text, used_engine)
        except Exception as fallback_exc:
            raise RuntimeError(
                f"{requested_engine} extraction failed ({exc}); "
                f"fallback {normalized_fallback} extraction failed ({fallback_exc})"
            ) from fallback_exc

    return ExtractionResult(
        requested_engine=requested_engine,
        used_engine=used_engine,
        text=text,
    )


def normalize_engine(engine: str) -> str:
    normalized = ENGINE_ALIASES.get(engine.strip().lower(), engine.strip().lower())
    if normalized not in SUPPORTED_EXTRACTION_ENGINES:
        aliases = ", ".join(f"{alias}->{target}" for alias, target in sorted(ENGINE_ALIASES.items()))
        choices = ", ".join(sorted(SUPPORTED_EXTRACTION_ENGINES))
        if aliases:
            choices = f"{choices} (aliases: {aliases})"
        raise ValueError(f"Unsupported extraction engine: {engine}. Use one of: {choices}")
    return normalized


def _raise_if_empty(text: str, engine: str) -> None:
    if not text.strip():
        raise ValueError(f"{engine} extracted empty text")


def clean_extracted_text(text: str) -> str:
    text = drop_markdown_tables(text)
    text = strip_compliance(text)

    blocks = text.split("\n\n")
    clean_blocks: list[str] = []

    for block in blocks:
        if is_sidebar_block(block):
            continue

        lines = block.split("\n")
        filtered_lines = [line for line in lines if not is_noise_line(line)]
        clean_block = "\n".join(filtered_lines).strip()
        if clean_block:
            clean_blocks.append(clean_block)

    return "\n\n".join(clean_blocks)


def drop_markdown_tables(text: str) -> str:
    """Remove table-only structures from PDF extraction output.

    OpenRouter is used after parsing for embeddings/reranking/generation rather
    than as a local PDF extraction engine. To keep table data out of the
    OpenRouter-backed indexing path, remove markdown, HTML, and plain-text
    table blocks from all extractor outputs before downstream cleaning and
    chunking. This also covers parser integrations whose public options do not
    provide a table extraction "off" switch.
    """
    without_html_tables = re.sub(
        r"(?is)<table\b[^>]*>.*?</table>",
        "\n\n",
        str(text or ""),
    )

    kept_lines: list[str] = []
    lines = without_html_tables.splitlines()
    index = 0
    while index < len(lines):
        if _is_markdown_table_line(lines[index]):
            start = index
            while index < len(lines) and _is_markdown_table_line(lines[index]):
                index += 1
            table_block = lines[start:index]
            if len(table_block) >= 2 or any(_is_markdown_table_separator(line) for line in table_block):
                continue
            kept_lines.extend(table_block)
            continue

        kept_lines.append(lines[index])
        index += 1

    without_markdown_tables = re.sub(r"\n{3,}", "\n\n", "\n".join(kept_lines)).strip()
    return _drop_plain_text_table_blocks(without_markdown_tables)


def _is_markdown_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def _is_markdown_table_separator(line: str) -> bool:
    return bool(
        re.match(
            r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$",
            line,
        )
    )


def _drop_plain_text_table_blocks(text: str) -> str:
    """Remove whitespace-flattened table blocks left by PDF extractors."""
    blocks = re.split(r"\n\s*\n", str(text or ""))
    kept_blocks: list[str] = []
    index = 0
    while index < len(blocks):
        block = blocks[index].strip()
        if not block:
            index += 1
            continue

        if _is_plain_text_table_block(block):
            index += 1
            continue

        # Some extractors emit a table heading as one paragraph and the table
        # body as the next paragraph. Drop both as one table section.
        if (
            _is_plain_text_table_heading(block)
            and index + 1 < len(blocks)
            and _is_plain_text_table_block(blocks[index + 1])
        ):
            index += 2
            continue

        kept_blocks.append(block)
        index += 1

    return "\n\n".join(kept_blocks).strip()


def _is_plain_text_table_block(block: str) -> bool:
    lines = [line.strip() for line in str(block or "").splitlines() if line.strip()]
    if not lines:
        return False

    normalized = _strip_markdown_heading(" ".join(lines))
    numeric_count = _numeric_token_count(normalized)
    label_count = len(_PLAIN_TABLE_LABEL_RE.findall(normalized))
    table_like_lines = sum(1 for line in lines if _is_plain_text_table_line(line))
    has_heading = _is_plain_text_table_heading(normalized)

    if has_heading:
        return True

    if len(lines) == 1:
        return numeric_count >= 2 and (
            label_count >= 1
            or _contains_year_pair(normalized)
            or _looks_like_shareholder_row(normalized)
        )

    return (
        numeric_count >= 3
        and label_count >= 1
        and table_like_lines / len(lines) >= 0.5
    )


def _is_plain_text_table_line(line: str) -> bool:
    normalized = _strip_markdown_heading(line)
    tokens = normalized.split()
    if len(tokens) < 3:
        return False
    numeric_count = sum(1 for token in tokens if _NUMBER_TOKEN_RE.match(token.strip("()[],")))
    if numeric_count < 2:
        return False
    if _PLAIN_TABLE_LABEL_RE.search(normalized):
        return True
    return numeric_count / len(tokens) >= 0.4 and len(tokens) >= 5


def _is_plain_text_table_heading(text: str) -> bool:
    normalized = _strip_markdown_heading(text).strip()
    return bool(_PLAIN_TABLE_HEADING_RE.search(normalized)) and len(normalized) <= 120


def _numeric_token_count(text: str) -> int:
    return sum(
        1
        for token in str(text or "").split()
        if _NUMBER_TOKEN_RE.match(token.strip("()[],"))
    )


def _contains_year_pair(text: str) -> bool:
    return len(re.findall(r"\b20\d{2}\b", str(text or ""))) >= 2


def _looks_like_shareholder_row(text: str) -> bool:
    normalized = str(text or "")
    return (
        bool(re.search(r"(외\s*\d+\s*인|국민연금|국민연금공단|최대주주|주요주주)", normalized))
        and _numeric_token_count(normalized) >= 2
    )


def _strip_markdown_heading(text: str) -> str:
    return re.sub(r"^\s*#{1,6}\s*", "", str(text or "")).strip()


def text_from_opendataloader_json_without_tables(payload: str | dict | list) -> str:
    """Convert OpenDataLoader JSON output to text while skipping table nodes.

    Official OpenDataLoader JSON output labels layout elements with a ``type``
    field and represents detected tables as ``table`` nodes containing
    ``table row``/cell children. Filtering those nodes is more reliable than
    trying to infer tables after they have already been flattened to Markdown.
    """
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return drop_markdown_tables(payload)
    else:
        parsed = payload

    table_ids = _collect_opendataloader_table_ids(parsed)
    parts = _collect_opendataloader_text(parsed, table_ids=table_ids, inside_table=False)
    return "\n\n".join(part for part in parts if part.strip()).strip()


def _collect_opendataloader_table_ids(node: object) -> set[int]:
    table_ids: set[int] = set()
    if isinstance(node, dict):
        if str(node.get("type", "")).casefold() == "table" and isinstance(node.get("id"), int):
            table_ids.add(node["id"])
        for value in node.values():
            table_ids.update(_collect_opendataloader_table_ids(value))
    elif isinstance(node, list):
        for item in node:
            table_ids.update(_collect_opendataloader_table_ids(item))
    return table_ids


def _collect_opendataloader_text(
    node: object,
    *,
    table_ids: set[int],
    inside_table: bool,
) -> list[str]:
    if isinstance(node, list):
        parts: list[str] = []
        for item in node:
            parts.extend(
                _collect_opendataloader_text(
                    item,
                    table_ids=table_ids,
                    inside_table=inside_table,
                )
            )
        return parts

    if not isinstance(node, dict):
        return []

    node_type = str(node.get("type", "")).casefold()
    if node_type in {"table", "table row", "table cell"}:
        return []

    linked_content_id = node.get("linked content id")
    if node_type == "caption" and isinstance(linked_content_id, int) and linked_content_id in table_ids:
        return []

    content = str(node.get("content") or "").strip()
    parts = [content] if content and not inside_table else []

    for child_key in ("kids", "list items", "rows", "cells"):
        child = node.get(child_key)
        if child is None:
            continue
        parts.extend(
            _collect_opendataloader_text(
                child,
                table_ids=table_ids,
                inside_table=inside_table,
            )
        )
    return parts


def _extract_pdf_text(pdf_path: Path, engine: str) -> str:
    if engine == "marker":
        return _extract_marker_markdown(pdf_path)
    if engine == "opendataloader":
        return _extract_opendataloader_markdown(pdf_path)
    if engine == "docling":
        return _extract_docling_markdown(pdf_path)
    if engine == "pdf-to-markdown":
        return _extract_pspdfkit_pdf_to_markdown(pdf_path)
    return _extract_pymupdf_text(pdf_path)


def _extract_marker_markdown(pdf_path: Path) -> str:
    from marker.config.parser import ConfigParser
    from marker.converters.pdf import PdfConverter

    config_dict = {
        "output_format": "markdown",
        "use_llm": False,
        "force_ocr": False,
    }
    config_parser = ConfigParser(config_dict)
    processor_list = (
        config_parser.get_processors()
        or _marker_processor_list_without_table_processors(PdfConverter)
    )

    converter = PdfConverter(
        config=config_parser.generate_config_dict(),
        artifact_dict=get_marker_models(),
        processor_list=processor_list,
        renderer=config_parser.get_renderer(),
        llm_service=config_parser.get_llm_service(),
    )

    rendered = converter(str(pdf_path))
    return rendered.markdown


def _marker_processor_list_without_table_processors(pdf_converter_cls) -> list[str]:
    """Use Marker's processor override hook while omitting table processors."""
    return [
        f"{processor.__module__}.{processor.__name__}"
        for processor in getattr(pdf_converter_cls, "default_processors", ())
        if processor.__name__ not in _MARKER_TABLE_PROCESSORS
    ]


def _extract_docling_markdown(pdf_path: Path) -> str:
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter
        from docling.document_converter import PdfFormatOption
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "docling is not installed. Install the optional parser with `pip install docling`."
        ) from exc

    # Docling's PDF pipeline has a first-class table-structure option. Keep it
    # disabled so this engine does not spend work reconstructing tables before
    # the shared cross-engine table scrubber removes table-shaped output.
    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_table_structure = False
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
        }
    )
    result = converter.convert(str(pdf_path))
    return result.document.export_to_markdown()


def _extract_pspdfkit_pdf_to_markdown(pdf_path: Path) -> str:
    executable = shutil.which("pdf-to-markdown")
    if not executable:
        raise RuntimeError(
            "pdf-to-markdown CLI is not installed. Install it with "
            "`npm install -g @pspdfkit/pdf-to-markdown` or make `pdf-to-markdown` available on PATH."
        )

    completed = subprocess.run(
        [executable, str(pdf_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        message = stderr or stdout or f"exit code {completed.returncode}"
        raise RuntimeError(f"pdf-to-markdown failed: {message}")
    return completed.stdout


def _extract_opendataloader_markdown(pdf_path: Path) -> str:
    _ensure_java_on_path()

    from langchain_opendataloader_pdf import OpenDataLoaderPDFLoader

    loader = OpenDataLoaderPDFLoader(
        file_path=str(pdf_path),
        format="json",
        split_pages=False,
        quiet=True,
        table_method="default",
        reading_order="xycut",
        image_output="off",
    )
    documents = loader.load()
    return "\n\n".join(
        text_from_opendataloader_json_without_tables(doc.page_content)
        for doc in documents
    )


def _ensure_java_on_path() -> None:
    if shutil.which("java"):
        return

    candidates = [
        os.environ.get("JAVA_HOME"),
        os.environ.get("JDK_HOME"),
        os.environ.get("JRE_HOME"),
        _get_windows_env("JAVA_HOME", "Machine"),
        _get_windows_env("JAVA_HOME", "User"),
    ]

    for candidate in candidates:
        if not candidate:
            continue

        java_bin = Path(candidate) / "bin"
        java_exe = java_bin / "java.exe"
        if java_exe.exists():
            os.environ["JAVA_HOME"] = str(Path(candidate))
            os.environ["PATH"] = f"{java_bin}{os.pathsep}{os.environ.get('PATH', '')}"
            return


def _get_windows_env(name: str, target: str) -> str | None:
    if os.name != "nt":
        return None

    try:
        import winreg

        root = winreg.HKEY_LOCAL_MACHINE if target == "Machine" else winreg.HKEY_CURRENT_USER
        key_path = (
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
            if target == "Machine"
            else "Environment"
        )
        with winreg.OpenKey(root, key_path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except OSError:
        return None


def _extract_pymupdf_text(pdf_path: Path) -> str:
    doc = fitz.open(str(pdf_path))
    try:
        page_texts: list[str] = []
        for page in doc:
            table_bboxes = _find_pymupdf_table_bboxes(page)
            if not table_bboxes:
                page_texts.append(page.get_text("text"))
                continue

            non_table_blocks = []
            for block in page.get_text("blocks", sort=True):
                block_text = str(block[4] or "").strip()
                if not block_text:
                    continue
                block_bbox = fitz.Rect(block[:4])
                if _intersects_any(block_bbox, table_bboxes):
                    continue
                non_table_blocks.append(block_text)
            page_texts.append("\n\n".join(non_table_blocks))

        return "\n\n".join(page_texts)
    finally:
        doc.close()


def _find_pymupdf_table_bboxes(page) -> list[fitz.Rect]:
    """Detect PyMuPDF table regions so normal text extraction can skip them."""
    table_bboxes: list[fitz.Rect] = []
    seen: set[tuple[float, float, float, float]] = set()

    # PyMuPDF supports both line-based and text-position based table detection.
    # Run both so borderless financial tables are also excluded when possible.
    for kwargs in ({}, {"strategy": "text"}):
        try:
            finder = page.find_tables(**kwargs)
        except Exception as exc:
            logger.debug("  PyMuPDF table detection failed with %s: %s", kwargs, exc)
            continue

        for table in getattr(finder, "tables", []) or []:
            bbox = getattr(table, "bbox", None)
            if bbox is None:
                continue

            rect = fitz.Rect(bbox)
            key = tuple(round(value, 2) for value in rect)
            if key in seen:
                continue
            seen.add(key)
            table_bboxes.append(rect)

    return table_bboxes


def _intersects_any(rect: fitz.Rect, bboxes: list[fitz.Rect]) -> bool:
    return any(rect.intersects(bbox) for bbox in bboxes)
