from __future__ import annotations

import pytest

from src.retrieval.identity import (
    EmbeddingProfile,
    MAX_FAISS_ID,
    IdentityError,
    assign_physical_ids,
    canonical_hash,
    canonical_json,
    compute_chunk_uid,
    compute_parent_uid,
    compute_profile_hash,
    compute_report_uid,
    normalize_relative_path,
    validate_physical_id_count,
)


def _profile(**changes):
    base = {
        'model': 'baai/bge-m3',
        'dimension': 1024,
        'metric': 'l2',
        'normalization': 'none',
        'prefix_template': '[Company: {target_name}, Title: {title}]\n',
        'extractor': 'pymupdf',
        'parent_policy': {'size': 2000, 'overlap': 200},
        'child_policy': {'size': 500, 'overlap': 50},
    }
    return {**base, **changes}


def test_length_delimited_hashing_prevents_field_boundary_collisions():
    assert canonical_hash('test', 'ab', 'c') != canonical_hash('test', 'a', 'bc')


def test_canonical_json_and_profile_hash_ignore_mapping_insertion_order():
    first = _profile()
    second = dict(reversed(list(first.items())))

    assert canonical_json(first) == canonical_json(second)
    assert compute_profile_hash(first) == compute_profile_hash(second)


@pytest.mark.parametrize(
    'changed',
    [
        {'model': 'baai/bge-m3-v2'},
        {'dimension': 1025},
        {'metric': 'inner_product'},
        {'normalization': 'l2'},
        {'prefix_template': '[Target: {target_name}]\n'},
        {'extractor': 'marker'},
        {'child_policy': {'size': 501, 'overlap': 50}},
    ],
)
def test_profile_hash_changes_when_retrieval_semantics_change(changed):
    base = {
        'model': 'baai/bge-m3',
        'dimension': 1024,
        'metric': 'l2',
        'normalization': 'none',
        'prefix_template': '[Company: {target_name}, Title: {title}]\n',
        'extractor': 'pymupdf',
        'parent_policy': {'size': 2000, 'overlap': 200},
        'child_policy': {'size': 500, 'overlap': 50},
    }
    candidate = {**base, **changed}

    assert compute_profile_hash(base) != compute_profile_hash(candidate)


def test_logical_ids_are_deterministic_and_composed_from_canonical_fields():
    profile_hash = compute_profile_hash(_profile(model='m', dimension=3))
    report_uid = compute_report_uid('reports/a.pdf', '01' * 32, '02' * 32)
    parent_uid = compute_parent_uid(profile_hash, report_uid, 0, '03' * 32)
    chunk_uid = compute_chunk_uid(profile_hash, parent_uid, 0, 10, 20, '04' * 32)

    assert report_uid == compute_report_uid('reports\\a.pdf', '01' * 32, '02' * 32)
    assert parent_uid == compute_parent_uid(profile_hash, report_uid, 0, '03' * 32)
    assert chunk_uid == compute_chunk_uid(profile_hash, parent_uid, 0, 10, 20, '04' * 32)
    assert len({profile_hash, report_uid, parent_uid, chunk_uid}) == 4


@pytest.mark.parametrize(
    'path',
    [
        '',
        '/absolute/report.pdf',
        'C:\\data\\report.pdf',
        '..\\report.pdf',
        'reports/../../report.pdf',
        '\\\\server\\share\\report.pdf',
    ],
)
def test_relative_path_normalization_rejects_unsafe_paths(path):
    with pytest.raises(IdentityError):
        normalize_relative_path(path)


def test_relative_path_normalization_is_platform_independent():
    assert normalize_relative_path('reports\\2026\\a.pdf') == 'reports/2026/a.pdf'
    assert normalize_relative_path('reports/./2026/a.pdf') == 'reports/2026/a.pdf'


def test_physical_ids_are_positive_dense_and_lexicographic():
    chunk_uids = ['ff' * 32, '01' * 32, '80' * 32]

    assigned = assign_physical_ids(chunk_uids)

    assert assigned == {'01' * 32: 1, '80' * 32: 2, 'ff' * 32: 3}


def test_physical_id_assignment_rejects_duplicate_logical_ids():
    with pytest.raises(IdentityError, match='duplicate'):
        assign_physical_ids(['01' * 32, '01' * 32])


def test_physical_id_count_rejects_signed_int64_overflow():
    validate_physical_id_count(MAX_FAISS_ID)
    with pytest.raises(IdentityError, match='signed int64'):
        validate_physical_id_count(MAX_FAISS_ID + 1)


def test_chunk_identity_rejects_empty_or_inverted_spans():
    with pytest.raises(IdentityError, match='span'):
        compute_chunk_uid('01' * 32, '02' * 32, 0, 4, 4, '03' * 32)


def test_profile_requires_every_retrieval_affecting_field():
    for required in _profile():
        incomplete = _profile()
        incomplete.pop(required)
        with pytest.raises(IdentityError, match='missing required fields'):
            compute_profile_hash(incomplete)


def test_profile_policies_are_frozen_and_hash_stable_after_input_mutation():
    raw = _profile(parent_policy={'size': 2000, 'separators': ['\n', ' ']})
    profile = EmbeddingProfile.from_mapping(raw)
    observed_hash = profile.profile_hash

    raw['parent_policy']['size'] = 1
    raw['parent_policy']['separators'].append('')

    assert profile.profile_hash == observed_hash
    with pytest.raises(TypeError):
        profile.parent_policy['size'] = 1
