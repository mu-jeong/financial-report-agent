import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest
import requests

from src.core import report_crawler
from src.core.report_metadata import parse_filename

from src.core.report_crawler import (
    _crawl_start_date,
    classify_report_date,
    download_naver_reports,
    normalize_report_categories,
)
from src.retrieval.publication import PublicationCoordinator
from src.retrieval.recovery import RecoveryDisposition, StartupReconciler
from tests.retrieval.test_retrieval_publication import make_native_install


class _FakeResponse:
    def __init__(self, *, text="", content=b"", status_code=200, payload=None):
        self.text = text
        self.content = content
        self.status_code = status_code
        self.encoding = None
        self.payload = payload

    def json(self):
        if self.payload is None:
            raise ValueError("Invalid JSON response")
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")


API_BASE = "https://stock.naver.com/api/stockSecurity/researches/v2"


@contextmanager
def _expect_partial_download(capsys, processed=0):
    yield
    output = capsys.readouterr().out
    assert f"Naver research download complete: processed={processed} failed=1" in output
    assert "실패: 1건" in output


def _report_item(**overrides):
    return {"nid": "1", "itemName": "테스트기업", "title": "테스트제목",
            "brokerName": "테스트증권", "writeDate": "2026-07-18",
            "industryKoreanName": "반도체", **overrides}


def _list_response(items=None, has_next=False):
    return _FakeResponse(payload={"items": [_report_item()] if items is None else items,
                                  "hasNext": has_next})


def _valid_pdf_bytes():
    import fitz

    document = fitz.open()
    document.new_page()
    try:
        return document.tobytes()
    finally:
        document.close()


def _run_mocked_company_download(tmp_path, monkeypatch, pdf_response, calls, *, item=None):
    list_url = f"{API_BASE}/company"

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if url == list_url:
            return _list_response([item] if item is not None else None)
        if url == f"{list_url}/1":
            return _FakeResponse(payload={"attachUrl": "https://example.test/report.pdf"})
        return pdf_response

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("src.configs.config.SAVE_DIR", str(tmp_path))
    return report_crawler._download_naver_reports_locked(
        "2026-07-18",
        categories="company",
    )


@pytest.mark.parametrize("payload", [{}, [], {"items": [], "hasNext": "false"},
                                     {"items": {}, "hasNext": False},
                                     {"items": [], "hasNext": True}])
def test_malformed_list_is_not_zero_success(tmp_path, monkeypatch, payload):
    monkeypatch.setattr("src.configs.config.SAVE_DIR", str(tmp_path))
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse(payload=payload))
    with pytest.raises((ValueError, RuntimeError)):
        report_crawler._download_naver_reports_locked("2026-07-18")
    assert list(tmp_path.iterdir()) == []


def test_html_list_is_not_zero_success(tmp_path, monkeypatch):
    monkeypatch.setattr("src.configs.config.SAVE_DIR", str(tmp_path))
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse(text="<html>new site</html>"))
    with pytest.raises((ValueError, RuntimeError)):
        report_crawler._download_naver_reports_locked("2026-07-18")


def test_empty_valid_list_returns_zero(tmp_path, monkeypatch):
    monkeypatch.setattr("src.configs.config.SAVE_DIR", str(tmp_path))
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _list_response([]))
    assert report_crawler._download_naver_reports_locked("2026-07-18") == 0


@pytest.mark.parametrize("overrides", [{"writeDate": "invalid"}, {"nid": None},
                                        {"title": None}, {"brokerName": None}])
def test_malformed_report_metadata_fails(tmp_path, monkeypatch, overrides):
    monkeypatch.setattr("src.configs.config.SAVE_DIR", str(tmp_path))
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _list_response([_report_item(**overrides)]))
    with pytest.raises((ValueError, RuntimeError)):
        report_crawler._download_naver_reports_locked("2026-07-18")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("category,target", [("company", "테스트기업"),
                                             ("industry", "반도체"), ("economy", "null")])
