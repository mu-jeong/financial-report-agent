'''Read helpers for one request-consistent base plus delta overlay.'''

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from src.retrieval.delta_schema import delta_schema_installed
from src.retrieval.vector_index import SnapshotDescriptor


@dataclass(frozen=True, slots=True)
class DeltaSegmentRecord:
    segment_id: str
    sequence: int
    relative_path: str | None
    descriptor: SnapshotDescriptor


@dataclass(frozen=True, slots=True)
class DeltaReportHead:
    canonical_relative_path: str
    action: str
    report_uid: str | None
    segment_id: str
    sequence: int


@dataclass(frozen=True, slots=True)
class ActiveDeltaOverlay:
    generation: int
    segments: tuple[DeltaSegmentRecord, ...]
    heads: tuple[DeltaReportHead, ...]

    @property
    def head_by_path(self) -> dict[str, DeltaReportHead]:
        return {head.canonical_relative_path: head for head in self.heads}

    @property
    def segment_by_id(self) -> dict[str, DeltaSegmentRecord]:
        return {segment.segment_id: segment for segment in self.segments}


EMPTY_DELTA_OVERLAY = ActiveDeltaOverlay(0, (), ())


def read_active_delta_overlay(
    connection: sqlite3.Connection,
    *,
    base_snapshot_id: str,
    base_publication_generation: int,
) -> ActiveDeltaOverlay:
    '''Pin the ready delta chain associated with one exact base revision.'''

    if not delta_schema_installed(connection):
        return EMPTY_DELTA_OVERLAY
    segment_rows = connection.execute(
        '''
        SELECT segment_id, sequence, relative_path, file_sha256, size_bytes,
               dimension, metric, ntotal
        FROM retrieval_delta_segments
        WHERE base_snapshot_id = ?
          AND base_publication_generation = ?
          AND state = 'ready'
        ORDER BY sequence, segment_id
        ''',
        (base_snapshot_id, base_publication_generation),
    ).fetchall()
    segments = tuple(
        DeltaSegmentRecord(
            segment_id=str(row[0]),
            sequence=int(row[1]),
            relative_path=None if row[2] is None else str(row[2]),
            descriptor=SnapshotDescriptor(
                sha256='' if row[3] is None else str(row[3]),
                size_bytes=int(row[4]),
                dimension=int(row[5]),
                metric=str(row[6]),
                ntotal=int(row[7]),
            ),
        )
        for row in segment_rows
    )
    if not segments:
        return EMPTY_DELTA_OVERLAY
    head_rows = connection.execute(
        '''
        WITH ranked AS (
            SELECT action.canonical_relative_path, action.action,
                   action.report_uid, action.segment_id, segment.sequence,
                   row_number() OVER (
                       PARTITION BY action.canonical_relative_path
                       ORDER BY segment.sequence DESC, segment.segment_id DESC
                   ) AS position
            FROM retrieval_delta_reports AS action
            JOIN retrieval_delta_segments AS segment
              ON segment.segment_id = action.segment_id
            WHERE segment.base_snapshot_id = ?
              AND segment.base_publication_generation = ?
              AND segment.state = 'ready'
              AND action.action IN ('upsert', 'delete')
        )
        SELECT canonical_relative_path, action, report_uid, segment_id, sequence
        FROM ranked
        WHERE position = 1
        ORDER BY canonical_relative_path
        ''',
        (base_snapshot_id, base_publication_generation),
    ).fetchall()
    heads = tuple(
        DeltaReportHead(
            canonical_relative_path=str(row[0]),
            action=str(row[1]),
            report_uid=None if row[2] is None else str(row[2]),
            segment_id=str(row[3]),
            sequence=int(row[4]),
        )
        for row in head_rows
    )
    segment_ids = {segment.segment_id for segment in segments}
    if any(head.segment_id not in segment_ids for head in heads):
        raise ValueError('delta head references a segment outside the pinned overlay')
    return ActiveDeltaOverlay(
        generation=max(segment.sequence for segment in segments),
        segments=segments,
        heads=heads,
    )


__all__ = [
    'ActiveDeltaOverlay',
    'DeltaReportHead',
    'DeltaSegmentRecord',
    'EMPTY_DELTA_OVERLAY',
    'read_active_delta_overlay',
]
