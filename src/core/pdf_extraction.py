from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil

import fitz

from src.configs import config
from src.utils.text_filters import is_noise_line, is_sidebar_block, strip_compliance

logger = config.get_logger(__name__)

SUPPORTED_EXTRACTION_ENGINES = {"pymupdf", "marker", "opendataloader"}

_MARKER_MODELS = None


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
        if clean:
            text = clean_extracted_text(text)
        _raise_if_empty(text, requested_engine)
        used_engine = requested_engine
    except Exception as exc:
        logger.warning("  %s extraction failed: %s", requested_engine, exc)
        if not allow_fallback or requested_engine == "pymupdf":
            raise

        logger.warning("  Falling back to pymupdf extraction.")
        text = _extract_pymupdf_text(path)
        if clean:
            text = clean_extracted_text(text)
        _raise_if_empty(text, "pymupdf-fallback")
        used_engine = "pymupdf-fallback"

    return ExtractionResult(
        requested_engine=requested_engine,
        used_engine=used_engine,
        text=text,
    )


def normalize_engine(engine: str) -> str:
    normalized = engine.strip().lower()
    if normalized not in SUPPORTED_EXTRACTION_ENGINES:
        choices = ", ".join(sorted(SUPPORTED_EXTRACTION_ENGINES))
        raise ValueError(f"Unsupported extraction engine: {engine}. Use one of: {choices}")
    return normalized


def _raise_if_empty(text: str, engine: str) -> None:
    if not text.strip():
        raise ValueError(f"{engine} extracted empty text")


def clean_extracted_text(text: str) -> str:
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


def _extract_pdf_text(pdf_path: Path, engine: str) -> str:
    if engine == "marker":
        return _extract_marker_markdown(pdf_path)
    if engine == "opendataloader":
        return _extract_opendataloader_markdown(pdf_path)
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

    converter = PdfConverter(
        config=config_parser.generate_config_dict(),
        artifact_dict=get_marker_models(),
        processor_list=config_parser.get_processors(),
        renderer=config_parser.get_renderer(),
        llm_service=config_parser.get_llm_service(),
    )

    rendered = converter(str(pdf_path))
    return rendered.markdown


def _extract_opendataloader_markdown(pdf_path: Path) -> str:
    _ensure_java_on_path()

    from langchain_opendataloader_pdf import OpenDataLoaderPDFLoader

    loader = OpenDataLoaderPDFLoader(
        file_path=str(pdf_path),
        format="markdown",
        split_pages=False,
        quiet=True,
        table_method="default",
        reading_order="xycut",
        image_output="off",
    )
    documents = loader.load()
    return "\n\n".join(doc.page_content for doc in documents)


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
        return "\n\n".join(page.get_text("text") for page in doc)
    finally:
        doc.close()