def test_api_categories_preserve_filename_contract(tmp_path, monkeypatch, category, target):
    calls = []
    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if url == f"{API_BASE}/{category}":
            return _list_response()
        if url == f"{API_BASE}/{category}/1":
            return _FakeResponse(payload={"attachUrl": "https://example.test/download"})
        assert url == "https://example.test/download"
        return _FakeResponse(content=_valid_pdf_bytes())
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr("src.configs.config.SAVE_DIR", str(tmp_path))
    assert report_crawler._download_naver_reports_locked("2026-07-18", categories=category) == 1
    assert (tmp_path / f"{category}_2026-07-18_{target}_테스트증권_테스트제목.pdf").exists()
    assert calls[0][1]["params"]["startDate"] == "2026-07-18"
    assert calls[0][1]["params"]["endDate"] == "2026-07-18"


@pytest.mark.parametrize("detail", [{}, {"attachUrl": None}, {"attachUrl": "file:///secret"}])
def test_invalid_attachment_fails_without_pdf_request(tmp_path, monkeypatch, detail):
    calls = []
    def fake_get(url, **kwargs):
        calls.append(url)
        if url == f"{API_BASE}/company":
            return _list_response()
        assert url == f"{API_BASE}/company/1"
        return _FakeResponse(payload=detail)
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr("src.configs.config.SAVE_DIR", str(tmp_path))
    assert report_crawler._download_naver_reports_locked("2026-07-18") == 0
    assert len(calls) == 2
    assert list(tmp_path.iterdir()) == []


def test_pagination_deduplicates_and_respects_target_count(tmp_path, monkeypatch):
    indexes = []
    def fake_get(url, **kwargs):
        if url == f"{API_BASE}/company":
            index = kwargs["params"]["index"]
            indexes.append(index)
            if index == 0:
                return _list_response(has_next=True)
            assert index == 1
            return _list_response([_report_item(), _report_item(nid="2", title="second")], True)
        if url.startswith(f"{API_BASE}/company/"):
            return _FakeResponse(payload={"attachUrl": "https://example.test/report.pdf"})
        return _FakeResponse(content=_valid_pdf_bytes())
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr("src.configs.config.SAVE_DIR", str(tmp_path))
    assert report_crawler._download_naver_reports_locked("2026-07-18", target_count=2) == 2
    assert indexes == [0, 1]
    assert len(list(tmp_path.glob("*.pdf"))) == 2


def test_repeated_page_fails_instead_of_looping(tmp_path, monkeypatch):
    def fake_get(url, **kwargs):
        if url == f"{API_BASE}/company":
            assert kwargs["params"]["index"] <= 1
            return _list_response(has_next=True)
        if url == f"{API_BASE}/company/1":
            return _FakeResponse(payload={"attachUrl": "https://example.test/report.pdf"})
        return _FakeResponse(content=_valid_pdf_bytes())
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr("src.configs.config.SAVE_DIR", str(tmp_path))
    with pytest.raises((ValueError, RuntimeError)):
        report_crawler._download_naver_reports_locked("2026-07-18")


@pytest.mark.parametrize("target_date,lookback,expected", [
    (None, 0, 1), (None, 1, 2), ("2026-07-17", 0, 1),
])
def test_date_window_and_latest_mode(tmp_path, monkeypatch, target_date, lookback, expected):
    detail_ids = []
    def fake_get(url, **kwargs):
        if url == f"{API_BASE}/company":
            return _list_response([
                _report_item(),
                _report_item(nid="2", writeDate="2026-07-17"),
                _report_item(nid="3", writeDate="2026-07-16"),
            ])
        if url.startswith(f"{API_BASE}/company/"):
            detail_ids.append(url.rsplit("/", 1)[1])
            return _FakeResponse(payload={"attachUrl": "https://example.test/report.pdf"})
        return _FakeResponse(content=_valid_pdf_bytes())
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr("src.configs.config.SAVE_DIR", str(tmp_path))
    assert report_crawler._download_naver_reports_locked(target_date, lookback_days=lookback) == expected
    assert detail_ids == (["2"] if target_date else ["1", "2"][:expected])


