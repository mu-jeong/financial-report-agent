from __future__ import annotations

import json

import pytest

from src.retrieval.manifest import (
    CorpusManifest,
    ExclusionPolicy,
    ManifestDecision,
    ManifestError,
)


REPORT_A = '01' * 32
REPORT_B = '80' * 32
REPORT_C = 'ff' * 32


def _policy() -> ExclusionPolicy:
    return ExclusionPolicy(
        version='source-exclusions-v1',
        excluded_reason_codes=frozenset(
            {'known_extraction_failure', 'source_removed'}
        ),
    )


def _build(discovered, decisions) -> CorpusManifest:
    return CorpusManifest.build(
        discovered_report_uids=discovered,
        decisions=decisions,
        exclusion_policy=_policy(),
    )


def test_manifest_json_and_hash_are_stable_across_input_order():
    first = _build(
        [REPORT_C, REPORT_A, REPORT_B],
        [
            ManifestDecision.excluded(REPORT_C, 'source_removed'),
            ManifestDecision.included(REPORT_B),
            ManifestDecision.included(REPORT_A),
        ],
    )
    second = _build(
        [REPORT_B, REPORT_C, REPORT_A],
        [
            ManifestDecision.included(REPORT_A),
            ManifestDecision.excluded(REPORT_C, 'source_removed'),
            ManifestDecision.included(REPORT_B),
        ],
    )

    assert first.canonical_json == second.canonical_json
    assert first.sha256 == second.sha256
    assert [entry.report_uid for entry in first.entries] == [
        REPORT_A,
        REPORT_B,
        REPORT_C,
    ]
    assert json.loads(first.canonical_json) == first.to_dict()


def test_manifest_counts_partition_the_complete_discovered_set():
    manifest = _build(
        [REPORT_A, REPORT_B, REPORT_C],
        [
            ManifestDecision.included(REPORT_A),
            ManifestDecision.excluded(REPORT_B, 'known_extraction_failure'),
            ManifestDecision.excluded(REPORT_C, 'source_removed'),
        ],
    )

    assert manifest.discovered_count == 3
    assert manifest.included_count == 1
    assert manifest.excluded_count == 2
    assert (
        manifest.included_count + manifest.excluded_count
        == manifest.discovered_count
    )
    assert manifest.to_dict()['counts'] == {
        'discovered': 3,
        'excluded': 2,
        'included': 1,
    }


@pytest.mark.parametrize(
    ('discovered', 'decisions', 'message'),
    [
        (
            [REPORT_A, REPORT_B],
            [ManifestDecision.included(REPORT_A)],
            'missing decision',
        ),
        (
            [REPORT_A],
            [ManifestDecision.included(REPORT_A), ManifestDecision.included(REPORT_B)],
            'undiscovered',
        ),
        (
            [REPORT_A, REPORT_A],
            [ManifestDecision.included(REPORT_A)],
            'duplicate discovered',
        ),
        (
            [REPORT_A],
            [ManifestDecision.included(REPORT_A), ManifestDecision.included(REPORT_A)],
            'duplicate decision',
        ),
    ],
)
def test_manifest_rejects_incomplete_or_duplicate_source_accounting(
    discovered, decisions, message
):
    with pytest.raises(ManifestError, match=message):
        _build(discovered, decisions)


def test_manifest_accepts_only_terminal_explained_decisions():
    with pytest.raises(ManifestError, match='status'):
        ManifestDecision(REPORT_A, 'pending', 'pending')
    with pytest.raises(ManifestError, match='included reason'):
        ManifestDecision(REPORT_A, 'included', 'unknown')
    with pytest.raises(ManifestError, match='versioned exclusion policy'):
        _build(
            [REPORT_A],
            [ManifestDecision.excluded(REPORT_A, 'unplanned_failure')],
        )


@pytest.mark.parametrize('reason_code', ['pending', 'unknown', 'unexplained', 'tbd'])
def test_exclusion_policy_rejects_unexplained_placeholder_reasons(reason_code):
    with pytest.raises(ManifestError, match='reserved'):
        ExclusionPolicy(
            version='source-exclusions-v1',
            excluded_reason_codes=frozenset({reason_code}),
        )


def test_manifest_validates_included_reports_reach_positive_snapshot_membership():
    manifest = _build(
        [REPORT_A, REPORT_B],
        [
            ManifestDecision.included(REPORT_A),
            ManifestDecision.excluded(REPORT_B, 'known_extraction_failure'),
        ],
    )

    manifest.validate_snapshot_membership({REPORT_A: 2})
    manifest.validate_snapshot_membership({REPORT_A: 2, REPORT_B: 0})

    with pytest.raises(ManifestError, match='zero snapshot members'):
        manifest.validate_snapshot_membership({REPORT_A: 0})
    with pytest.raises(ManifestError, match='zero snapshot members'):
        manifest.validate_snapshot_membership({})
    with pytest.raises(ManifestError, match='excluded report'):
        manifest.validate_snapshot_membership({REPORT_A: 1, REPORT_B: 1})
    with pytest.raises(ManifestError, match='not present in manifest'):
        manifest.validate_snapshot_membership({REPORT_A: 1, REPORT_C: 1})


@pytest.mark.parametrize('count', [-1, True, 1.5])
def test_manifest_membership_counts_must_be_non_negative_integers(count):
    manifest = _build([REPORT_A], [ManifestDecision.included(REPORT_A)])

    with pytest.raises(ManifestError, match='non-negative integer'):
        manifest.validate_snapshot_membership({REPORT_A: count})


def test_manifest_rejects_malformed_or_case_duplicate_report_uids():
    with pytest.raises(ManifestError, match='SHA-256'):
        _build(['not-a-digest'], [])
    with pytest.raises(ManifestError, match='duplicate discovered'):
        _build(
            [REPORT_A, REPORT_A.upper()],
            [ManifestDecision.included(REPORT_A)],
        )
