"""One-click, fail-safe V1 to V2 migration for normal local installations.

The live V1 database, PDFs, and FAISS files are never replaced.  A copied V1
install is converted under an off-path staging root, validated, checked against
the configured embedding provider, and smoke-tested.  Only the completed
``retrieval`` directory is then atomically renamed into the live data root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


from src.migrations.v2.assess import ProvenanceEvidence, assess_v1_install
from src.migrations.v2.evidence import (
    CopiedInstallEvidence,
    create_copied_v1_install,
    seal_compatibility_bundle,
)
from src.migrations.v2.import_v1 import convert_v1_seed, validate_converted_seed
from src.retrieval.bootstrap import RuntimeSelection, inspect_runtime
from src.retrieval.build_service import validate_epoch_zero_same_space_canary
from src.retrieval.identity import EmbeddingProfile, canonical_json
from src.retrieval.publication import PublicationOutcome, activate_epoch_zero_seed
from src.retrieval.update_lock import RetrievalUpdateLock


MIGRATION_SCHEMA_VERSION = 1
V1_PREFIX_TEMPLATE = "[Company: {target_name}, Title: {title}]\n"
CONTROL_DIRECTORY_NAME = ".v2m"
CUTOVER_JOURNAL_NAME = "cutover-journal.json"
ROLLED_BACK_RECEIPT_NAME = "rolled-back-receipt.json"
CUTOVER_PHASES = frozenset(
    {"PREPARED", "ACTIVATED", "VERIFIED", "ROLLED_BACK", "MANUAL_SUPPORT"}
)
CUTOVER_TERMINAL_PHASES = frozenset({"VERIFIED", "ROLLED_BACK"})
RUNTIME_SMOKE_TIMEOUT_SECONDS = 120


class MigrationError(RuntimeError):
    """Raised when migration cannot finish without risking the live V1 data."""


class MigrationProcessCrash(BaseException):
    """Test-only signal that leaves durable state as a hard process exit would."""


class EmbeddingsPort(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


EmbeddingsFactory = Callable[[], EmbeddingsPort]
SmokeCheck = Callable[[Path, str, bool], None]


@dataclass(frozen=True)
class UserMigrationSettings:
    install_root: Path
    data_root: Path
    db_path: Path
    faiss_dir: Path
    source_dir: Path
    model: str
    extractor: str
    parent_chunk_size: int
    child_chunk_size: int
    single_chunk_size: int
    use_parent_child: bool


CutoverHook = Callable[[str], None]


@dataclass(frozen=True)
class MigrationOutcome:
    status: str
    snapshot_id: str
    publication_generation: int
    write_epoch: int
    v1_fallback_open: bool
    write_enabled: bool
    run_root: Path | None = None
    backup_root: Path | None = None
    receipt_path: Path | None = None


def migrate_v1_to_v2(
    settings: UserMigrationSettings,
    *,
    embeddings_factory: EmbeddingsFactory | None = None,
    smoke_check: SmokeCheck | None = None,
    cutover_hook: CutoverHook | None = None,
) -> MigrationOutcome:
    """Convert and activate the V1-backed native seed without mutating V1."""

    normalized = _validated_settings(settings)
    provider_factory = embeddings_factory or _default_embeddings_factory
    smoke = smoke_check or _subprocess_smoke_check(normalized)
    control_root = normalized.data_root.parent / CONTROL_DIRECTORY_NAME
    control_root.mkdir(parents=True, exist_ok=True)
    _require_plain_local_path(control_root, "migration control directory")

    with _MigrationLock(control_root / "migration.lock"):
        recovered = _reconcile_unfinished_cutover(
            normalized,
            control_root,
            smoke,
            cutover_hook=cutover_hook,
        )
        if recovered is not None:
            return recovered
        current = _inspect(normalized.db_path)
        if current.is_native:
            _require_supported_native(current)
            _require_expected_native(
                normalized.db_path,
                current.active_snapshot_id or "",
            )
            smoke(normalized.db_path, current.active_snapshot_id or "", True)
            return _outcome("already_migrated", current)
        if current.mode != "legacy_v1":
            raise MigrationError(f"unsupported retrieval runtime: {current.mode}")

        run_id = _new_run_id()
        run_root = control_root / run_id
        run_root.mkdir(parents=True, exist_ok=False)
        backup_root = run_root / "v1"
        stage_root = run_root / "s"
        stage_root.mkdir()
        receipt_path = run_root / "migration-receipt.json"
        activated = False
        rolled_back = False
        preexisting_empty: Path | None = None

        try:
            print("[1/8] V1 원본의 안전한 백업 복사본을 만드는 중...", flush=True)
            copied = create_copied_v1_install(normalized.data_root, backup_root)
            expected_hashes = {
                artifact.relative_path: artifact.sha256
                for artifact in copied.copied_artifacts
            }
            provenance = ProvenanceEvidence(
                model=normalized.model,
                normalization="none",
                same_space_attested=False,
            )
            assessment = assess_v1_install(
                backup_root,
                expected_hashes=expected_hashes,
                provenance=provenance,
            )

            print("[2/8] V1 PDF 원본을 검증하는 중...", flush=True)
            source_hashes = _source_pdf_hashes(
                backup_root / "reports.db",
                normalized.source_dir,
            )
            source_inventory = _pdf_inventory(normalized.source_dir)
            profile = _embedding_profile(
                normalized,
                assessment.observable.dimension,
                assessment.observable.metric,
            )

            print("[3/8] 격리된 위치에서 V2 호환 seed를 변환하는 중...", flush=True)
            bundle = seal_compatibility_bundle(backup_root, stage_root)
            conversion = convert_v1_seed(
                backup_root,
                stage_root,
                expected_hashes=expected_hashes,
                profile=profile,
                source_hashes=source_hashes,
                compatibility_bundle_id=bundle.bundle_id,
                provenance=provenance,
            )
            validate_converted_seed(stage_root, conversion)

            print("[4/8] 현재 임베딩 모델과 기존 벡터의 호환성을 확인하는 중...", flush=True)
            try:
                provider = provider_factory()
                canary = validate_epoch_zero_same_space_canary(
                    stage_root / "reports.db",
                    data_root=stage_root,
                    embeddings=provider,
                    metric=assessment.observable.metric,
                    normalization="none",
                )
            except Exception as exc:
                raise MigrationError(
                    f"same-space canary failed; V1 was kept unchanged: {type(exc).__name__}: {exc}"
                ) from exc

            print("[5/8] 변환 seed를 쓰기 가능한 V2로 승격하는 중...", flush=True)
            try:
                publication = activate_epoch_zero_seed(
                    stage_root,
                    snapshot_id=conversion.snapshot_id,
                    canary=asdict(canary),
                )
            except Exception as exc:
                raise MigrationError(
                    "writable V2 seed activation failed; V1 was kept unchanged: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            if publication.active_snapshot_id != conversion.snapshot_id:
                raise MigrationError("seed activation selected an unexpected snapshot")

            print("[6/8] 전환 전 읽기·쓰기 실행 테스트를 수행하는 중...", flush=True)
            staged = _require_expected_native(
                stage_root / "reports.db",
                conversion.snapshot_id,
                data_root=stage_root,
            )
            _require_seed_activation(
                staged,
                seed_snapshot_id=conversion.snapshot_id,
                publication=publication,
            )
            smoke(stage_root / "reports.db", conversion.snapshot_id, True)

            owner_marker = {
                "schema_version": MIGRATION_SCHEMA_VERSION,
                "kind": "finance_llm_v2_user_migration_owner",
                "run_id": run_id,
                **_native_identity(staged),
            }
            receipt = {
                "schema_version": MIGRATION_SCHEMA_VERSION,
                "kind": "finance_llm_v2_user_migration_receipt",
                "status": "migrated",
                "run_id": run_id,
                "created_at_utc": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "snapshot_id": conversion.snapshot_id,
                "publication_generation": staged.publication_generation,
                "write_epoch": staged.write_epoch,
                "v1_fallback_open": staged.v1_fallback_open,
                "write_enabled": staged.write_enabled,
                "backup_relative_path": "v1",
                "canary": asdict(canary),
                "profile_assumptions": {
                    "chunk_policy": "current-config-unattested",
                    "extractor": "current-config-unattested",
                },
                "assessment_uncertainties": list(assessment.uncertainties),
                "legacy_report_count": copied.source_report_count,
                "legacy_parent_count": copied.source_parent_count,
                "native_report_count": conversion.report_count,
                "native_chunk_count": conversion.chunk_count,
            }
            live_retrieval = normalized.data_root / "retrieval"
            stage_retrieval = stage_root / "retrieval"
            journal_path = run_root / CUTOVER_JOURNAL_NAME
            journal: dict[str, Any] | None = None

            print("[7/8] 동시 업데이트를 막고 V1 상태를 마지막으로 확인하는 중...", flush=True)
            with RetrievalUpdateLock(normalized.data_root):
                try:
                    recheck_root = run_root / "r"
                    rechecked = create_copied_v1_install(
                        normalized.data_root,
                        recheck_root,
                    )
                    _require_same_v1_state(
                        copied,
                        rechecked,
                        backup_root / "reports.db",
                        recheck_root / "reports.db",
                    )
                    if (
                        _source_pdf_hashes(
                            recheck_root / "reports.db",
                            normalized.source_dir,
                        )
                        != source_hashes
                    ):
                        raise MigrationError(
                            "source PDFs changed during migration; V1 was kept active"
                        )
                    if _pdf_inventory(normalized.source_dir) != source_inventory:
                        raise MigrationError(
                            "source PDF membership or bytes changed during migration; "
                            "V1 was kept active"
                        )
                    if not stage_retrieval.is_dir() or stage_retrieval.is_symlink():
                        raise MigrationError(
                            "validated staged retrieval directory is unavailable"
                        )
                    if stage_retrieval.stat().st_dev != normalized.data_root.stat().st_dev:
                        raise MigrationError(
                            "staged and live data roots are not on the same volume"
                        )
                    prior_retrieval_state = "absent"
                    if live_retrieval.exists():
                        if live_retrieval.is_symlink() or not live_retrieval.is_dir():
                            raise MigrationError("live retrieval path is not a plain directory")
                        try:
                            next(live_retrieval.iterdir())
                        except StopIteration:
                            prior_retrieval_state = "empty"
                            preexisting_empty = run_root / "empty-retrieval"
                        else:
                            raise MigrationError(
                                "live retrieval directory became non-empty during migration"
                            )

                    _write_json_once(
                        stage_retrieval / "migration-owner.json",
                        owner_marker,
                    )
                    journal = {
                        "schema_version": MIGRATION_SCHEMA_VERSION,
                        "kind": "finance_llm_v2_user_cutover_journal",
                        "run_id": run_id,
                        "phase": "PREPARED",
                        "updated_at_utc": datetime.now(timezone.utc).isoformat(
                            timespec="seconds"
                        ),
                        "prior_retrieval_state": prior_retrieval_state,
                        "owner": owner_marker,
                        "expected_identity": _native_identity(staged),
                        "receipt": receipt,
                        "v1_artifact_sha256": {
                            relative_path: digest
                            for relative_path, digest in sorted(expected_hashes.items())
                            if relative_path != "reports.db"
                        },
                        "v1_database_logical_sha256": _sqlite_logical_sha256(
                            backup_root / "reports.db"
                        ),
                        "source_inventory_sha256": dict(sorted(source_inventory.items())),
                    }
                    _write_json_once(journal_path, journal)
                    _invoke_cutover_hook(cutover_hook, "journal_prepared")
                    if preexisting_empty is not None:
                        os.rename(live_retrieval, preexisting_empty)
                        _fsync_directory(normalized.data_root)
                        _fsync_directory(run_root)
                    print("[8/8] 검증된 V2를 활성화하고 실제 실행을 확인하는 중...", flush=True)
                    os.rename(stage_retrieval, live_retrieval)
                    activated = True
                    _fsync_directory(stage_root)
                    _fsync_directory(normalized.data_root)
                    _invoke_cutover_hook(cutover_hook, "retrieval_renamed")
                    journal = _transition_cutover_journal(
                        journal_path,
                        journal,
                        "ACTIVATED",
                    )
                    _invoke_cutover_hook(cutover_hook, "journal_activated")
                    live = _require_expected_native(
                        normalized.db_path,
                        conversion.snapshot_id,
                    )
                    _require_same_native_identity(staged, live)
                    smoke(normalized.db_path, conversion.snapshot_id, False)
                    _write_json_once_or_same(receipt_path, receipt)
                    _invoke_cutover_hook(cutover_hook, "receipt_written")
                    journal = _transition_cutover_journal(
                        journal_path,
                        journal,
                        "VERIFIED",
                    )
                    return _outcome(
                        "migrated",
                        live,
                        run_root=run_root,
                        backup_root=backup_root,
                        receipt_path=receipt_path,
                    )
                except BaseException as exc:
                    if isinstance(exc, MigrationProcessCrash):
                        raise
                    if activated:
                        rollback_target = run_root / "failed-retrieval"
                        try:
                            if live_retrieval.exists():
                                _rollback_owned_retrieval(
                                    live_retrieval,
                                    rollback_target,
                                    owner_marker,
                                    staged,
                                )
                                _invoke_cutover_hook(
                                    cutover_hook,
                                    "rollback_retrieval_moved",
                                )
                            if preexisting_empty is not None and preexisting_empty.exists():
                                os.rename(preexisting_empty, live_retrieval)
                                _fsync_directory(run_root)
                                _fsync_directory(normalized.data_root)
                            restored = _inspect(normalized.db_path)
                            if restored.mode != "legacy_v1":
                                raise MigrationError(
                                    "automatic rollback did not restore V1 selection"
                                )
                            if journal is not None:
                                _quarantine_rolled_back_receipt(run_root, receipt)
                                journal = _transition_cutover_journal(
                                    journal_path,
                                    journal,
                                    "ROLLED_BACK",
                                )
                            rolled_back = True
                        except BaseException as rollback_exc:
                            if isinstance(rollback_exc, MigrationProcessCrash):
                                raise
                            if journal is not None:
                                _transition_cutover_journal_best_effort(
                                    journal_path,
                                    journal,
                                    "MANUAL_SUPPORT",
                                )
                            _write_failure_best_effort(run_root, exc, rollback_exc)
                            raise MigrationError(
                                "V2 activation failed and automatic rollback also failed; "
                                f"preserved run: {run_root}"
                            ) from rollback_exc
                    elif preexisting_empty is not None and preexisting_empty.exists():
                        if not live_retrieval.exists():
                            os.rename(preexisting_empty, live_retrieval)
                            _fsync_directory(run_root)
                            _fsync_directory(normalized.data_root)
                    if journal is not None and journal["phase"] not in CUTOVER_TERMINAL_PHASES:
                        _quarantine_rolled_back_receipt(run_root, receipt)
                        journal = _transition_cutover_journal(
                            journal_path,
                            journal,
                            "ROLLED_BACK",
                        )
                    raise
        except BaseException as exc:
            if isinstance(exc, MigrationProcessCrash):
                raise
            _write_failure_best_effort(run_root, exc)
            if isinstance(exc, KeyboardInterrupt):
                raise
            if rolled_back:
                raise MigrationError(
                    "live V2 smoke test failed and was automatically rolled back to V1: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            if isinstance(exc, MigrationError):
                raise
            raise MigrationError(
                f"migration stopped before activation; V1 was kept unchanged: "
                f"{type(exc).__name__}: {exc}"
            ) from exc


def _reconcile_unfinished_cutover(
    settings: UserMigrationSettings,
    control_root: Path,
    smoke: SmokeCheck,
    *,
    cutover_hook: CutoverHook | None,
) -> MigrationOutcome | None:
    unfinished: list[tuple[Path, dict[str, Any]]] = []
    for run_root in sorted(control_root.iterdir(), key=lambda path: path.name):
        if not run_root.is_dir() or run_root.is_symlink():
            continue
        journal_path = run_root / CUTOVER_JOURNAL_NAME
        if not journal_path.exists():
            continue
        journal = _load_cutover_journal(journal_path)
        if journal["phase"] == "MANUAL_SUPPORT":
            raise MigrationError(
                f"unfinished V2 cutover requires manual support: {run_root}"
            )
        if journal["phase"] not in CUTOVER_TERMINAL_PHASES:
            unfinished.append((journal_path, journal))
    if len(unfinished) > 1:
        raise MigrationError("multiple unfinished V2 cutovers require manual support")
    if not unfinished:
        return None

    journal_path, journal = unfinished[0]
    run_root = journal_path.parent
    live_retrieval = settings.data_root / "retrieval"
    stage_retrieval = run_root / "s" / "retrieval"
    failed_retrieval = run_root / "failed-retrieval"
    prior_empty = run_root / "empty-retrieval"
    owner = journal["owner"]
    expected_identity = journal["expected_identity"]
    receipt = journal["receipt"]
    receipt_path = run_root / "migration-receipt.json"

    with RetrievalUpdateLock(settings.data_root):
        observed_journal = _load_cutover_journal(journal_path)
        if observed_journal != journal:
            raise MigrationError("cutover journal changed before reconciliation")
        journal = observed_journal
        if journal["phase"] in CUTOVER_TERMINAL_PHASES:
            return None
        try:
            live_is_owned = _retrieval_is_owned(live_retrieval, owner)
        except Exception as exc:
            _mark_cutover_manual_support(journal_path, journal)
            raise MigrationError(
                "unfinished cutover ownership changed and requires manual support"
            ) from exc
        try:
            _require_recovery_baseline(settings, journal)
        except Exception as exc:
            _mark_cutover_manual_support(journal_path, journal)
            raise MigrationError(
                "unfinished cutover baseline changed and requires manual support"
            ) from exc
        if live_is_owned:
            try:
                live = _inspect(settings.db_path)
                _require_identity_payload(expected_identity, live)
            except Exception as exc:
                _mark_cutover_manual_support(journal_path, journal)
                raise MigrationError(
                    "unfinished cutover native identity changed and requires "
                    "manual support"
                ) from exc
            if journal["phase"] == "PREPARED":
                journal = _transition_cutover_journal(
                    journal_path,
                    journal,
                    "ACTIVATED",
                )
            try:
                smoke(
                    settings.db_path,
                    str(expected_identity["active_snapshot_id"]),
                    False,
                )
                _write_json_once_or_same(receipt_path, receipt)
                _invoke_cutover_hook(cutover_hook, "receipt_written")
                journal = _transition_cutover_journal(
                    journal_path,
                    journal,
                    "VERIFIED",
                )
                return _outcome(
                    "recovered",
                    live,
                    run_root=run_root,
                    backup_root=run_root / "v1",
                    receipt_path=receipt_path,
                )
            except BaseException as exc:
                if isinstance(exc, MigrationProcessCrash):
                    raise
                try:
                    _rollback_owned_retrieval(
                        live_retrieval,
                        failed_retrieval,
                        owner,
                        live,
                    )
                    _invoke_cutover_hook(cutover_hook, "rollback_retrieval_moved")
                    _restore_prior_empty_retrieval(
                        settings.data_root,
                        run_root,
                        journal,
                    )
                    restored = _inspect(settings.db_path)
                    if restored.mode != "legacy_v1":
                        raise MigrationError(
                            "unfinished cutover rollback did not restore V1"
                        )
                    _quarantine_rolled_back_receipt(run_root, receipt)
                    journal = _transition_cutover_journal(
                        journal_path,
                        journal,
                        "ROLLED_BACK",
                    )
                except BaseException as rollback_exc:
                    if isinstance(rollback_exc, MigrationProcessCrash):
                        raise
                    _transition_cutover_journal_best_effort(
                        journal_path,
                        journal,
                        "MANUAL_SUPPORT",
                    )
                    _write_failure_best_effort(run_root, exc, rollback_exc)
                    raise MigrationError(
                        "unfinished V2 cutover failed smoke validation and could not "
                        f"be rolled back safely: {run_root}"
                    ) from rollback_exc
                _write_failure_best_effort(run_root, exc)
                raise MigrationError(
                    "unfinished V2 cutover failed smoke validation and was rolled "
                    "back to V1"
                ) from exc

        if live_retrieval.exists():
            if live_retrieval.is_symlink() or not live_retrieval.is_dir():
                _mark_cutover_manual_support(journal_path, journal)
                raise MigrationError("unfinished cutover live path is unsafe")
            try:
                next(live_retrieval.iterdir())
            except StopIteration:
                if journal["prior_retrieval_state"] != "empty":
                    _mark_cutover_manual_support(journal_path, journal)
                    raise MigrationError(
                        "unfinished cutover found an unexpected live directory"
                    )
            else:
                _mark_cutover_manual_support(journal_path, journal)
                raise MigrationError(
                    "unfinished cutover live retrieval identity is not owned"
                )
        if failed_retrieval.exists():
            try:
                failed_is_owned = _retrieval_is_owned(failed_retrieval, owner)
            except Exception as exc:
                _mark_cutover_manual_support(journal_path, journal)
                raise MigrationError(
                    "unfinished rollback ownership changed and requires manual support"
                ) from exc
            if not failed_is_owned:
                _mark_cutover_manual_support(journal_path, journal)
                raise MigrationError("unfinished rollback target is not owned")
        elif journal["phase"] == "ACTIVATED":
            _mark_cutover_manual_support(journal_path, journal)
            raise MigrationError("activated V2 cutover artifacts are missing")
        elif not stage_retrieval.is_dir() or stage_retrieval.is_symlink():
            _mark_cutover_manual_support(journal_path, journal)
            raise MigrationError("prepared V2 cutover artifacts are missing")

        try:
            _restore_prior_empty_retrieval(settings.data_root, run_root, journal)
            restored = _inspect(settings.db_path)
            if restored.mode != "legacy_v1":
                raise MigrationError("unfinished cutover cannot prove V1 restoration")
            _quarantine_rolled_back_receipt(run_root, receipt)
        except Exception as exc:
            _mark_cutover_manual_support(journal_path, journal)
            raise MigrationError(
                "unfinished cutover cannot restore V1 and requires manual support"
            ) from exc
        _transition_cutover_journal(journal_path, journal, "ROLLED_BACK")
    return None


def _restore_prior_empty_retrieval(
    data_root: Path,
    run_root: Path,
    journal: dict[str, Any],
) -> None:
    if journal["prior_retrieval_state"] != "empty":
        return
    live_retrieval = data_root / "retrieval"
    prior_empty = run_root / "empty-retrieval"
    if live_retrieval.exists():
        if prior_empty.exists():
            raise MigrationError("both live and preserved empty retrieval paths exist")
        return
    if not prior_empty.is_dir() or prior_empty.is_symlink():
        raise MigrationError("preserved empty retrieval directory is unavailable")
    os.rename(prior_empty, live_retrieval)
    _fsync_directory(run_root)
    _fsync_directory(data_root)


def _require_recovery_baseline(
    settings: UserMigrationSettings,
    journal: dict[str, Any],
) -> None:
    if (
        _sqlite_logical_sha256(settings.db_path)
        != journal["v1_database_logical_sha256"]
    ):
        raise MigrationError("V1 reports database changed after cutover")
    for relative_path, expected_hash in journal["v1_artifact_sha256"].items():
        relative = Path(relative_path)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise MigrationError("cutover journal contains an unsafe V1 artifact path")
        artifact = _plain_file(
            settings.data_root.joinpath(*relative.parts),
            f"V1 recovery artifact {relative_path}",
        )
        if _sha256_file(artifact) != expected_hash:
            raise MigrationError(f"V1 recovery artifact changed: {relative_path}")
    if _pdf_inventory(settings.source_dir) != journal["source_inventory_sha256"]:
        raise MigrationError("source PDF inventory changed after cutover")


def _retrieval_is_owned(path: Path, expected_owner: dict[str, Any]) -> bool:
    if not path.exists():
        return False
    if path.is_symlink() or not path.is_dir():
        raise MigrationError("migration-owned retrieval path is unsafe")
    marker = path / "migration-owner.json"
    if not marker.exists():
        return False
    if marker.is_symlink() or not marker.is_file():
        raise MigrationError("migration owner marker is unsafe")
    try:
        observed = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError("migration owner marker is invalid") from exc
    if observed != expected_owner:
        raise MigrationError("migration owner marker changed")
    return True


def _load_cutover_journal(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise MigrationError("cutover journal is unavailable or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError("cutover journal is invalid") from exc
    if not isinstance(value, dict):
        raise MigrationError("cutover journal must be an object")
    required = {
        "schema_version",
        "kind",
        "run_id",
        "phase",
        "updated_at_utc",
        "prior_retrieval_state",
        "owner",
        "expected_identity",
        "receipt",
        "v1_artifact_sha256",
        "v1_database_logical_sha256",
        "source_inventory_sha256",
    }
    if set(value) != required:
        raise MigrationError("cutover journal fields are invalid")
    if (
        value["schema_version"] != MIGRATION_SCHEMA_VERSION
        or value["kind"] != "finance_llm_v2_user_cutover_journal"
        or value["run_id"] != path.parent.name
        or value["phase"] not in CUTOVER_PHASES
        or value["prior_retrieval_state"] not in {"absent", "empty"}
    ):
        raise MigrationError("cutover journal identity is invalid")
    if not isinstance(value["updated_at_utc"], str) or not value["updated_at_utc"]:
        raise MigrationError("cutover journal timestamp is invalid")
    owner = value["owner"]
    expected_identity = value["expected_identity"]
    receipt = value["receipt"]
    if not isinstance(owner, dict) or not isinstance(expected_identity, dict):
        raise MigrationError("cutover journal native identity is invalid")
    _validate_native_identity_payload(expected_identity)
    if owner != {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "kind": "finance_llm_v2_user_migration_owner",
        "run_id": value["run_id"],
        **expected_identity,
    }:
        raise MigrationError("cutover journal owner identity is inconsistent")
    if (
        not isinstance(receipt, dict)
        or receipt.get("run_id") != value["run_id"]
        or receipt.get("snapshot_id") != expected_identity["active_snapshot_id"]
        or receipt.get("publication_generation")
        != expected_identity["publication_generation"]
        or receipt.get("write_epoch") != expected_identity["write_epoch"]
    ):
        raise MigrationError("cutover journal receipt is inconsistent")
    for field in ("v1_artifact_sha256", "source_inventory_sha256"):
        hashes = value[field]
        if not isinstance(hashes, dict) or not hashes or any(
            not isinstance(name, str)
            or not name
            or not isinstance(digest, str)
            or len(digest) != 64
            for name, digest in hashes.items()
        ):
            raise MigrationError(f"cutover journal {field} is invalid")
    database_hash = value["v1_database_logical_sha256"]
    if not isinstance(database_hash, str) or len(database_hash) != 64:
        raise MigrationError("cutover journal V1 database hash is invalid")
    return value


def _transition_cutover_journal(
    path: Path,
    journal: dict[str, Any],
    phase: str,
) -> dict[str, Any]:
    allowed = {
        "PREPARED": {"ACTIVATED", "ROLLED_BACK", "MANUAL_SUPPORT"},
        "ACTIVATED": {"VERIFIED", "ROLLED_BACK", "MANUAL_SUPPORT"},
        "VERIFIED": set(),
        "ROLLED_BACK": set(),
        "MANUAL_SUPPORT": set(),
    }
    if phase not in allowed.get(str(journal.get("phase")), set()):
        raise MigrationError("invalid cutover journal transition")
    observed = _load_cutover_journal(path)
    if observed != journal:
        raise MigrationError("cutover journal changed concurrently")
    updated = {
        **journal,
        "phase": phase,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _write_json_replace(path, updated)
    return updated


def _transition_cutover_journal_best_effort(
    path: Path,
    journal: dict[str, Any],
    phase: str,
) -> None:
    try:
        _transition_cutover_journal(path, journal, phase)
    except Exception:
        pass


def _mark_cutover_manual_support(path: Path, journal: dict[str, Any]) -> None:
    _transition_cutover_journal_best_effort(
        path,
        journal,
        "MANUAL_SUPPORT",
    )


def _invoke_cutover_hook(hook: CutoverHook | None, phase: str) -> None:
    if hook is not None:
        hook(phase)


def _validated_settings(settings: UserMigrationSettings) -> UserMigrationSettings:
    if not isinstance(settings, UserMigrationSettings):
        raise MigrationError("migration settings are invalid")
    install_root = _plain_directory(settings.install_root, "install root")
    data_root = _plain_directory(settings.data_root, "data root")
    db_path = _plain_file(settings.db_path, "reports database")
    faiss_dir = _plain_directory(settings.faiss_dir, "V1 vector directory")
    source_dir = _plain_directory(settings.source_dir, "source PDF directory")
    if db_path != data_root / "reports.db":
        raise MigrationError("one-click migration requires data/reports.db standard layout")
    if faiss_dir != data_root / "vector_db":
        raise MigrationError("one-click migration requires data/vector_db standard layout")
    if source_dir != data_root / "downloaded":
        raise MigrationError("one-click migration requires data/downloaded standard layout")
    for required in (faiss_dir / "index.faiss", faiss_dir / "index.pkl"):
        _plain_file(required, f"V1 artifact {required.name}")
    if not isinstance(settings.model, str) or not settings.model.strip():
        raise MigrationError("embedding model is missing")
    if not isinstance(settings.extractor, str) or not settings.extractor.strip():
        raise MigrationError("PDF extractor is missing")
    for name, value in (
        ("parent chunk size", settings.parent_chunk_size),
        ("child chunk size", settings.child_chunk_size),
        ("single chunk size", settings.single_chunk_size),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise MigrationError(f"{name} must be a positive integer")
    if settings.child_chunk_size > settings.parent_chunk_size:
        raise MigrationError("child chunks cannot be larger than parent chunks")
    if not isinstance(settings.use_parent_child, bool):
        raise MigrationError("USE_PARENT_CHILD must be boolean")
    if not settings.use_parent_child:
        raise MigrationError(
            "one-click migration currently requires USE_PARENT_CHILD=true V1 data"
        )
    return UserMigrationSettings(
        install_root=install_root,
        data_root=data_root,
        db_path=db_path,
        faiss_dir=faiss_dir,
        source_dir=source_dir,
        model=settings.model.strip(),
        extractor=settings.extractor.strip(),
        parent_chunk_size=settings.parent_chunk_size,
        child_chunk_size=settings.child_chunk_size,
        single_chunk_size=settings.single_chunk_size,
        use_parent_child=settings.use_parent_child,
    )


def _embedding_profile(
    settings: UserMigrationSettings,
    dimension: int,
    metric: str,
) -> EmbeddingProfile:
    parent_policy: dict[str, Any] = {
        "algorithm": "langchain-recursive-v1",
        "chunk_overlap": int(settings.parent_chunk_size * 0.1),
        "chunk_size": settings.parent_chunk_size,
        "headers": ["#", "##", "###"],
        "separators": ["\n\n", "\n", ". ", " ", ""],
        "configuration_source": "current-config-unattested",
    }
    child_policy: dict[str, Any] = {
        "algorithm": "langchain-recursive-v1",
        "chunk_overlap": int(settings.child_chunk_size * 0.1),
        "chunk_size": settings.child_chunk_size,
        "separators": ["\n\n", "\n", ". ", " ", ""],
        "span_source": "splitter_start_index",
        "configuration_source": "current-config-unattested",
    }
    return EmbeddingProfile(
        model=settings.model,
        dimension=dimension,
        metric=metric,
        normalization="none",
        prefix_template=V1_PREFIX_TEMPLATE,
        extractor=f"legacy-v1-import|configured={settings.extractor}|unattested",
        parent_policy=parent_policy,
        child_policy=child_policy,
    )


def _source_pdf_hashes(database: Path, source_dir: Path) -> dict[str, str]:
    uri = f"file:{database.resolve(strict=True).as_posix()}?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            names = [
                row[0]
                for row in connection.execute(
                    "SELECT file_name FROM reports ORDER BY file_name"
                )
            ]
    except sqlite3.Error as exc:
        raise MigrationError(f"V1 report list cannot be read: {exc}") from exc
    if not names:
        raise MigrationError("V1 reports database is empty")
    if any(not isinstance(name, str) or not name for name in names):
        raise MigrationError("V1 contains an invalid source PDF name")
    if len(set(names)) != len(names):
        raise MigrationError("V1 contains duplicate source PDF names")
    result: dict[str, str] = {}
    for name in names:
        if Path(name).name != name or name in {".", ".."}:
            raise MigrationError(f"unsafe source PDF name: {name}")
        path = source_dir / name
        if not path.exists():
            raise MigrationError(f"source PDF is missing: {name}")
        resolved = _plain_file(path, f"source PDF {name}")
        if resolved.parent != source_dir:
            raise MigrationError(f"source PDF escapes data/downloaded: {name}")
        result[name] = _sha256_file(resolved)
    return result


def _pdf_inventory(source_dir: Path) -> dict[str, str]:
    inventory: dict[str, str] = {}
    observed_casefold: set[str] = set()
    for path in sorted(source_dir.iterdir(), key=lambda item: item.name.casefold()):
        if not path.name.lower().endswith(".pdf"):
            continue
        if path.is_symlink() or not path.is_file():
            raise MigrationError(f"source PDF must be a real file: {path.name}")
        folded = path.name.casefold()
        if folded in observed_casefold:
            raise MigrationError(f"duplicate source PDF name: {path.name}")
        observed_casefold.add(folded)
        inventory[f"downloaded/{path.name}"] = _sha256_file(path)
    if not inventory:
        raise MigrationError("source PDF directory is empty")
    return inventory


def _require_same_v1_state(
    before: CopiedInstallEvidence,
    after: CopiedInstallEvidence,
    before_database: Path,
    after_database: Path,
) -> None:
    before_vectors = {
        item.relative_path: (item.size_bytes, item.sha256)
        for item in before.copied_artifacts
        if item.relative_path != "reports.db"
    }
    after_vectors = {
        item.relative_path: (item.size_bytes, item.sha256)
        for item in after.copied_artifacts
        if item.relative_path != "reports.db"
    }
    if before_vectors != after_vectors:
        raise MigrationError("V1 vector artifacts changed during migration")
    if (
        before.source_report_count != after.source_report_count
        or before.source_parent_count != after.source_parent_count
        or _sqlite_logical_sha256(before_database) != _sqlite_logical_sha256(after_database)
    ):
        raise MigrationError("V1 reports database changed during migration")


def _rollback_owned_retrieval(
    live_retrieval: Path,
    rollback_target: Path,
    expected_owner: dict[str, Any],
    expected_selection: RuntimeSelection,
) -> None:
    marker = live_retrieval / "migration-owner.json"
    if marker.is_symlink() or not marker.is_file():
        raise MigrationError("automatic rollback cannot prove migration ownership")
    try:
        observed = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError("automatic rollback owner marker is invalid") from exc
    if observed != expected_owner:
        raise MigrationError("automatic rollback owner marker changed")
    current = _inspect(live_retrieval.parent / "reports.db")
    _require_same_native_identity(expected_selection, current)
    if (live_retrieval / "v2" / "writer.lock").exists():
        raise MigrationError("automatic rollback is blocked by an active native writer")
    if rollback_target.exists():
        raise MigrationError("automatic rollback target already exists")
    os.rename(live_retrieval, rollback_target)
    _fsync_directory(live_retrieval.parent)
    _fsync_directory(rollback_target.parent)


def _sqlite_logical_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    uri = f"file:{path.resolve(strict=True).as_posix()}?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            for line in connection.iterdump():
                digest.update(line.encode("utf-8"))
                digest.update(b"\n")
    except sqlite3.Error as exc:
        raise MigrationError(f"V1 database cannot be rechecked: {exc}") from exc
    return digest.hexdigest()


def _require_expected_native(
    db_path: Path,
    expected_snapshot_id: str,
    *,
    data_root: Path | None = None,
) -> RuntimeSelection:
    selection = _inspect(db_path, data_root=data_root)
    _require_supported_native(selection)
    if selection.active_snapshot_id != expected_snapshot_id:
        raise MigrationError("runtime selected an unexpected V2 snapshot")
    return selection


def _require_supported_native(selection: RuntimeSelection) -> None:
    if not selection.is_native or not selection.active_snapshot_id:
        raise MigrationError("validated native runtime is unavailable")
    healthy_native = (
        selection.write_epoch > 0
        and not selection.v1_fallback_open
        and selection.write_enabled
        and not selection.degraded
    )
    if not healthy_native:
        raise MigrationError(
            "one-click completion requires a healthy writable native runtime; "
            "an epoch-zero or degraded native state is not accepted"
        )


def _require_seed_activation(
    selection: RuntimeSelection,
    *,
    seed_snapshot_id: str,
    publication: PublicationOutcome,
) -> None:
    _require_supported_native(selection)
    expected = (
        seed_snapshot_id,
        None,
        2,
        1,
        False,
        True,
        False,
    )
    observed = (
        selection.active_snapshot_id,
        selection.predecessor_snapshot_id,
        selection.publication_generation,
        selection.write_epoch,
        selection.v1_fallback_open,
        selection.write_enabled,
        selection.degraded,
    )
    publication_identity = (
        publication.active_snapshot_id,
        publication.predecessor_snapshot_id,
        publication.publication_generation,
        publication.write_epoch,
        publication.v1_fallback_open,
    )
    expected_publication = (
        seed_snapshot_id,
        None,
        2,
        1,
        False,
    )
    if (
        observed != expected
        or publication_identity != expected_publication
        or not publication.publication_id
    ):
        raise MigrationError("converted seed activation identity is inconsistent")


def _native_identity(selection: RuntimeSelection) -> dict[str, Any]:
    identity = {
        "active_snapshot_id": selection.active_snapshot_id,
        "active_build_id": selection.active_build_id,
        "predecessor_snapshot_id": selection.predecessor_snapshot_id,
        "publication_generation": selection.publication_generation,
        "write_epoch": selection.write_epoch,
        "v1_fallback_open": selection.v1_fallback_open,
        "write_enabled": selection.write_enabled,
        "degraded": selection.degraded,
    }
    _validate_native_identity_payload(identity)
    return identity


def _validate_native_identity_payload(identity: dict[str, Any]) -> None:
    expected_fields = {
        "active_snapshot_id",
        "active_build_id",
        "predecessor_snapshot_id",
        "publication_generation",
        "write_epoch",
        "v1_fallback_open",
        "write_enabled",
        "degraded",
    }
    if set(identity) != expected_fields:
        raise MigrationError("native identity fields are invalid")
    for field in ("active_snapshot_id", "active_build_id"):
        if not isinstance(identity[field], str) or not identity[field]:
            raise MigrationError("native identity string fields are invalid")
    predecessor = identity["predecessor_snapshot_id"]
    if predecessor is not None and (
        not isinstance(predecessor, str) or not predecessor
    ):
        raise MigrationError("native predecessor identity is invalid")
    for field in ("publication_generation", "write_epoch"):
        value = identity[field]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise MigrationError("native identity counters are invalid")
    for field in ("v1_fallback_open", "write_enabled", "degraded"):
        if not isinstance(identity[field], bool):
            raise MigrationError("native identity flags are invalid")


def _require_identity_payload(
    expected_identity: dict[str, Any],
    observed: RuntimeSelection,
) -> None:
    _require_supported_native(observed)
    _validate_native_identity_payload(expected_identity)
    if _native_identity(observed) != expected_identity:
        raise MigrationError("native runtime identity changed during activation")


def _require_same_native_identity(
    expected: RuntimeSelection,
    observed: RuntimeSelection,
) -> None:
    _require_identity_payload(_native_identity(expected), observed)


def _inspect(db_path: Path, *, data_root: Path | None = None) -> RuntimeSelection:
    try:
        return inspect_runtime(db_path, data_root=data_root, validate_snapshot=True)
    except Exception as exc:
        raise MigrationError(
            "retrieval runtime validation failed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _outcome(
    status: str,
    selection: RuntimeSelection,
    *,
    run_root: Path | None = None,
    backup_root: Path | None = None,
    receipt_path: Path | None = None,
) -> MigrationOutcome:
    return MigrationOutcome(
        status=status,
        snapshot_id=selection.active_snapshot_id or "",
        publication_generation=selection.publication_generation,
        write_epoch=selection.write_epoch,
        v1_fallback_open=selection.v1_fallback_open,
        write_enabled=selection.write_enabled,
        run_root=run_root,
        backup_root=backup_root,
        receipt_path=receipt_path,
    )


def _subprocess_smoke_check(settings: UserMigrationSettings) -> SmokeCheck:
    def run(
        db_path: Path,
        expected_snapshot_id: str,
        require_write: bool,
    ) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "DB_PATH": str(db_path),
                "SAVE_DIR": str(settings.source_dir),
                "FAISS_DIR": str(settings.faiss_dir),
                "REPORT_PDF_DIR": str(settings.source_dir),
                "PYTHONUTF8": "1",
            }
        )
        launcher_command = [sys.executable, "-m", "src.retrieval.launcher_guard"]
        if require_write:
            launcher_command.append("--write")
        commands = (
            launcher_command,
            [sys.executable, "apps/gui/app.py", "--runtime-smoke"],
        )
        for command in commands:
            try:
                completed = subprocess.run(
                    command,
                    cwd=settings.install_root,
                    env=environment,
                    check=False,
                    timeout=RUNTIME_SMOKE_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as exc:
                raise MigrationError(
                    "runtime smoke command timed out after "
                    f"{RUNTIME_SMOKE_TIMEOUT_SECONDS} seconds"
                ) from exc
            if completed.returncode != 0:
                raise MigrationError(
                    f"runtime smoke command failed with exit code {completed.returncode}"
                )
        _require_expected_native(
            db_path,
            expected_snapshot_id,
            data_root=db_path.parent,
        )

    return run


def _default_embeddings_factory() -> EmbeddingsPort:
    from src.llms.embeddings import build_embeddings_model

    return build_embeddings_model()


def _plain_directory(path: Path, label: str) -> Path:
    lexical = Path(path).expanduser().absolute()
    if not lexical.is_dir():
        raise MigrationError(f"{label} is missing: {lexical}")
    _require_plain_local_path(lexical, label)
    return lexical.resolve(strict=True)


def _plain_file(path: Path, label: str) -> Path:
    lexical = Path(path).expanduser().absolute()
    if not lexical.is_file():
        raise MigrationError(f"{label} is missing: {lexical}")
    _require_plain_local_path(lexical, label)
    return lexical.resolve(strict=True)


def _require_plain_local_path(path: Path, label: str) -> None:
    absolute = path.absolute()
    if str(absolute).startswith("\\\\"):
        raise MigrationError(f"{label} must be on a local drive")
    for candidate in (absolute, *absolute.parents):
        if not candidate.exists():
            continue
        if candidate.is_symlink() or _is_reparse_point(candidate):
            raise MigrationError(
                f"{label} cannot traverse a symlink or reparse point"
            )


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(flag and attributes & flag)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return (canonical_json(payload) + "\n").encode("utf-8")


def _write_json_once(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"migration evidence already exists: {path.name}")
    encoded = _canonical_json_bytes(payload)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_once_or_same(path: Path, payload: dict[str, Any]) -> None:
    encoded = _canonical_json_bytes(payload)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != encoded:
            raise MigrationError(f"migration evidence conflicts: {path.name}")
        return
    _write_json_once(path, payload)


def _quarantine_rolled_back_receipt(
    run_root: Path,
    receipt: dict[str, Any],
) -> None:
    receipt_path = run_root / "migration-receipt.json"
    rolled_back_path = run_root / ROLLED_BACK_RECEIPT_NAME
    encoded = _canonical_json_bytes(receipt)
    rolled_back_exists = rolled_back_path.exists() or rolled_back_path.is_symlink()
    if rolled_back_exists and (
        rolled_back_path.is_symlink()
        or not rolled_back_path.is_file()
        or rolled_back_path.read_bytes() != encoded
    ):
        raise MigrationError("rolled-back migration receipt conflicts")
    receipt_exists = receipt_path.exists() or receipt_path.is_symlink()
    if not receipt_exists:
        return
    if (
        receipt_path.is_symlink()
        or not receipt_path.is_file()
        or receipt_path.read_bytes() != encoded
    ):
        raise MigrationError("migration receipt changed before rollback")
    if rolled_back_exists:
        receipt_path.unlink()
    else:
        os.rename(receipt_path, rolled_back_path)
    _fsync_directory(run_root)


def _write_json_replace(path: Path, payload: dict[str, Any]) -> None:
    if path.is_symlink() or not path.is_file():
        raise MigrationError(f"migration evidence is unavailable: {path.name}")
    encoded = _canonical_json_bytes(payload)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_failure_best_effort(
    run_root: Path,
    failure: BaseException,
    rollback_failure: BaseException | None = None,
) -> None:
    payload: dict[str, Any] = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "kind": "finance_llm_v2_user_migration_failure",
        "status": "failed",
        "failed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "error_type": type(failure).__name__,
        "error": str(failure),
    }
    if rollback_failure is not None:
        payload["rollback_error_type"] = type(rollback_failure).__name__
        payload["rollback_error"] = str(rollback_failure)
    try:
        _write_json_once(run_root / "failure.json", payload)
    except Exception:
        pass


def _new_run_id() -> str:
    return uuid.uuid4().hex[:10]


class _MigrationLock(AbstractContextManager["_MigrationLock"]):
    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream: Any = None

    def __enter__(self) -> "_MigrationLock":
        _require_plain_local_path(self.path.parent, "migration lock parent")
        if self.path.exists():
            _require_plain_local_path(self.path, "migration lock")
        self._stream = self.path.open("a+b")
        self._stream.seek(0, os.SEEK_END)
        if self._stream.tell() == 0:
            self._stream.write(b"0")
            self._stream.flush()
        self._stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._stream.close()
            self._stream = None
            raise MigrationError("another V2 migration is already running") from exc
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._stream is None:
            return None
        try:
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None
        return None


def _settings_from_cli(data_root_override: Path | None) -> UserMigrationSettings:
    from src.configs import config

    if data_root_override is None:
        data_root = Path(config.DB_PATH).expanduser().absolute().parent
        db_path = Path(config.DB_PATH)
        faiss_dir = Path(config.FAISS_DIR)
        source_dir = Path(config.SAVE_DIR)
    else:
        data_root = data_root_override.expanduser().absolute()
        db_path = data_root / "reports.db"
        faiss_dir = data_root / "vector_db"
        source_dir = data_root / "downloaded"
    extractor = str(config.UNEMBEDDED_EXTRACTION_ENGINE or config.EXTRACTION_ENGINE)
    return UserMigrationSettings(
        install_root=REPOSITORY_ROOT,
        data_root=data_root,
        db_path=db_path,
        faiss_dir=faiss_dir,
        source_dir=source_dir,
        model=config.EMBEDDING_MODEL,
        extractor=extractor,
        parent_chunk_size=config.PARENT_CHUNK_SIZE,
        child_chunk_size=config.CHILD_CHUNK_SIZE,
        single_chunk_size=config.CHUNK_SIZE,
        use_parent_child=config.USE_PARENT_CHILD,
    )


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Safely migrate a standard Finance LLM V1 data directory to native V2"
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        help="standard-layout data directory; defaults to the configured DB_PATH parent",
    )
    args = parser.parse_args(argv)
    _configure_console()
    print("=" * 68)
    print("Finance LLM V2 안전 마이그레이션")
    print("V1 원본은 유지하고, 검증된 복사본만 V2로 활성화합니다.")
    print("=" * 68)
    try:
        outcome = migrate_v1_to_v2(_settings_from_cli(args.data_root))
    except KeyboardInterrupt:
        print("\n사용자 요청으로 중단했습니다. V1 원본은 삭제되지 않습니다.")
        return 130
    except Exception as exc:
        print(f"\n[실패] {exc}")
        print("V2 활성화 전 실패했거나 V1으로 자동 롤백되었습니다.")
        print("자세한 기록: data 폴더 옆 .v2m")
        return 1

    if outcome.status == "already_migrated":
        print("\n[완료] 이미 쓰기 가능한 V2 데이터가 활성화되어 있습니다.")
    else:
        print("\n[완료] 쓰기 가능한 V2 변환과 실제 GUI 실행 확인을 통과했습니다.")
        print(f"V1 백업: {outcome.backup_root}")
        print(f"검증 기록: {outcome.receipt_path}")
    print("현재 상태: V2 질문/검색 및 데이터 갱신 가능")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
