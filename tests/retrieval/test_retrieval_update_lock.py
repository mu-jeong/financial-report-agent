from __future__ import annotations

from pathlib import Path

import pytest

from src.retrieval.update_lock import RetrievalUpdateLock, RetrievalUpdateLockError


def test_retrieval_update_lock_excludes_a_second_supported_writer(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()

    with RetrievalUpdateLock(data_root):
        with pytest.raises(RetrievalUpdateLockError, match="already running"):
            with RetrievalUpdateLock(data_root):
                raise AssertionError("a second updater must never enter")

    with RetrievalUpdateLock(data_root):
        pass


def test_retrieval_update_lock_is_scoped_to_one_data_root(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    with RetrievalUpdateLock(first), RetrievalUpdateLock(second):
        assert (first / ".retrieval-update.guard").is_file()
        assert (second / ".retrieval-update.guard").is_file()
