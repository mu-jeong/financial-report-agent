from __future__ import annotations

import argparse
import csv
import json
import re
import hashlib
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Iterable

from src.configs import config
from src.core import pdf_extraction

SUPPORTED_EXTRACTION_ENGINES = pdf_extraction.SUPPORTED_EXTRACTION_ENGINES
ENGINE_ALIASES = getattr(pdf_extraction, "ENGINE_ALIASES", {})
extract_pdf_text = pdf_extraction.extract_pdf_text
normalize_engine = pdf_extraction.normalize_engine

NUMERIC_TOKEN_RE = re.compile(r"^[+-]?\d[\d,]*(?:\.\d+)?%?$")
KOREAN_RE = re.compile(r"[\uac00-\ud7a3]")
DEFAULT_OUTPUT_DIR = Path("reports") / "pdf_extraction"


def collect_pdf_files(paths: list[str], limit: int) -> list[Path]:
    if paths:
        files: list[Path] = []
        for raw_path in paths:
            path = Path(raw_path)
            if path.is_dir():
                files.extend(sorted(path.glob("*.pdf")))
            elif path.suffix.lower() == ".pdf":
                files.append(path)
        return files[:limit] if limit > 0 else files

    save_dir = Path(config.SAVE_DIR)
    files = sorted(save_dir.glob("*.pdf"))
    return files[:limit] if limit > 0 else files


def build_metrics(text: str) -> dict[str, object]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    blocks = [block.strip() for block in text.split("\n\n") if block.strip()]

    numeric_lines = 0
    korean_lines = 0
    pipe_table_lines = 0
    header_lines = 0
    total_tokens = 0

    for line in lines:
        tokens = line.split()
        total_tokens += len(tokens)
        if tokens:
            numeric_count = sum(1 for token in tokens if NUMERIC_TOKEN_RE.match(token))
            if numeric_count / len(tokens) >= 0.5:
                numeric_lines += 1
        if KOREAN_RE.search(line):
            korean_lines += 1
        if line.count("|") >= 2:
            pipe_table_lines += 1
        if line.startswith("#"):
            header_lines += 1

    return {
        "char_count": len(text),
        "line_count": len(lines),
        "block_count": len(blocks),
        "avg_line_length": round(mean(len(line) for line in lines), 2) if lines else 0,
        "header_lines": header_lines,
        "pipe_table_lines": pipe_table_lines,
        "numeric_line_ratio": round(numeric_lines / len(lines), 4) if lines else 0,
        "korean_line_ratio": round(korean_lines / len(lines), 4) if lines else 0,
        "token_count": total_tokens,
    }


