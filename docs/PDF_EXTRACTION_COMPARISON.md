# PDF Extraction Comparison

This project supports three extraction engines:

- `pymupdf`: fast baseline using local PyMuPDF text extraction.
- `opendataloader`: LangChain OpenDataLoader PDF integration. It produces Markdown locally and requires Java 11+ on `PATH`.
- `marker`: high-quality Markdown extraction, but heavy and slow on CPU-only machines.

## Configure The Production Engine

Set `EXTRACTION_ENGINE` in `src/configs/config.py`.

```python
EXTRACTION_ENGINE = "opendataloader"
```

The embedding pipeline keeps the same downstream contract for all engines:

1. Extract PDF content.
2. Apply finance-report cleanup filters.
3. Split with `MarkdownHeaderTextSplitter`.
4. Build parent-child chunks.
5. Store embeddings with the configured OpenRouter embedding model (`EMBEDDING_MODEL=baai/bge-m3` by default).

Changing extraction behavior can change chunk text. If you want a clean production index after changing the extraction engine or embedding model, delete `data/vector_db`, reset `reports.is_embedded`, clear `parent_chunks`, and rerun `python -m src.core.embed_pipeline --all`.

If `opendataloader` or `marker` fails during production extraction, the pipeline falls back to `pymupdf`.

## Compare Engines

Run a lightweight comparison before changing the default engine:

```bash
python -m src.core.compare_pdf_extractors --limit 10
```

This compares `pymupdf` and `opendataloader` over the first 10 PDFs in `data/downloaded/`.

Use explicit files or folders:

```bash
python -m src.core.compare_pdf_extractors data/downloaded --limit 20
```

Include `marker` only when the machine has enough RAM/GPU time:

```bash
python -m src.core.compare_pdf_extractors --engines pymupdf opendataloader marker --limit 5
```

Compare raw extractor output before cleanup filters:

```bash
python -m src.core.compare_pdf_extractors --raw --limit 5
```

Write extracted text samples for manual inspection:

```bash
python -m src.core.compare_pdf_extractors --limit 1 --sample-dir reports/pdf_samples
```

Use `--sample-chars 0` to write the full extracted text instead of a preview.

Outputs:

- `reports/pdf_extraction_compare.csv`
- `reports/pdf_extraction_compare.json`

## Metrics

The comparison process records:

- extraction status and elapsed seconds
- character, token, line, and block counts
- Markdown header line count
- Markdown table-like line count
- numeric-line ratio, useful for spotting table/sidebar noise
- Korean-line ratio, useful for detecting extraction failures or excess numeric fragments

Use the JSON summary for quick engine-level comparison, then inspect the CSV rows for outlier PDFs.
