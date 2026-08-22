'''Canonical whole-corpus source manifests for V2 retrieval builds.'''

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from src.retrieval.identity import canonical_json, sha256_text


MANIFEST_SCHEMA_VERSION = 1
INCLUDED_REASON_CODE = 'included'

_SHA256_RE = re.compile(r'^[0-9a-fA-F]{64}$')
_TOKEN_RE = re.compile(r'^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$')
_RESERVED_REASON_CODES = frozenset(
    {'none', 'pending', 'tbd', 'unknown', 'unexplained', 'unspecified'}
)

ManifestStatus = Literal['included', 'excluded']


class ManifestError(ValueError):
    '''Raised when a source manifest is incomplete or ambiguous.'''


@dataclass(frozen=True)
class ExclusionPolicy:
    '''A versioned allow-list of stable full-corpus exclusion reasons.'''

    version: str
    excluded_reason_codes: frozenset[str]

    def __post_init__(self) -> None:
        version = _validate_token(self.version, 'exclusion policy version')
        try:
            reasons = frozenset(self.excluded_reason_codes)
        except TypeError as exc:
            raise ManifestError(
                'excluded reason codes must be an iterable of strings'
            ) from exc
        if isinstance(self.excluded_reason_codes, (str, bytes)):
            raise ManifestError('excluded reason codes must be an iterable of strings')

        normalized: set[str] = set()
        for reason in reasons:
            reason = _validate_token(reason, 'exclusion reason code')
            if reason == INCLUDED_REASON_CODE:
                raise ManifestError('included is not an exclusion reason code')
            if reason in _RESERVED_REASON_CODES:
                raise ManifestError(f'exclusion reason code is reserved: {reason}')
            normalized.add(reason)

        object.__setattr__(self, 'version', version)
        object.__setattr__(self, 'excluded_reason_codes', frozenset(normalized))

    def to_dict(self) -> dict[str, Any]:
        return {
            'reason_codes': sorted(self.excluded_reason_codes),
            'version': self.version,
        }


@dataclass(frozen=True)
class ManifestDecision:
    '''One terminal and explained decision for one discovered report.'''

    report_uid: str
    status: ManifestStatus
    reason_code: str

    def __post_init__(self) -> None:
        report_uid = _normalize_report_uid(self.report_uid)
        if self.status not in ('included', 'excluded'):
            raise ManifestError('manifest status must be included or excluded')
        reason_code = _validate_token(self.reason_code, 'manifest reason code')
        if self.status == 'included' and reason_code != INCLUDED_REASON_CODE:
            raise ManifestError('included reason must be included')
        if self.status == 'excluded' and reason_code == INCLUDED_REASON_CODE:
            raise ManifestError('excluded report cannot use the included reason')

        object.__setattr__(self, 'report_uid', report_uid)
        object.__setattr__(self, 'reason_code', reason_code)

    @classmethod
    def included(cls, report_uid: str) -> ManifestDecision:
        return cls(report_uid, 'included', INCLUDED_REASON_CODE)

    @classmethod
    def excluded(cls, report_uid: str, reason_code: str) -> ManifestDecision:
        return cls(report_uid, 'excluded', reason_code)

    def to_dict(self) -> dict[str, str]:
        return {
            'reason_code': self.reason_code,
            'report_uid': self.report_uid,
            'status': self.status,
        }