def test_download_holds_cutover_fence_for_guard_and_source_writes(
    tmp_path,
    monkeypatch,
):
    events = []
    data_root = tmp_path / "data"
    data_root.mkdir()

    class FakeUpdateLock:
        def __init__(self, observed_root):
            assert Path(observed_root) == data_root

        def __enter__(self):
            events.append("locked")
            return self

        def __exit__(self, *_args):
            events.append("unlocked")

    def guarded():
        assert events == ["locked"]
        events.append("guarded")

    def download(*_args, **_kwargs):
        assert events == ["locked", "guarded", "catalog"]
        assert _kwargs["registered_paths"] == frozenset({"downloaded/existing.pdf"})
        events.append("downloaded")
        return 3

    def registered_paths(*, data_root):
        assert str(data_root) == str(tmp_path / "data")
        assert events == ["locked", "guarded"]
        events.append("catalog")
        return frozenset({"downloaded/existing.pdf"})

    monkeypatch.setattr("src.configs.config.DATA_ROOT", str(data_root))
    monkeypatch.setattr(report_crawler, "RetrievalUpdateLock", FakeUpdateLock)
    monkeypatch.setattr(report_crawler, "guard_before_report_download", guarded)
    monkeypatch.setattr(report_crawler, "_download_naver_reports_locked", download)
    monkeypatch.setattr("src.core.report_corpus.list_active_source_paths", registered_paths)

    assert report_crawler.download_naver_reports("2026-07-18") == 3
    assert events == ["locked", "guarded", "catalog", "downloaded", "unlocked"]


def test_normalize_report_categories_defaults_to_company():
    assert normalize_report_categories(None) == ["company"]
    assert normalize_report_categories("") == ["company"]


def test_normalize_report_categories_accepts_comma_separated_selection():
    assert normalize_report_categories("industry,economy") == ["industry", "economy"]


def test_normalize_report_categories_expands_all():
    assert normalize_report_categories("all") == ["company", "industry", "economy"]


def test_normalize_report_categories_deduplicates_preserving_order():
    assert normalize_report_categories(["economy", "company", "economy"]) == ["economy", "company"]


def test_normalize_report_categories_rejects_unknown_values():
    with pytest.raises(ValueError):
        normalize_report_categories("company,invalid")


def test_classify_report_date_skips_newer_than_requested_end():
    assert (
        classify_report_date(
            report_date=date(2026, 5, 30),
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 29),
        )
        == "skip_newer"
    )


def test_classify_report_date_processes_within_window():
    assert (
        classify_report_date(
            report_date=date(2026, 5, 29),
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 31),
        )
        == "process"
    )


def test_classify_report_date_stops_when_older_than_window():
    assert (
        classify_report_date(
            report_date=date(2026, 4, 30),
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 31),
        )
        == "stop_older"
    )


def test_crawl_start_date_uses_target_count_safety_window():
    assert _crawl_start_date(
        end_date=date(2026, 5, 31),
        lookback_days=0,
        target_count=100,
        max_lookback_days=30,
    ) == date(2026, 5, 1)


def test_crawl_start_date_prefers_explicit_lookback_window():
    assert _crawl_start_date(
        end_date=date(2026, 5, 31),
        lookback_days=7,
        target_count=0,
        max_lookback_days=30,
    ) == date(2026, 5, 24)


def test_pdf_http_error_is_not_persisted_or_counted(tmp_path, monkeypatch, capsys):
    calls = []

    with _expect_partial_download(capsys):
        _run_mocked_company_download(
            tmp_path,
            monkeypatch,
            _FakeResponse(
                content=b"<html>service unavailable</html>",
                status_code=503,
            ),
            calls,
        )

    assert list(tmp_path.glob("*.pdf")) == []