def compare_extractors(
    files: Iterable[Path],
    engines: list[str],
    *,
    raw: bool,
    sample_dir: Path | None = None,
    sample_chars: int = 4000,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []

    for pdf_path in files:
        for engine in engines:
            started = time.perf_counter()
            row: dict[str, object] = {
                "file_name": pdf_path.name,
                "file_path": str(pdf_path),
                "requested_engine": engine,
                "used_engine": "",
                "status": "ok",
                "elapsed_sec": 0,
                "error": "",
            }
            try:
                result = extract_pdf_text(
                    pdf_path,
                    engine,
                    clean=not raw,
                    allow_fallback=False,
                )
                row["used_engine"] = result.used_engine
                row.update(build_metrics(result.text))
                if sample_dir is not None:
                    row["sample_path"] = str(
                        write_sample(result.text, pdf_path, engine, sample_dir, sample_chars)
                    )
            except Exception as exc:
                row["status"] = "error"
                row["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                row["elapsed_sec"] = round(time.perf_counter() - started, 3)
            rows.append(row)

    return rows


def write_sample(
    text: str,
    pdf_path: Path,
    engine: str,
    sample_dir: Path,
    sample_chars: int,
) -> Path:
    sample_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(str(pdf_path).encode("utf-8")).hexdigest()[:8]
    safe_stem = re.sub(r"[^0-9A-Za-z_.-]+", "_", pdf_path.stem)[:80]
    sample_path = sample_dir / f"{safe_stem}_{digest}_{engine}.md"
    sample_text = text[:sample_chars] if sample_chars > 0 else text
    sample_path.write_text(sample_text, encoding="utf-8")
    return sample_path


def summarize(rows: list[dict[str, object]]) -> dict[str, object]:
    by_engine: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_engine.setdefault(str(row["requested_engine"]), []).append(row)

    summary: dict[str, object] = {}
    for engine, engine_rows in by_engine.items():
        ok_rows = [row for row in engine_rows if row["status"] == "ok"]
        summary[engine] = {
            "files": len(engine_rows),
            "success": len(ok_rows),
            "errors": len(engine_rows) - len(ok_rows),
            "avg_elapsed_sec": _avg(ok_rows, "elapsed_sec"),
            "avg_char_count": _avg(ok_rows, "char_count"),
            "avg_block_count": _avg(ok_rows, "block_count"),
            "avg_numeric_line_ratio": _avg(ok_rows, "numeric_line_ratio"),
            "avg_korean_line_ratio": _avg(ok_rows, "korean_line_ratio"),
            "fallbacks": dict(Counter(row.get("used_engine", "") for row in ok_rows)),
        }
    return summary


def _avg(rows: list[dict[str, object]], key: str) -> float:
    values = [float(row[key]) for row in rows if key in row and row[key] != ""]
    return round(mean(values), 4) if values else 0


def write_csv(rows: list[dict[str, object]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row})
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(rows: list[dict[str, object]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"summary": summarize(rows), "rows": rows}
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_pdf_extraction_comparison(
    paths: list[str],
    engines: list[str],
    *,
    limit: int = 10,
    raw: bool = False,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    run_id: str | None = None,
    write_samples: bool = False,
    sample_chars: int = 4000,
) -> dict[str, object]:
    """Run and persist a PDF extraction-engine comparison.

    This wraps the CLI primitives so Monitoring Mode can run the same
    evaluation without shelling out. The returned payload is intentionally small
    enough to store in Streamlit session state while still linking to the
    persisted CSV/JSON/sample artifacts.
    """
    normalized_engines = [config_engine(engine) for engine in engines]
    files = collect_pdf_files(paths, limit)
    if not files:
        raise ValueError("No PDF files found for comparison.")

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    sample_dir = output_root / f"{run_id}_samples" if write_samples else None

    rows = compare_extractors(
        files,
        normalized_engines,
        raw=raw,
        sample_dir=sample_dir,
        sample_chars=sample_chars,
    )
    csv_path = output_root / f"{run_id}.csv"
    json_path = output_root / f"{run_id}.json"
    write_csv(rows, csv_path)
    write_json(rows, json_path)

    return {
        "run_id": run_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "paths": paths,
        "engines": normalized_engines,
        "limit": limit,
        "raw": raw,
        "file_count": len(files),
        "csv_path": str(csv_path),
        "json_path": str(json_path),
        "sample_dir": str(sample_dir) if sample_dir is not None else "",
        "summary": summarize(rows),
        "rows": rows,
    }


def config_engine(engine: str) -> str:
    """Normalize an engine name for comparison configuration."""
    return normalize_engine(engine)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare PDF extraction engines.")
    parser.add_argument(
        "paths",
        nargs="*",
        help="PDF files or directories. Defaults to config.SAVE_DIR.",
    )
    parser.add_argument(
        "--engines",
        nargs="+",
        default=["pymupdf", "opendataloader"],
        help=(
            "Extraction engines to compare. Supported: "
            f"{', '.join(sorted(SUPPORTED_EXTRACTION_ENGINES))}. "
            f"Aliases: {', '.join(f'{alias}->{target}' for alias, target in sorted(ENGINE_ALIASES.items()))}. "
            "Optional engines are intentionally opt-in."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of PDF files to sample. Use 0 for all files.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Compare raw extractor output before finance-report cleanup filters.",
    )
    parser.add_argument(
        "--csv",
        default=str(Path("reports") / "pdf_extraction_compare.csv"),
        help="CSV output path.",
    )
    parser.add_argument(
        "--json",
        default=str(Path("reports") / "pdf_extraction_compare.json"),
        help="JSON output path.",
    )
    parser.add_argument(
        "--sample-dir",
        default="",
        help="Optional directory for per-engine extracted text samples.",
    )
    parser.add_argument(
        "--sample-chars",
        type=int,
        default=4000,
        help="Maximum characters to write per sample. Use 0 for full extracted text.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    files = collect_pdf_files(args.paths, args.limit)
    if not files:
        raise SystemExit("No PDF files found for comparison.")

    engines = [config_engine(engine) for engine in args.engines]
    sample_dir = Path(args.sample_dir) if args.sample_dir else None
    rows = compare_extractors(
        files,
        engines,
        raw=args.raw,
        sample_dir=sample_dir,
        sample_chars=args.sample_chars,
    )
    write_csv(rows, Path(args.csv))
    write_json(rows, Path(args.json))

    print(json.dumps(summarize(rows), ensure_ascii=False, indent=2))
    print(f"\nCSV: {Path(args.csv).resolve()}")
    print(f"JSON: {Path(args.json).resolve()}")


if __name__ == "__main__":
    main()
