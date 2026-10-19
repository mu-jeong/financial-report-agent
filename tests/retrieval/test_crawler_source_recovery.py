from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.core import report_crawler
from src.core.report_metadata import parse_filename
from src.retrieval.continuous_update import execute_continuous_update
from src.retrieval.initializer import initialize_empty_native
from src.retrieval.writer_lock import NativeWriterLock
from tests.retrieval.native_build_fixtures import DeterministicEmbeddings


API_BASE = "https://stock.naver.com/api/stockSecurity/researches/v2"


class _FakeResponse:
    def __init__(self, *, payload=None, content: bytes = b"") -> None:
        self.payload = payload
        self.content = content

    def json(self):
        if self.payload is None:
            raise ValueError("response does not contain JSON")
        return self.payload

    def raise_for_status(self) -> None:
        return None


def _valid_pdf_bytes() -> bytes:
    import fitz

    document = fitz.open()
    document.new_page()
    try:
        return document.tobytes()
    finally:
        document.close()


def _initialize_native(data_root: Path) -> None:
    data_root.mkdir()
    with NativeWriterLock(data_root) as lease:
        initialize_empty_native(data_root, writer_lease=lease)


def _active_report_uids(data_root: Path) -> dict[str, str]:
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    with sqlite3.connect(catalog) as connection:
        return dict(
            connection.execute(
                "SELECT canonical_relative_path, report_uid FROM active_reports"
            )
        )


def _update_options(embeddings: DeterministicEmbeddings) -> dict[str, object]:
    return {
        "embeddings": embeddings,
        "model": "model-a",
        "extractor_name": "deterministic-extractor",
        "extractor": lambda path, _engine: f"content for {path.name}",
        "metadata_parser": parse_filename,
        "allow_extraction_fallback": False,
        "use_parent_child": True,
        "parent_chunk_size": 2000,
        "child_chunk_size": 500,
        "metric": "l2",
        "normalization": "none",
    }


def _install_api_mock(
    monkeypatch: pytest.MonkeyPatch,
    pdf_bytes: bytes,
    items_by_category: dict[str, list[dict[str, object]]],
) -> None:
    def fake_get(url: str, **_kwargs):
        for category, items in items_by_category.items():
            list_url = f"{API_BASE}/{category}"
            if url == list_url:
                return _FakeResponse(payload={"items": items, "hasNext": False})
            if url.startswith(f"{list_url}/"):
                nid = url.rsplit("/", 1)[1]
                return _FakeResponse(
                    payload={"attachUrl": f"https://example.test/{category}-{nid}.pdf"}
                )
        if url.startswith("https://example.test/"):
            return _FakeResponse(content=pdf_bytes)
        raise AssertionError(f"unexpected request: {url}")

    monkeypatch.setattr("requests.get", fake_get)


@pytest.mark.parametrize(
    ("name_strategy", "title"),
    [
        ("legacy-50", "legacy " + "x" * 90),
        ("full-long", "full long title " + "y" * 70),
        ("hash-truncated", "hash title " + "z" * 400),
    ],
)
def test_missing_indexed_report_is_recrawled_to_the_same_canonical_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name_strategy: str,
    title: str,
) -> None:
    data_root = tmp_path / "native"
    sources = tmp_path / "external-downloads"
    sources.mkdir()
    _initialize_native(data_root)
    pdf_bytes = _valid_pdf_bytes()
    prefix = "company_2026-07-18_Target_Broker_"

    monkeypatch.setattr("src.configs.config.DATA_ROOT", str(data_root))
    monkeypatch.setattr("src.configs.config.SAVE_DIR", str(sources))
    item = {
        "nid": "1",
        "itemName": "Target",
        "title": title,
        "brokerName": "Broker",
        "writeDate": "2026-07-18",
        "industryKoreanName": "Sector",
    }
    _install_api_mock(monkeypatch, pdf_bytes, {"company": [item]})

    if name_strategy == "legacy-50":
        expected = sources / f"{prefix}{title[:50]}....pdf"
        expected.write_bytes(pdf_bytes)
    else:
        assert report_crawler.download_naver_reports(
            "2026-07-18", categories="company"
        ) == 1
        [expected] = list(sources.glob("*.pdf"))
        if name_strategy == "full-long":
            assert expected.name == f"{prefix}{title}.pdf"
        else:
            assert "~" in expected.stem
            assert expected.name != f"{prefix}{title}.pdf"

    initial_embeddings = DeterministicEmbeddings()
    first = execute_continuous_update(
        data_root,
        sources,
        **_update_options(initial_embeddings),
    )
    assert first is not None
    before = _active_report_uids(data_root)
    canonical_path = f"downloaded/{expected.name}"
    assert set(before) == {canonical_path}

    expected.unlink()
    assert report_crawler.download_naver_reports(
        "2026-07-18", categories="company"
    ) == 1

    assert expected.read_bytes() == pdf_bytes
    assert list(sources.glob("*.pdf")) == [expected]
    restart_embeddings = DeterministicEmbeddings()
    assert execute_continuous_update(
        data_root,
        sources,
        **_update_options(restart_embeddings),
    ) is None
    assert restart_embeddings.calls == []
    assert _active_report_uids(data_root) == before