def test_list_http_error_fails_before_parsing_or_writing(tmp_path, monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeResponse(
            text="<html>service unavailable</html>",
            status_code=503,
        )

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("src.configs.config.SAVE_DIR", str(tmp_path))

    with pytest.raises(requests.HTTPError, match="HTTP 503"):
        report_crawler._download_naver_reports_locked(
            "2026-07-18",
            categories="company",
        )

    assert len(calls) == 1
    assert list(tmp_path.iterdir()) == []


def test_non_pdf_success_response_is_not_persisted_or_counted(tmp_path, monkeypatch, capsys):
    calls = []

    with _expect_partial_download(capsys):
        _run_mocked_company_download(
            tmp_path,
            monkeypatch,
            _FakeResponse(content=b"<html>login required</html>"),
            calls,
        )

    assert list(tmp_path.glob("*.pdf")) == []


def test_pdf_timeout_is_not_persisted_or_counted(tmp_path, monkeypatch, capsys):
    list_url = f"{API_BASE}/company"
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if url == list_url:
            return _list_response()
        if url == f"{list_url}/1":
            return _FakeResponse(payload={"attachUrl": "https://example.test/report.pdf"})
        raise requests.Timeout("download timed out")

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("src.configs.config.SAVE_DIR", str(tmp_path))

    with _expect_partial_download(capsys):
        report_crawler._download_naver_reports_locked(
            "2026-07-18",
            categories="company",
        )
    assert len(calls) == 3
    assert all(call_kwargs.get("timeout") for _, call_kwargs in calls)
    assert list(tmp_path.iterdir()) == []


def test_partial_download_failure_preserves_successes_and_returns_count(
    tmp_path,
    monkeypatch,
    capsys,
):
    list_url = f"{API_BASE}/company"
    good_url = "https://example.test/good.pdf"
    bad_url = "https://example.test/bad.pdf"

    def fake_get(url, **kwargs):
        if url == list_url:
            second = {**_report_item(), "nid": "2", "title": "실패"}
            return _list_response([_report_item(), second])
        if url == f"{list_url}/1":
            return _FakeResponse(payload={"attachUrl": good_url})
        if url == f"{list_url}/2":
            return _FakeResponse(payload={"attachUrl": bad_url})
        if url == good_url:
            return _FakeResponse(content=_valid_pdf_bytes())
        return _FakeResponse(status_code=503)

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("src.configs.config.SAVE_DIR", str(tmp_path))

    with _expect_partial_download(capsys, processed=1):
        assert report_crawler._download_naver_reports_locked(
            "2026-07-18",
            categories="company",
        ) == 1

    saved_reports = list(tmp_path.glob("*.pdf"))
    assert len(saved_reports) == 1
    assert report_crawler._is_valid_pdf_file(saved_reports[0])


def test_list_and_pdf_requests_use_a_timeout(tmp_path, monkeypatch):
    calls = []

    assert (
        _run_mocked_company_download(
            tmp_path,
            monkeypatch,
            _FakeResponse(content=_valid_pdf_bytes()),
            calls,
        )
        == 1
    )

    assert len(calls) == 3
    assert all(call_kwargs.get("timeout") for _, call_kwargs in calls)


def test_failed_category_does_not_block_next_category(tmp_path, monkeypatch, capsys):
    def fake_get(url, **kwargs):
        if url in (f"{API_BASE}/company", f"{API_BASE}/industry"):
            return _list_response()
        if url.startswith(API_BASE):
            category = url.split("/")[-2]
            return _FakeResponse(payload={"attachUrl": f"https://example.test/{category}.pdf"})
        if url.endswith("company.pdf"):
            return _FakeResponse(status_code=503)
        return _FakeResponse(content=_valid_pdf_bytes())

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr("src.configs.config.SAVE_DIR", str(tmp_path))
    with _expect_partial_download(capsys, processed=1):
        assert report_crawler._download_naver_reports_locked(
            "2026-07-18", categories=["company", "industry"]
        ) == 1
    files = list(tmp_path.glob("*.pdf"))
    assert len(files) == 1 and files[0].name.startswith("industry_")


def test_atomic_save_does_not_leave_partial_final_file_on_replace_error(
    tmp_path,
    monkeypatch,
):
    final_path = tmp_path / "report.pdf"

    def fail_replace(_source, _destination):
        raise OSError("disk error")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="disk error"):
        report_crawler._save_pdf_atomically(final_path, _valid_pdf_bytes())

    assert not final_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_valid_existing_pdf_is_counted_without_redownload(tmp_path, monkeypatch):
    expected_name = "company_2026-07-18_테스트기업_테스트증권_테스트제목.pdf"
    (tmp_path / expected_name).write_bytes(_valid_pdf_bytes())
    calls = []

    assert (
        _run_mocked_company_download(
            tmp_path,
            monkeypatch,
            _FakeResponse(content=b"must not be requested"),
            calls,
        )
        == 1
    )
    assert len(calls) == 1


