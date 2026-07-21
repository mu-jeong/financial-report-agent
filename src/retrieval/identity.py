'''Deterministic logical and physical identity contracts for V2 retrieval.'''

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


IDENTITY_VERSION = 'finance-llm-retrieval-v2'
MAX_FAISS_ID = (1 << 63) - 1
_SHA256_RE = re.compile(r'^[0-9a-fA-F]{64}$')
_WINDOWS_DRIVE_RE = re.compile(r'^[A-Za-z]:')
_PROFILE_FIELDS = frozenset(
    {
        'model',
        'dimension',
        'metric',
        'normalization',
        'prefix_template',
        'extractor',
        'parent_policy',
        'child_policy',
    }
)


class IdentityError(ValueError):
    '''Raised when a canonical identity cannot be constructed safely.'''


@dataclass(frozen=True)
class EmbeddingProfile:
    '''Complete immutable fingerprint input for one retrieval semantic space.'''

    model: str
    dimension: int
    metric: str
    normalization: str
    prefix_template: str
    extractor: str
    parent_policy: Mapping[str, Any]
    child_policy: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in ('model', 'normalization', 'prefix_template', 'extractor'):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise IdentityError(f'embedding profile {name} must be a non-empty string')
        if (
            not isinstance(self.dimension, int)
            or isinstance(self.dimension, bool)
            or self.dimension <= 0
        ):
            raise IdentityError('embedding profile dimension must be a positive integer')
        if self.metric not in {'l2', 'inner_product'}:
            raise IdentityError('embedding profile metric must be l2 or inner_product')
        if self.normalization not in {'none', 'l2'}:
            raise IdentityError('embedding profile normalization must be none or l2')
        for name in ('parent_policy', 'child_policy'):
            policy = getattr(self, name)
            if not isinstance(policy, Mapping) or not policy:
                raise IdentityError(f'embedding profile {name} must be a non-empty mapping')
            frozen = _freeze_json_value(dict(policy))
            object.__setattr__(self, name, frozen)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> 'EmbeddingProfile':
        if not isinstance(value, Mapping):
            raise IdentityError('embedding profile must be a mapping')
        keys = set(value)
        missing = _PROFILE_FIELDS - keys
        extra = keys - _PROFILE_FIELDS
        if missing:
            raise IdentityError(
                f'embedding profile is missing required fields: {", ".join(sorted(missing))}'
            )
        if extra:
            raise IdentityError(
                f'embedding profile contains unsupported fields: {", ".join(sorted(extra))}'
            )
        return cls(**{name: value[name] for name in _PROFILE_FIELDS})

    def to_dict(self) -> dict[str, Any]:
        return {
            'model': self.model,
            'dimension': self.dimension,
            'metric': self.metric,
            'normalization': self.normalization,
            'prefix_template': self.prefix_template,
            'extractor': self.extractor,
            'parent_policy': _thaw_json_value(self.parent_policy),
            'child_policy': _thaw_json_value(self.child_policy),
        }

    @property
    def profile_hash(self) -> str:
        return canonical_hash('embedding-profile', self.to_dict())


def canonical_json(value: Any) -> str:
    '''Return stable UTF-8 JSON text and reject non-finite numbers.'''
    _reject_non_finite(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(',', ':'),
        )
    except (TypeError, ValueError) as exc:
        raise IdentityError(f'value is not canonical JSON: {exc}') from exc


def canonical_hash(namespace: str, *fields: Any) -> str:
    '''Hash versioned, type-tagged, length-delimited fields with SHA-256.'''
    if not isinstance(namespace, str) or not namespace:
        raise IdentityError('namespace must be a non-empty string')

    digest = hashlib.sha256()
    for field in (IDENTITY_VERSION, namespace, *fields):
        encoded = _encode_field(field)
        digest.update(len(encoded).to_bytes(8, byteorder='big', signed=False))
        digest.update(encoded)
    return digest.hexdigest()


def normalize_relative_path(path: str) -> str:
    '''Normalize an install-relative path while rejecting escape/absolute forms.'''
    if not isinstance(path, str) or not path:
        raise IdentityError('relative path must be a non-empty string')
    if '\x00' in path:
        raise IdentityError('relative path cannot contain NUL')

    normalized = unicodedata.normalize('NFC', path).replace('\\', '/')
    if normalized.startswith('/') or _WINDOWS_DRIVE_RE.match(normalized):
        raise IdentityError('absolute paths are forbidden')

    parts: list[str] = []
    for part in normalized.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            raise IdentityError('relative path traversal is forbidden')
        parts.append(part)

    if not parts:
        raise IdentityError('relative path must name an object')
    return '/'.join(parts)


def compute_profile_hash(profile: EmbeddingProfile | Mapping[str, Any]) -> str:
    normalized = (
        profile if isinstance(profile, EmbeddingProfile) else EmbeddingProfile.from_mapping(profile)
    )
    return normalized.profile_hash