def test_full_recrawl_from_new_naming_preserves_all_category_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "native"
    sources = tmp_path / "external-downloads"
    sources.mkdir()
    _initialize_native(data_root)
    pdf_bytes = _valid_pdf_bytes()
    title = "complete collection title " + "w" * 64
    items = {
        category: [
            {
                "nid": str(index),
                "itemName": "Target",
                "title": f"{title} {category}",
                "brokerName": "Broker",
                "writeDate": "2026-07-18",
                "industryKoreanName": "Sector",
            }
        ]
        for index, category in enumerate(("company", "industry", "economy"), 1)
    }
    monkeypatch.setattr("src.configs.config.DATA_ROOT", str(data_root))
    monkeypatch.setattr("src.configs.config.SAVE_DIR", str(sources))
    _install_api_mock(monkeypatch, pdf_bytes, items)

    assert report_crawler.download_naver_reports(
        "2026-07-18", categories="all"
    ) == 3
    original_files = sorted(sources.glob("*.pdf"))
    assert len(original_files) == 3
    assert all(len(parse_filename(path.name)["title"]) > 50 for path in original_files)

    first = execute_continuous_update(
        data_root,
        sources,
        **_update_options(DeterministicEmbeddings()),
    )
    assert first is not None
    before = _active_report_uids(data_root)
    assert len(before) == 3

    for path in original_files:
        path.unlink()
    assert report_crawler.download_naver_reports(
        "2026-07-18", categories="all"
    ) == 3

    assert sorted(path.name for path in sources.glob("*.pdf")) == [
        path.name for path in original_files
    ]
    restart_embeddings = DeterministicEmbeddings()
    assert execute_continuous_update(
        data_root,
        sources,
        **_update_options(restart_embeddings),
    ) is None
    assert restart_embeddings.calls == []
    assert _active_report_uids(data_root) == before


def test_ready_delta_only_report_is_recovered_before_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "native"
    sources = tmp_path / "external-downloads"
    sources.mkdir()
    _initialize_native(data_root)
    pdf_bytes = _valid_pdf_bytes()
    (sources / "company_2026-07-17_Base_Broker_Baseline.pdf").write_bytes(pdf_bytes)
    assert execute_continuous_update(
        data_root,
        sources,
        **_update_options(DeterministicEmbeddings()),
    ) is not None

    title = "ready delta legacy " + "q" * 80
    legacy = sources / f"company_2026-07-18_Target_Broker_{title[:50]}....pdf"
    legacy.write_bytes(pdf_bytes)

    def interrupt_after_delta(_publication) -> None:
        raise RuntimeError("interrupt after ready delta")

    with pytest.raises(RuntimeError, match="interrupt after ready delta"):
        execute_continuous_update(
            data_root,
            sources,
            progress_callback=interrupt_after_delta,
            **_update_options(DeterministicEmbeddings()),
        )
    delta_active = _active_report_uids(data_root)
    delta_path = f"downloaded/{legacy.name}"
    assert delta_path in delta_active

    item = {
        "nid": "2",
        "itemName": "Target",
        "title": title,
        "brokerName": "Broker",
        "writeDate": "2026-07-18",
        "industryKoreanName": "Sector",
    }
    monkeypatch.setattr("src.configs.config.DATA_ROOT", str(data_root))
    monkeypatch.setattr("src.configs.config.SAVE_DIR", str(sources))
    _install_api_mock(monkeypatch, pdf_bytes, {"company": [item]})
    legacy.unlink()

    assert report_crawler.download_naver_reports(
        "2026-07-18", categories="company"
    ) == 1
    assert legacy.read_bytes() == pdf_bytes
    assert not (sources / f"company_2026-07-18_Target_Broker_{title}.pdf").exists()

    restart_embeddings = DeterministicEmbeddings()
    resumed = execute_continuous_update(
        data_root,
        sources,
        **_update_options(restart_embeddings),
    )
    assert resumed is not None
    assert resumed.delta_publications == ()
    assert resumed.attempted_report_uids == ()
    assert restart_embeddings.calls == []
    assert _active_report_uids(data_root) == delta_active