def test_corrupt_existing_pdf_is_replaced_with_valid_download(tmp_path, monkeypatch):
    expected_path = tmp_path / "company_2026-07-18_테스트기업_테스트증권_테스트제목.pdf"
    expected_path.write_bytes(b"<html>old failed download</html>")
    valid_pdf = _valid_pdf_bytes()
    calls = []

    assert (
        _run_mocked_company_download(
            tmp_path,
            monkeypatch,
            _FakeResponse(content=valid_pdf),
            calls,
        )
        == 1
    )
    assert len(calls) == 3
    assert expected_path.read_bytes() == valid_pdf


def test_new_download_preserves_title_longer_than_legacy_limit(tmp_path, monkeypatch):
    title = "A complete semiconductor outlook " + "with detail " * 5
    calls = []

    assert _run_mocked_company_download(
        tmp_path,
        monkeypatch,
        _FakeResponse(content=_valid_pdf_bytes()),
        calls,
        item=_report_item(title=title),
    ) == 1

    saved_path = next(tmp_path.glob("*.pdf"))
    assert title.strip() in saved_path.name
    assert len(title) > 50


@pytest.mark.parametrize(
    "title",
    [
        "한글보고서제목" * 200,
        "ascii-report-title-" * 500,
        "📈🚀" * 1000,
    ],
)
def test_extreme_titles_fit_platform_filename_and_path_limits(tmp_path, title):
    prefix = "company_2026-07-18_테스트기업_테스트증권_"
    report_path = report_crawler._report_file_path(tmp_path, prefix, title)

    assert report_crawler._filename_fits(tmp_path, Path(report_path).name)
    assert Path(report_path).name.startswith(prefix)
    assert Path(report_path).name.endswith(".pdf")
    assert "~" in Path(report_path).stem
    metadata = parse_filename(Path(report_path).name)
    assert metadata is not None
    assert metadata["title"] == Path(report_path).stem.split("_", 4)[4]


def test_windows_filename_limit_counts_utf16_code_units(monkeypatch):
    monkeypatch.setattr(report_crawler, "_IS_WINDOWS", True)
    monkeypatch.setattr(report_crawler, "_path_length_limits", lambda _directory: (255, 10_000))

    assert report_crawler._filename_fits("C:\\reports", "😀" * 127 + "a")
    assert not report_crawler._filename_fits("C:\\reports", "😀" * 127 + "ab")


def test_windows_normal_path_limit_is_259_utf16_units(monkeypatch):
    monkeypatch.setattr(report_crawler, "_IS_WINDOWS", True)
    monkeypatch.setattr(report_crawler, "_path_length_limits", lambda _directory: (255, 259))
    monkeypatch.setattr(report_crawler.os.path, "abspath", lambda _path: "x" * 259)

    assert report_crawler._filename_fits("C:\\reports", "report.pdf")
    monkeypatch.setattr(report_crawler.os.path, "abspath", lambda _path: "x" * 260)
    assert not report_crawler._filename_fits("C:\\reports", "report.pdf")


def test_posix_limits_use_filesystem_bytes_and_exclude_path_nul(monkeypatch):
    monkeypatch.setattr(report_crawler, "_IS_WINDOWS", False)
    monkeypatch.setattr(
        report_crawler.os,
        "pathconf",
        lambda _directory, name: 255 if name == "PC_NAME_MAX" else 4096,
        raising=False,
    )
    monkeypatch.setattr(report_crawler.os, "fsencode", lambda value: value.encode("utf-8"))

    assert report_crawler._path_length_limits("/reports") == (255, 4095)
    monkeypatch.setattr(report_crawler, "_path_length_limits", lambda _directory: (255, 10_000))
    assert report_crawler._filename_fits("/reports", "가" * 85)
    assert not report_crawler._filename_fits("/reports", "가" * 85 + "a")