def compute_report_uid(
    canonical_relative_path: str,
    source_sha256: str,
    retrieval_metadata_sha256: str,
) -> str:
    return canonical_hash(
        'report',
        normalize_relative_path(canonical_relative_path),
        _normalize_sha256(source_sha256, 'source_sha256'),
        _normalize_sha256(retrieval_metadata_sha256, 'retrieval_metadata_sha256'),
    )


def compute_parent_uid(
    profile_hash: str,
    report_uid: str,
    parent_order: int,
    content_sha256: str,
) -> str:
    if not isinstance(parent_order, int) or isinstance(parent_order, bool) or parent_order < 0:
        raise IdentityError('parent_order must be a non-negative integer')
    return canonical_hash(
        'parent',
        _normalize_sha256(profile_hash, 'profile_hash'),
        _normalize_sha256(report_uid, 'report_uid'),
        parent_order,
        _normalize_sha256(content_sha256, 'content_sha256'),
    )


def compute_chunk_uid(
    profile_hash: str,
    parent_uid: str,
    child_order: int,
    span_start: int,
    span_end: int,
    embedding_text_sha256: str,
) -> str:
    if not isinstance(child_order, int) or isinstance(child_order, bool) or child_order < 0:
        raise IdentityError('child_order must be a non-negative integer')
    if (
        not isinstance(span_start, int)
        or isinstance(span_start, bool)
        or not isinstance(span_end, int)
        or isinstance(span_end, bool)
        or span_start < 0
        or span_end <= span_start
    ):
        raise IdentityError('chunk span must satisfy 0 <= start < end')
    return canonical_hash(
        'chunk',
        _normalize_sha256(profile_hash, 'profile_hash'),
        _normalize_sha256(parent_uid, 'parent_uid'),
        child_order,
        span_start,
        span_end,
        _normalize_sha256(embedding_text_sha256, 'embedding_text_sha256'),
    )


def validate_physical_id_count(count: int) -> None:
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise IdentityError('physical ID count must be a non-negative integer')
    if count > MAX_FAISS_ID:
        raise IdentityError('physical ID count exceeds positive signed int64 capacity')


def assign_physical_ids(chunk_uids: Iterable[str]) -> dict[str, int]:
    '''Assign snapshot-local IDs 1..N by canonical chunk UID byte ordering.'''
    normalized = [_normalize_sha256(uid, 'chunk_uid') for uid in chunk_uids]
    validate_physical_id_count(len(normalized))
    if len(set(normalized)) != len(normalized):
        raise IdentityError('duplicate chunk_uid in snapshot')
    ordered = sorted(normalized, key=bytes.fromhex)
    return {chunk_uid: position for position, chunk_uid in enumerate(ordered, start=1)}


def sha256_bytes(value: bytes) -> str:
    if not isinstance(value, bytes):
        raise IdentityError('sha256_bytes expects bytes')
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    if not isinstance(value, str):
        raise IdentityError('sha256_text expects str')
    return sha256_bytes(value.encode('utf-8'))


def render_embedding_prefix(template: str, metadata: Mapping[str, Any]) -> str:
    '''Render the configured embedding prefix without silent defaults.'''
    if not isinstance(template, str) or not template:
        raise IdentityError('embedding prefix template must be non-empty')
    if not isinstance(metadata, Mapping):
        raise IdentityError('embedding metadata must be a mapping')
    try:
        prefix = template.format_map(dict(metadata))
    except (KeyError, TypeError, ValueError) as exc:
        raise IdentityError(f'embedding prefix cannot be rendered: {exc}') from exc
    if not isinstance(prefix, str) or not prefix:
        raise IdentityError('rendered embedding prefix must be non-empty')
    return prefix


def _normalize_sha256(value: str, name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise IdentityError(f'{name} must be a 64-character SHA-256 hex digest')
    return value.lower()


def _encode_field(value: Any) -> bytes:
    if value is None:
        return b'n'
    if isinstance(value, bool):
        return b'b1' if value else b'b0'
    if isinstance(value, int):
        return b'i' + str(value).encode('ascii')
    if isinstance(value, str):
        return b's' + unicodedata.normalize('NFC', value).encode('utf-8')
    if isinstance(value, bytes):
        return b'y' + value
    if isinstance(value, (Mapping, Sequence)) and not isinstance(value, (str, bytes, bytearray)):
        return b'j' + canonical_json(value).encode('utf-8')
    raise IdentityError(f'unsupported canonical field type: {type(value).__name__}')


def _reject_non_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise IdentityError('canonical JSON cannot contain non-finite numbers')
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise IdentityError('canonical JSON object keys must be strings')
            _reject_non_finite(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _reject_non_finite(item)


def _freeze_json_value(value: Any) -> Any:
    _reject_non_finite(value)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise IdentityError('embedding policy keys must be strings')
        return MappingProxyType(
            {
                key: _freeze_json_value(item)
                for key, item in sorted(value.items(), key=lambda pair: pair[0])
            }
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_json_value(item) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise IdentityError(f'unsupported embedding policy value: {type(value).__name__}')


def _thaw_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value