@dataclass(frozen=True)
class CorpusManifest:
    '''An ordered, complete partition of the discovered source corpus.'''

    entries: tuple[ManifestDecision, ...]
    exclusion_policy: ExclusionPolicy
    schema_version: int = MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise ManifestError(
                f'unsupported manifest schema version: {self.schema_version}'
            )
        if not isinstance(self.exclusion_policy, ExclusionPolicy):
            raise ManifestError('manifest requires a versioned exclusion policy')

        entries = tuple(self.entries)
        if any(not isinstance(entry, ManifestDecision) for entry in entries):
            raise ManifestError('manifest entries must be ManifestDecision values')
        report_uids = [entry.report_uid for entry in entries]
        if len(set(report_uids)) != len(report_uids):
            raise ManifestError('duplicate decision for discovered report_uid')

        for entry in entries:
            if (
                entry.status == 'excluded'
                and entry.reason_code
                not in self.exclusion_policy.excluded_reason_codes
            ):
                raise ManifestError(
                    'excluded reason is not present in the versioned exclusion policy: '
                    f'{entry.reason_code}'
                )

        ordered = tuple(
            sorted(entries, key=lambda entry: bytes.fromhex(entry.report_uid))
        )
        object.__setattr__(self, 'entries', ordered)

    @classmethod
    def build(
        cls,
        discovered_report_uids: Iterable[str],
        decisions: Iterable[ManifestDecision],
        exclusion_policy: ExclusionPolicy,
    ) -> CorpusManifest:
        discovered = [_normalize_report_uid(uid) for uid in discovered_report_uids]
        if len(set(discovered)) != len(discovered):
            raise ManifestError('duplicate discovered report_uid')

        decision_list = list(decisions)
        if any(not isinstance(entry, ManifestDecision) for entry in decision_list):
            raise ManifestError('manifest decisions must be ManifestDecision values')
        decision_uids = [entry.report_uid for entry in decision_list]
        if len(set(decision_uids)) != len(decision_uids):
            raise ManifestError('duplicate decision for discovered report_uid')

        discovered_set = set(discovered)
        decision_set = set(decision_uids)
        extra = decision_set - discovered_set
        if extra:
            raise ManifestError(
                'decision references an undiscovered report_uid: '
                f'{sorted(extra)[0]}'
            )
        missing = discovered_set - decision_set
        if missing:
            raise ManifestError(
                'missing decision for discovered report_uid: '
                f'{sorted(missing)[0]}'
            )

        return cls(tuple(decision_list), exclusion_policy)

    @property
    def discovered_count(self) -> int:
        return len(self.entries)

    @property
    def included_count(self) -> int:
        return sum(entry.status == 'included' for entry in self.entries)

    @property
    def excluded_count(self) -> int:
        return sum(entry.status == 'excluded' for entry in self.entries)

    @property
    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())

    @property
    def sha256(self) -> str:
        return sha256_text(self.canonical_json)

    def to_dict(self) -> dict[str, Any]:
        return {
            'counts': {
                'discovered': self.discovered_count,
                'excluded': self.excluded_count,
                'included': self.included_count,
            },
            'exclusion_policy': self.exclusion_policy.to_dict(),
            'reports': [entry.to_dict() for entry in self.entries],
            'schema_version': self.schema_version,
        }

    def validate_snapshot_membership(
        self,
        report_chunk_counts: Mapping[str, int],
    ) -> None:
        '''Require exact report reachability for a candidate snapshot.

        The caller derives counts by joining snapshot membership through chunks
        and parents to reports. Included reports must have at least one member;
        excluded reports must have none.
        '''
        if not isinstance(report_chunk_counts, Mapping):
            raise ManifestError('snapshot membership counts must be a mapping')

        normalized_counts: dict[str, int] = {}
        for raw_uid, count in report_chunk_counts.items():
            report_uid = _normalize_report_uid(raw_uid)
            if report_uid in normalized_counts:
                raise ManifestError(
                    'duplicate report_uid in snapshot membership counts'
                )
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ManifestError(
                    'snapshot membership count must be a non-negative integer'
                )
            normalized_counts[report_uid] = count

        manifest_by_uid = {entry.report_uid: entry for entry in self.entries}
        unknown = set(normalized_counts) - set(manifest_by_uid)
        if unknown:
            raise ManifestError(
                'snapshot membership report_uid is not present in manifest: '
                f'{sorted(unknown)[0]}'
            )

        for entry in self.entries:
            count = normalized_counts.get(entry.report_uid, 0)
            if entry.status == 'included' and count == 0:
                raise ManifestError(
                    'included report has zero snapshot members: '
                    f'{entry.report_uid}'
                )
            if entry.status == 'excluded' and count != 0:
                raise ManifestError(
                    'excluded report has snapshot members: '
                    f'{entry.report_uid}'
                )


def _normalize_report_uid(value: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ManifestError('report_uid must be a 64-character SHA-256 hex digest')
    return value.lower()


def _validate_token(value: str, name: str) -> str:
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        raise ManifestError(
            f'{name} must be a stable lowercase token using letters, digits, ., _, or -'
        )
    return value


__all__ = [
    'CorpusManifest',
    'ExclusionPolicy',
    'INCLUDED_REASON_CODE',
    'MANIFEST_SCHEMA_VERSION',
    'ManifestDecision',
    'ManifestError',
]