def test_truncated_titles_with_same_prefix_have_distinct_stable_names(tmp_path):
    prefix = "company_2026-07-18_테스트기업_테스트증권_"
    shared = "동일한 매우 긴 제목" * 200

    first = report_crawler._report_file_path(tmp_path, prefix, shared + "첫번째")
    second = report_crawler._report_file_path(tmp_path, prefix, shared + "두번째")

    assert first != second
    assert first == report_crawler._report_file_path(tmp_path, prefix, shared + "첫번째")


def test_long_directory_reduces_title_without_exceeding_path_limit(tmp_path):
    report_dir = tmp_path / ("long-directory-" * 5)
    report_dir.mkdir()
    prefix = "company_2026-07-18_target_broker_"
    title = "detailed-title-" * 100

    report_path = report_crawler._report_file_path(report_dir, prefix, title)

    assert report_crawler._filename_fits(report_dir, Path(report_path).name)
    assert len(Path(report_path).name) > len(prefix) + len(".pdf")


def test_existing_long_title_legacy_file_is_reused_without_redownload(tmp_path, monkeypatch):
    title = "기존에 색인된 장문 보고서 제목" * 5
    prefix = "company_2026-07-18_테스트기업_테스트증권_"
    legacy_path = tmp_path / f"{prefix}{title[:50]}....pdf"
    legacy_path.write_bytes(_valid_pdf_bytes())
    calls = []

    assert _run_mocked_company_download(
        tmp_path,
        monkeypatch,
        _FakeResponse(content=b"must not be requested"),
        calls,
        item=_report_item(title=title),
    ) == 1
    assert len(calls) == 1
    assert legacy_path.exists()
    assert len(list(tmp_path.glob("*.pdf"))) == 1


def test_corrupt_long_title_legacy_file_is_repaired_in_place(tmp_path, monkeypatch):
    title = "기존에 색인된 장문 보고서 제목" * 5
    prefix = "company_2026-07-18_테스트기업_테스트증권_"
    legacy_path = tmp_path / f"{prefix}{title[:50]}....pdf"
    legacy_path.write_bytes(b"corrupt")
    valid_pdf = _valid_pdf_bytes()
    calls = []

    assert _run_mocked_company_download(
        tmp_path,
        monkeypatch,
        _FakeResponse(content=valid_pdf),
        calls,
        item=_report_item(title=title),
    ) == 1
    assert len(calls) == 3
    assert legacy_path.read_bytes() == valid_pdf
    assert len(list(tmp_path.glob("*.pdf"))) == 1


def test_missing_registered_legacy_filename_is_preserved(tmp_path):
    from src.retrieval.identity import normalize_relative_path

    prefix = "company_2026-07-18_target_broker_"
    title = "compatibility-\uf967-" * 5
    legacy_name = f"{prefix}{title[:50]}....pdf"
    registered_paths = frozenset({normalize_relative_path(f"downloaded/{legacy_name}")})

    chosen = report_crawler._report_file_path(
        tmp_path, prefix, title, registered_paths=registered_paths,
    )

    assert chosen == str(tmp_path / legacy_name)
    assert not Path(chosen).exists()


@pytest.mark.parametrize("title", ["long-title-" * 7, "long-title-" * 100])
def test_registered_new_filename_takes_priority_over_unindexed_legacy_file(tmp_path, title):
    from src.retrieval.identity import normalize_relative_path

    prefix = "company_2026-07-18_target_broker_"
    new_path = report_crawler._report_file_path(tmp_path, prefix, title)
    registered_paths = frozenset({normalize_relative_path(f"downloaded/{Path(new_path).name}")})
    legacy_path = tmp_path / f"{prefix}{title[:50]}....pdf"
    legacy_path.write_bytes(_valid_pdf_bytes())

    assert report_crawler._report_file_path(
        tmp_path, prefix, title, registered_paths=registered_paths,
    ) == new_path


def test_atomic_save_supports_maximized_long_final_filename(tmp_path):
    prefix = "company_2026-07-18_target_broker_"
    destination = report_crawler._report_file_path(tmp_path, prefix, "long-title-" * 100)

    report_crawler._save_pdf_atomically(destination, _valid_pdf_bytes())

    assert report_crawler._is_valid_pdf_file(destination)
    assert not list(tmp_path.glob(".report-*.tmp"))


