import pytest

from scripts import run_fast_tests


def test_fast_runner_preserves_forwarded_scope_arguments():
    assert run_fast_tests.build_pytest_args(
        ["tests/test_metadata_filters.py", "-k", "report_type_only_followup"]
    ) == [
        "-q",
        "-m",
        "not slow",
        "tests/test_metadata_filters.py",
        "-k",
        "report_type_only_followup",
    ]


@pytest.mark.parametrize("override", [["-m", "slow"], ["-mslow"], ["-m=slow"]])
def test_fast_runner_rejects_marker_overrides(override):
    with pytest.raises(ValueError, match="always excludes slow tests"):
        run_fast_tests.build_pytest_args(override)