def test_metadata_prefix_that_cannot_fit_is_counted_as_failed_report(
    tmp_path,
    monkeypatch,
    capsys,
):
    calls = []
    monkeypatch.setattr(report_crawler, "_path_length_limits", lambda _directory: (20, 40))

    with _expect_partial_download(capsys):
        assert _run_mocked_company_download(
            tmp_path,
            monkeypatch,
            _FakeResponse(content=b"must not be requested"),
            calls,
        ) == 0

    assert len(calls) == 1
    assert not list(tmp_path.iterdir())


def test_failed_repair_preserves_corrupt_existing_file(tmp_path, monkeypatch, capsys):
    expected_path = tmp_path / "company_2026-07-18_테스트기업_테스트증권_테스트제목.pdf"
    original_content = b"<html>old failed download</html>"
    expected_path.write_bytes(original_content)
    calls = []

    with _expect_partial_download(capsys):
        _run_mocked_company_download(
            tmp_path,
            monkeypatch,
            _FakeResponse(content=b"<html>new failed download</html>"),
            calls,
        )
    assert len(calls) == 3
    assert expected_path.read_bytes() == original_content


def test_download_guard_blocks_before_crawler_dependencies_or_source_writes(
    tmp_path,
    monkeypatch,
):
    events = []
    data_root = tmp_path / "data"
    data_root.mkdir()

    def blocked_guard():
        events.append("guard")
        raise RuntimeError("write runtime blocked")

    monkeypatch.setattr(
        "src.core.report_crawler.guard_before_report_download",
        blocked_guard,
    )
    monkeypatch.setattr(
        "src.configs.config.DATA_ROOT",
        str(data_root),
    )
    monkeypatch.setattr(
        "os.makedirs",
        lambda *_args, **_kwargs: events.append("mkdir"),
    )

    with pytest.raises(RuntimeError, match="write runtime blocked"):
        download_naver_reports("2026-07-16")

    assert events == ["guard"]


def test_unreadable_source_catalog_blocks_before_network_or_downloads(tmp_path, monkeypatch):
    from src.core.report_corpus import ReportCorpusError

    data_root = tmp_path / "data"
    catalog = data_root / "retrieval/v2/catalog.sqlite3"
    catalog.parent.mkdir(parents=True)
    catalog.write_bytes(b"unreadable catalog")
    save_dir = tmp_path / "must-not-be-created"
    monkeypatch.setattr("src.configs.config.DATA_ROOT", str(data_root))
    monkeypatch.setattr("src.configs.config.SAVE_DIR", str(save_dir))
    monkeypatch.setattr(report_crawler, "guard_before_report_download", lambda: None)

    def unexpected_request(*_args, **_kwargs):
        pytest.fail("catalog errors must stop downloads before network access")

    monkeypatch.setattr(requests, "get", unexpected_request)

    with pytest.raises(ReportCorpusError):
        download_naver_reports("2026-07-18")

    assert not save_dir.exists()
    assert catalog.read_bytes() == b"unreadable catalog"


def test_direct_crawler_command_fails_before_source_mutation_when_degraded(tmp_path):
    native_fixture = tmp_path / "native"
    native_fixture.mkdir()
    data_root, request = make_native_install(native_fixture)
    PublicationCoordinator(data_root).publish(request)
    active_snapshot = (
        data_root / "retrieval" / "v2" / "snapshots" / "snapshot-successor.faiss"
    )
    active_snapshot.write_bytes(b"corrupt-active-snapshot")
    recovery = StartupReconciler(data_root).reconcile()
    assert recovery.disposition == RecoveryDisposition.PREDECESSOR_DEGRADED
    save_dir = tmp_path / "must-not-be-created"
    environment = os.environ.copy()
    environment["DATA_ROOT"] = str(data_root)
    environment["SAVE_DIR"] = str(save_dir)
    root = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [sys.executable, "-m", "src.core.report_crawler"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode != 0
    assert "RetrievalWriteBlocked" in completed.stderr
    assert not save_dir.exists()
