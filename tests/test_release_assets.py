from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from src.core import release_assets


def _run_git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write_bundle_inputs(root: Path, *, answer: str = "registered") -> dict[str, object]:
    app = root / "app-source"
    runtime = root / "runtime-source"
    app.mkdir(parents=True)
    runtime.mkdir(parents=True)
    (app / "runner.py").write_text(
        "import json, os\n"
        f"payload = {{'answer': {answer!r}, 'snapshot': os.environ['FINANCE_LLM_FIXED_SNAPSHOT_ROOT']}}\n"
        "with open(os.environ['FINANCE_LLM_RUN_ARTIFACT_PATH'], 'w', encoding='utf-8') as handle:\n"
        "    json.dump(payload, handle)\n",
        encoding="utf-8",
    )
    (runtime / "README.txt").write_text("uses the selected Python runtime\n", encoding="utf-8")
    return {
        "app_source": app,
        "runtime_source": runtime,
        "runtime_profile": {"model": "fixture-model", "temperature": 0},
        "runner": {
            "contract_version": 1,
            "command": ["{python}", "{app_root}/runner.py"],
            "artifact_relative_path": "result.json",
        },
    }


def test_runtime_profile_snapshot_captures_complete_non_secret_contract() -> None:
    values = {
        key: f"configured-{index}"
        for index, key in enumerate(
            sorted(release_assets.RUNTIME_PROFILE_ENVIRONMENT_KEYS)
        )
    }
    values["OPENROUTER_API_KEY"] = "must-not-be-persisted"

    environment = release_assets.snapshot_runtime_profile_environment(values)

    assert environment == {
        key: values[key]
        for key in sorted(release_assets.RUNTIME_PROFILE_ENVIRONMENT_KEYS)
    }
    assert "OPENROUTER_API_KEY" not in environment

    missing_key = next(iter(release_assets.RUNTIME_PROFILE_ENVIRONMENT_KEYS))
    with pytest.raises(release_assets.ReleaseAssetError, match="missing settings"):
        release_assets.snapshot_runtime_profile_environment(
            {key: value for key, value in values.items() if key != missing_key}
        )
    with pytest.raises(release_assets.ReleaseAssetError, match="sensitive key"):
        release_assets.validate_runtime_profile(
            {"nested": {"OPENROUTER_API_KEY": "must-not-persist"}}
        )


def _write_project_release_checkout(root: Path) -> None:
    files = {
        "apps/cli/__init__.py": "",
        "apps/cli/app.py": "def run_search(query, thread_id): return {}\n",
        "apps/cli/reproduction_runner.py": "raise SystemExit(0)\n",
        "apps/gui/app.py": "# packaged GUI\n",
        "src/core/example.py": "VALUE = 'packaged'\n",
        "src/core/__pycache__/example.pyc": "must-not-be-bundled\n",
        "README.md": "> Version: 0.6.2\n",
        "requirements.txt": "example-dependency==1.0\n",
        "data/listed_company_industries.csv": (
            "company_name,industry,main_products\nExample,Software,Search\n"
        ),
        ".env": "OPENROUTER_API_KEY=must-not-be-bundled\n",
        ".git/config": "must-not-be-bundled\n",
        "data/private.sqlite3": "must-not-be-bundled\n",
        "tests/test_private.py": "must_not_be_bundled = True\n",
    }
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _initialize_git_release_checkout(root: Path) -> str:
    _write_project_release_checkout(root)
    _run_git(root, "init")
    _run_git(root, "config", "user.email", "release-test@example.com")
    _run_git(root, "config", "user.name", "Release Test")
    _run_git(root, "config", "core.autocrlf", "false")
    _run_git(root, "add", "apps", "src", "README.md", "requirements.txt", "data")
    _run_git(root, "commit", "-m", "Create release fixture")
    return _run_git(root, "rev-parse", "HEAD")


def test_current_project_release_identity_comes_from_clean_git(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    revision = _initialize_git_release_checkout(project_root)

    identity = release_assets.inspect_current_project_release(project_root)

    assert identity.app_version == "0.6.2"
    assert identity.git_revision == revision
    assert len(identity.git_revision) == 40

    (project_root / "src" / "core" / "example.py").write_text(
        "VALUE = 'dirty'\n",
        encoding="utf-8",
    )
    with pytest.raises(release_assets.ReleaseAssetError, match="working tree"):
        release_assets.inspect_current_project_release(project_root)


def test_release_identity_is_version_and_commit_not_runtime_profile(
    tmp_path: Path,
) -> None:
    inputs = _write_bundle_inputs(tmp_path / "inputs")
    first = release_assets.prepare_release_stage(
        tmp_path / "managed-first",
        app_version="0.6.2",
        git_revision="1" * 40,
        manifest_version=release_assets.GIT_RELEASE_SCHEMA_VERSION,
        **inputs,
    )
    inputs["runtime_profile"] = {"environment": {"SEARCH_TOP_K": 99}}
    second = release_assets.prepare_release_stage(
        tmp_path / "managed-second",
        app_version="0.6.2",
        git_revision="1" * 40,
        manifest_version=release_assets.GIT_RELEASE_SCHEMA_VERSION,
        **inputs,
    )

    first_release = release_assets.validate_release_stage(first)
    second_release = release_assets.validate_release_stage(second)

    assert first_release.release_manifest_id == second_release.release_manifest_id
    assert first_release.runtime_profile_digest == second_release.runtime_profile_digest == ""
    assert not (first / "runtime-profile.json").exists()
    assert not (second / "runtime-profile.json").exists()


def test_git_release_executes_with_an_ephemeral_run_profile(
    tmp_path: Path,
) -> None:
    managed_root = tmp_path / "managed"
    inputs = _write_bundle_inputs(tmp_path / "inputs")
    app_source = inputs["app_source"]
    assert isinstance(app_source, Path)
    (app_source / "runner.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "bundle = Path(os.environ['FINANCE_LLM_RELEASE_BUNDLE_ROOT'])\n"
        "profile = json.loads((bundle / 'runtime-profile.json').read_text(encoding='utf-8'))\n"
        "Path(os.environ['FINANCE_LLM_RUN_ARTIFACT_PATH']).write_text(\n"
        "    json.dumps({'runtime_profile': profile}), encoding='utf-8'\n"
        ")\n",
        encoding="utf-8",
    )
    stage = release_assets.prepare_release_stage(
        managed_root,
        app_version="0.6.2",
        git_revision="1" * 40,
        manifest_version=release_assets.GIT_RELEASE_SCHEMA_VERSION,
        **inputs,
    )
    descriptor = release_assets.register_release_stage(
        managed_root,
        stage,
        expected_tag_version="v0.6.2",
    )
    snapshot = managed_root / "snapshots" / "snapshot-1"
    snapshot.mkdir(parents=True)
    runtime_profile = {
        "environment": {"SEARCH_TOP_K": 9},
        "snapshot_reader": {"manifest_schema_version": 2},
    }

    result = release_assets.execute_registered_release(
        managed_root,
        descriptor,
        snapshot_root=snapshot,
        run_id="run-v2-profile",
        runtime_profile=runtime_profile,
    )

    assert json.loads(result.artifact_path.read_text(encoding="utf-8")) == {
        "runtime_profile": runtime_profile
    }
    assert not (descriptor.path / "runtime-profile.json").exists()
    with pytest.raises(release_assets.ReleaseAssetError, match="Run profile"):
        release_assets.execute_registered_release(
            managed_root,
            descriptor,
            snapshot_root=snapshot,
            run_id="run-v2-profile-missing",
        )


def test_release_cache_manifest_schema_must_match_the_registered_record(
    tmp_path: Path,
) -> None:
    managed_root = tmp_path / "managed"
    inputs = _write_bundle_inputs(tmp_path / "inputs")
    stage = release_assets.prepare_release_stage(
        managed_root,
        app_version="0.6.2",
        git_revision="1" * 40,
        **inputs,
    )
    descriptor = release_assets.register_release_stage(
        managed_root,
        stage,
        expected_tag_version="v0.6.2",
    )

    assert release_assets.inspect_release(
        replace(
            descriptor,
            manifest_version=release_assets.GIT_RELEASE_SCHEMA_VERSION,
        )
    ) is release_assets.ReleaseAvailability.CORRUPT


def test_release_cache_checksum_is_verified_without_becoming_identity(
    tmp_path: Path,
) -> None:
    managed_root = tmp_path / "managed"
    inputs = _write_bundle_inputs(tmp_path / "inputs")
    first_stage = release_assets.prepare_release_stage(
        managed_root,
        app_version="0.6.2",
        git_revision="1" * 40,
        manifest_version=release_assets.GIT_RELEASE_SCHEMA_VERSION,
        **inputs,
    )
    descriptor = release_assets.register_release_stage(
        managed_root,
        first_stage,
        expected_tag_version="v0.6.2",
    )
    changed_runner = dict(inputs["runner"])
    changed_runner["artifact_relative_path"] = "changed-result.json"
    second_stage = release_assets.prepare_release_stage(
        managed_root,
        app_source=inputs["app_source"],
        runtime_source=inputs["runtime_source"],
        runtime_profile=inputs["runtime_profile"],
        runner=changed_runner,
        app_version="0.6.2",
        git_revision="1" * 40,
        manifest_version=release_assets.GIT_RELEASE_SCHEMA_VERSION,
    )
    changed = release_assets.validate_release_stage(second_stage)

    assert changed.release_manifest_id == descriptor.release_manifest_id
    assert changed.runtime_bundle_digest != descriptor.runtime_bundle_digest
    assert release_assets.inspect_release(
        replace(descriptor, path=second_stage)
    ) is release_assets.ReleaseAvailability.CORRUPT


def test_missing_release_cache_is_rebuilt_from_git_commit(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    managed_root = tmp_path / "managed"
    revision = _initialize_git_release_checkout(project_root)
    staged = release_assets.prepare_current_project_release_stage(
        managed_root,
        project_root=project_root,
    )
    descriptor = release_assets.register_release_stage(
        managed_root,
        staged,
        expected_tag_version="v0.6.2",
        expected_git_revision=revision,
    )
    release_assets.safe_cleanup(descriptor.path, managed_root=managed_root)
    (project_root / "src" / "core" / "example.py").write_text(
        "VALUE = 'dirty-working-tree'\n",
        encoding="utf-8",
    )

    rebuilt = release_assets.rebuild_release_cache_from_git(
        managed_root,
        project_root=project_root,
        descriptor=descriptor,
    )

    assert rebuilt.release_manifest_id == descriptor.release_manifest_id
    assert rebuilt.git_revision == revision
    assert (rebuilt.path / "app" / "src" / "core" / "example.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 'packaged'\n"
    assert release_assets.inspect_release(rebuilt) is release_assets.ReleaseAvailability.AVAILABLE


def _create_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError:
        if os.name != "nt":
            pytest.skip("symlink creation is unavailable")
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip("directory link creation is unavailable")


def test_project_release_stage_builds_clean_sources_automatically(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    managed_root = project_root / "data" / "monitoring"
    _write_project_release_checkout(project_root)

    stage = release_assets.prepare_project_release_stage(
        managed_root,
        project_root=project_root,
        app_version="0.6.2",
        git_revision="project-revision",
        runtime_profile={"model": "fixture-model"},
        runner={
            "contract_version": 1,
            "command": ["{python}", "{runtime_root}/reproduction_runner.py"],
            "artifact_relative_path": "result.json",
        },
    )

    assert (stage / "app" / "apps" / "cli" / "app.py").is_file()
    assert (stage / "app" / "apps" / "gui" / "app.py").is_file()
    assert (stage / "app" / "src" / "core" / "example.py").is_file()
    assert (stage / "app" / "README.md").is_file()
    assert (stage / "app" / "requirements.txt").is_file()
    assert (
        stage / "app" / "data" / "listed_company_industries.csv"
    ).is_file()
    assert (stage / "runtime" / "reproduction_runner.py").read_text(
        encoding="utf-8"
    ) == "raise SystemExit(0)\n"
    assert not (stage / "app" / ".env").exists()
    assert not (stage / "app" / ".git").exists()
    assert not (stage / "app" / "data" / "private.sqlite3").exists()
    assert not (stage / "app" / "src" / "core" / "__pycache__").exists()
    assert not (stage / "app" / "tests").exists()
    assert not any((managed_root / "release-sources").glob("release-*"))
    assert release_assets.validate_release_stage(stage).stage_path == stage


def test_project_release_source_cleanup_runs_when_staging_fails(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    managed_root = project_root / "data" / "monitoring"
    _write_project_release_checkout(project_root)

    with pytest.raises(release_assets.ReleaseAssetError, match="entrypoint"):
        release_assets.prepare_project_release_stage(
            managed_root,
            project_root=project_root,
            app_version="0.6.2",
            git_revision="project-revision",
            runtime_profile={"model": "fixture-model"},
            runner={
                "contract_version": 1,
                "command": ["{python}", "{bundle_root}/missing-runner.py"],
                "artifact_relative_path": "result.json",
            },
        )

    assert not any((managed_root / "release-sources").glob("release-*"))
    assert not any((managed_root / "staging").glob("release-*"))


def test_project_release_stage_rejects_symlinked_source_parent(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    managed_root = project_root / "data" / "monitoring"
    outside = tmp_path / "outside-sources"
    _write_project_release_checkout(project_root)
    managed_root.mkdir(parents=True)
    outside.mkdir()
    _create_directory_link(managed_root / "release-sources", outside)

    with pytest.raises(
        release_assets.ReleaseAssetError,
        match="symbolic link|escapes the managed root",
    ):
        release_assets.prepare_project_release_stage(
            managed_root,
            project_root=project_root,
            app_version="0.6.2",
            git_revision="project-revision",
            runtime_profile={"model": "fixture-model"},
            runner={
                "contract_version": 1,
                "command": ["{python}", "{runtime_root}/reproduction_runner.py"],
                "artifact_relative_path": "result.json",
            },
        )

    assert list(outside.iterdir()) == []


def test_release_stage_rejects_symlinked_staging_parent(tmp_path: Path) -> None:
    managed_root = tmp_path / "managed"
    outside = tmp_path / "outside-staging"
    inputs = _write_bundle_inputs(tmp_path / "inputs")
    managed_root.mkdir()
    outside.mkdir()
    _create_directory_link(managed_root / "staging", outside)

    with pytest.raises(
        release_assets.ReleaseAssetError,
        match="symbolic link|escapes the managed root",
    ):
        release_assets.prepare_release_stage(
            managed_root,
            app_version="0.6.2",
            git_revision="project-revision",
            **inputs,
        )

    assert list(outside.iterdir()) == []


def test_project_release_preserves_staging_error_when_source_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    managed_root = project_root / "data" / "monitoring"
    _write_project_release_checkout(project_root)
    real_cleanup = release_assets.safe_cleanup

    def fail_source_cleanup(path: Path, *, managed_root: Path, process=None) -> None:
        if "release-sources" in Path(path).parts:
            raise OSError("source cleanup locked")
        real_cleanup(path, managed_root=managed_root, process=process)

    monkeypatch.setattr(release_assets, "safe_cleanup", fail_source_cleanup)

    with pytest.raises(release_assets.ReleaseAssetError) as captured:
        release_assets.prepare_project_release_stage(
            managed_root,
            project_root=project_root,
            app_version="0.6.2",
            git_revision="project-revision",
            runtime_profile={"model": "fixture-model"},
            runner={
                "contract_version": 1,
                "command": ["{python}", "{bundle_root}/missing-runner.py"],
                "artifact_relative_path": "result.json",
            },
        )

    assert "runner entrypoint" in str(captured.value)
    assert "source cleanup locked" in str(captured.value)


def test_project_release_reports_source_and_stage_cleanup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    managed_root = project_root / "data" / "monitoring"
    _write_project_release_checkout(project_root)

    def fail_cleanup(path: Path, *, managed_root: Path, process=None) -> None:
        parts = Path(path).parts
        if "release-sources" in parts:
            raise OSError("source cleanup locked")
        if "staging" in parts:
            raise OSError("stage cleanup locked")
        raise AssertionError(f"unexpected cleanup target: {path}")

    monkeypatch.setattr(release_assets, "safe_cleanup", fail_cleanup)

    with pytest.raises(release_assets.ReleaseAssetError) as captured:
        release_assets.prepare_project_release_stage(
            managed_root,
            project_root=project_root,
            app_version="0.6.2",
            git_revision="project-revision",
            runtime_profile={"model": "fixture-model"},
            runner={
                "contract_version": 1,
                "command": ["{python}", "{runtime_root}/reproduction_runner.py"],
                "artifact_relative_path": "result.json",
            },
        )

    assert "source cleanup locked" in str(captured.value)
    assert "stage cleanup locked" in str(captured.value)


def _registered_release(tmp_path: Path) -> tuple[Path, release_assets.ReleaseDescriptor]:
    managed_root = tmp_path / "managed"
    inputs = _write_bundle_inputs(tmp_path / "inputs")
    staged = release_assets.prepare_release_stage(
        managed_root,
        app_version="0.6.1",
        git_revision="aac850769e97388884e49c0068ea97f691e06d9e",
        **inputs,
    )
    descriptor = release_assets.register_release_stage(
        managed_root,
        staged,
        expected_tag_version="v0.6.1",
        expected_git_revision="aac850769e97388884e49c0068ea97f691e06d9e",
    )
    return managed_root, descriptor


def _fixed_snapshot(
    root: Path,
    *,
    manifest_build_id: str = "fixed-build",
    runtime_build_id: str = "fixed-build",
) -> Path:
    root.mkdir(parents=True)
    catalog = root / "projected_catalog.sqlite3"
    connection = sqlite3.connect(catalog)
    connection.execute(
        "CREATE TABLE retrieval_runtime ("
        "runtime_id INTEGER PRIMARY KEY, active_snapshot_id TEXT, "
        "active_build_id TEXT, publication_generation INTEGER, write_epoch INTEGER)"
    )
    connection.execute(
        "INSERT INTO retrieval_runtime VALUES (1, ?, ?, 1, 0)",
        ("fixed-snapshot", runtime_build_id),
    )
    connection.commit()
    connection.close()
    (root / "subset.faiss").write_bytes(b"fixed-index")
    identity = {
        "sha256": hashlib.sha256(catalog.read_bytes()).hexdigest(),
        "size_bytes": catalog.stat().st_size,
    }
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "files": {"projected_catalog.sqlite3": identity},
                "projected_snapshot_id": "fixed-snapshot",
                "projected_build_id": manifest_build_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return root


def test_stage_validates_exact_bundle_and_registers_create_only(tmp_path: Path) -> None:
    managed_root, descriptor = _registered_release(tmp_path)

    assert descriptor.app_version == "0.6.1"
    assert descriptor.state == "REGISTERED_READY"
    assert descriptor.path == managed_root / "releases" / descriptor.release_manifest_id
    assert sorted(path.name for path in descriptor.path.iterdir()) == [
        "app",
        "object-hashes.json",
        "release-manifest.json",
        "runner.json",
        "runtime",
        "runtime-profile.json",
    ]
    assert release_assets.inspect_release(descriptor).value == "AVAILABLE"


def test_release_stage_rejects_runner_outside_registered_bytes(tmp_path: Path) -> None:
    inputs = _write_bundle_inputs(tmp_path / "inputs")
    outside_runner = tmp_path / "current-checkout" / "runner.py"
    outside_runner.parent.mkdir(parents=True)
    outside_runner.write_text("raise SystemExit(0)\n", encoding="utf-8")
    inputs["runner"] = {
        "contract_version": 1,
        "command": ["{python}", str(outside_runner), "{app_root}"],
        "artifact_relative_path": "result.json",
    }

    with pytest.raises(
        release_assets.ReleaseAssetError,
        match="registered app or runtime",
    ):
        release_assets.prepare_release_stage(
            tmp_path / "managed",
            app_version="0.6.1",
            git_revision="aac850769e97388884e49c0068ea97f691e06d9e",
            **inputs,
        )


@pytest.mark.parametrize(
    "relative_path",
    (
        ".env",
        "config/.env.production",
        "keys/operator-private.key",
        ".git/config",
    ),
)
def test_release_stage_rejects_credential_and_vcs_files(
    tmp_path: Path,
    relative_path: str,
) -> None:
    inputs = _write_bundle_inputs(tmp_path / "inputs")
    app_source = inputs["app_source"]
    assert isinstance(app_source, Path)
    sensitive = app_source / relative_path
    sensitive.parent.mkdir(parents=True, exist_ok=True)
    sensitive.write_text("must-not-be-bundled\n", encoding="utf-8")

    with pytest.raises(release_assets.ReleaseAssetError, match="sensitive file"):
        release_assets.prepare_release_stage(
            tmp_path / "managed",
            app_version="0.6.1",
            git_revision="aac850769e97388884e49c0068ea97f691e06d9e",
            **inputs,
        )


def test_release_stage_rejects_private_key_content_with_safe_looking_name(
    tmp_path: Path,
) -> None:
    inputs = _write_bundle_inputs(tmp_path / "inputs")
    app_source = inputs["app_source"]
    assert isinstance(app_source, Path)
    (app_source / "runtime-config.txt").write_text(
        "-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )

    with pytest.raises(release_assets.ReleaseAssetError, match="private key"):
        release_assets.prepare_release_stage(
            tmp_path / "managed",
            app_version="0.6.1",
            git_revision="aac850769e97388884e49c0068ea97f691e06d9e",
            **inputs,
        )


def test_release_stage_allows_documented_environment_template(tmp_path: Path) -> None:
    inputs = _write_bundle_inputs(tmp_path / "inputs")
    app_source = inputs["app_source"]
    assert isinstance(app_source, Path)
    (app_source / ".env.example").write_text(
        "OPENROUTER_API_KEY=\n",
        encoding="utf-8",
    )
    (app_source / "ca-bundle.pem").write_text(
        "-----BEGIN CERTIFICATE-----\npublic-fixture\n-----END CERTIFICATE-----\n",
        encoding="utf-8",
    )

    stage = release_assets.prepare_release_stage(
        tmp_path / "managed",
        app_version="0.6.1",
        git_revision="aac850769e97388884e49c0068ea97f691e06d9e",
        **inputs,
    )

    assert (stage / "app" / ".env.example").is_file()
    assert (stage / "app" / "ca-bundle.pem").is_file()


def test_first_baseline_accepts_v061_and_rejects_v060(tmp_path: Path) -> None:
    inputs = _write_bundle_inputs(tmp_path / "inputs")
    with pytest.raises(release_assets.ReleaseAssetError, match="v0.6.0"):
        release_assets.prepare_release_stage(
            tmp_path / "managed",
            app_version="0.6.0",
            git_revision="local-only",
            **inputs,
        )
    with pytest.raises(release_assets.ReleaseAssetError, match="official remote"):
        release_assets.prepare_release_stage(
            tmp_path / "managed",
            app_version="0.6.1",
            git_revision="local-lookalike",
            **inputs,
        )


def test_registration_rejects_version_and_revision_conflicts(tmp_path: Path) -> None:
    managed_root = tmp_path / "managed"
    inputs = _write_bundle_inputs(tmp_path / "inputs")
    staged = release_assets.prepare_release_stage(
        managed_root,
        app_version="0.6.1",
        git_revision="aac850769e97388884e49c0068ea97f691e06d9e",
        **inputs,
    )
    validated = release_assets.validate_release_stage(staged)

    with pytest.raises(release_assets.ReleaseAssetError, match="tag"):
        release_assets.register_release_stage(
            managed_root, staged, expected_tag_version="v0.6.2"
        )
    with pytest.raises(release_assets.ReleaseAssetError, match="revision"):
        release_assets.register_release_stage(
            managed_root,
            staged,
            expected_tag_version="v0.6.1",
            expected_git_revision="wrong",
        )
    with pytest.raises(release_assets.ReleaseAssetError, match="different Git commit"):
        release_assets.assert_version_commit_compatible(
            "0.6.1", validated.git_revision, "0" * 40
        )


def test_managed_stage_validation_only_accepts_contained_staging_objects(
    tmp_path: Path,
) -> None:
    managed_root = tmp_path / "managed"
    inputs = _write_bundle_inputs(tmp_path / "inputs")
    staged = release_assets.prepare_release_stage(
        managed_root,
        app_version="0.6.2",
        git_revision="project-revision",
        **inputs,
    )

    validated = release_assets.validate_managed_release_stage(managed_root, staged)

    assert validated.stage_path == staged
    assert validated.app_version == "0.6.2"
    assert validated.git_revision == "project-revision"

    outside = tmp_path / "outside-stage"
    outside.mkdir()
    with pytest.raises(release_assets.ReleaseAssetError, match="managed root"):
        release_assets.validate_managed_release_stage(managed_root, outside)
    with pytest.raises(release_assets.ReleaseAssetError, match="managed root"):
        release_assets.validate_managed_release_stage(managed_root, managed_root)
    with pytest.raises(release_assets.ReleaseAssetError, match="managed staging"):
        release_assets.validate_managed_release_stage(
            managed_root,
            managed_root / "staging",
        )

    descriptor = release_assets.register_release_stage(
        managed_root,
        staged,
        expected_tag_version="v0.6.2",
        expected_git_revision="project-revision",
    )
    with pytest.raises(release_assets.ReleaseAssetError, match="managed staging"):
        release_assets.validate_managed_release_stage(
            managed_root,
            descriptor.path,
        )


def test_availability_distinguishes_missing_corrupt_and_incompatible(tmp_path: Path) -> None:
    managed_root, descriptor = _registered_release(tmp_path)
    assert release_assets.inspect_release(
        descriptor, expected_runner_contract_version=2
    ).value == "INCOMPATIBLE"

    runner_path = descriptor.path / "runner.json"
    original = runner_path.read_bytes()
    runner_path.write_bytes(original + b"\n")
    assert release_assets.inspect_release(descriptor).value == "CORRUPT"
    runner_path.write_bytes(original)
    assert release_assets.inspect_release(descriptor).value == "AVAILABLE"

    release_assets.safe_cleanup(descriptor.path, managed_root=managed_root)
    assert release_assets.inspect_release(descriptor).value == "LOCAL_MISSING"


def test_official_execution_uses_registered_app_and_fixed_snapshot(tmp_path: Path) -> None:
    managed_root, descriptor = _registered_release(tmp_path)
    snapshot = managed_root / "snapshots" / "snapshot-1"
    snapshot.mkdir(parents=True)
    (snapshot / "manifest.json").write_text("{}\n", encoding="utf-8")

    # Changing an unrelated current/source file cannot affect registered execution.
    unrelated = tmp_path / "current-source" / "runner.py"
    unrelated.parent.mkdir()
    unrelated.write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")

    result = release_assets.execute_registered_release(
        managed_root,
        descriptor,
        snapshot_root=snapshot,
        run_id="run-1",
        input_payload={"question": "same fixture"},
        python_executable=sys.executable,
    )

    assert result.returncode == 0
    assert result.cleanup_warning is None
    assert not result.workspace_path.exists()
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))
    assert payload == {"answer": "registered", "snapshot": str(snapshot.resolve())}
    assert result.artifact_path.is_relative_to(managed_root.resolve())


def test_execution_side_effects_never_mutate_registered_release(tmp_path: Path) -> None:
    inputs = _write_bundle_inputs(tmp_path / "inputs")
    app_source = inputs["app_source"]
    assert isinstance(app_source, Path)
    (app_source / "helper.py").write_text("VALUE = 'from-copy'\n", encoding="utf-8")
    (app_source / "runner.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "import helper\n"
        "app_root = Path(os.environ['FINANCE_LLM_RELEASE_APP_ROOT'])\n"
        "(app_root / '__pycache__').mkdir()\n"
        "(app_root / '__pycache__' / 'runner.pyc').write_bytes(b'run-only')\n"
        "(app_root / 'logs').mkdir()\n"
        "(app_root / 'logs' / 'finance_llm.log').write_text('run-only', encoding='utf-8')\n"
        "payload = json.loads(Path(os.environ['FINANCE_LLM_RUN_INPUT_PATH']).read_text(encoding='utf-8'))\n"
        "result = {'answer': helper.VALUE, 'dont_write_bytecode': os.environ.get('PYTHONDONTWRITEBYTECODE')}\n"
        "Path(os.environ['FINANCE_LLM_RUN_ARTIFACT_PATH']).write_text(json.dumps(result), encoding='utf-8')\n"
        "sys.exit(int(payload.get('returncode', 0)))\n",
        encoding="utf-8",
    )
    managed_root = tmp_path / "managed"
    staged = release_assets.prepare_release_stage(
        managed_root,
        app_version="0.6.1",
        git_revision="aac850769e97388884e49c0068ea97f691e06d9e",
        **inputs,
    )
    descriptor = release_assets.register_release_stage(
        managed_root, staged, expected_tag_version="v0.6.1"
    )
    snapshot = managed_root / "snapshots" / "snapshot-1"
    snapshot.mkdir(parents=True)
    registered_files = sorted(
        path.relative_to(descriptor.path).as_posix()
        for path in descriptor.path.rglob("*")
        if path.is_file()
    )

    for run_id, returncode in (("run-success", 0), ("run-failure", 7)):
        result = release_assets.execute_registered_release(
            managed_root,
            descriptor,
            snapshot_root=snapshot,
            run_id=run_id,
            input_payload={"returncode": returncode},
            python_executable=sys.executable,
        )

        assert result.returncode == returncode
        assert json.loads(result.artifact_path.read_text(encoding="utf-8")) == {
            "answer": "from-copy",
            "dont_write_bytecode": "1",
        }
        assert release_assets.inspect_release(descriptor).value == "AVAILABLE"
        assert sorted(
            path.relative_to(descriptor.path).as_posix()
            for path in descriptor.path.rglob("*")
            if path.is_file()
        ) == registered_files
        assert not (descriptor.path / "app" / "__pycache__").exists()
        assert not (descriptor.path / "app" / "logs").exists()


def test_old_runner_sees_fixed_snapshot_checkpoint_and_floor(tmp_path: Path) -> None:
    inputs = _write_bundle_inputs(tmp_path / "inputs")
    app_source = inputs["app_source"]
    assert isinstance(app_source, Path)
    (app_source / "runner.py").write_text(
        "import hashlib, json, os, shutil\n"
        "from pathlib import Path\n"
        "snapshot = Path(os.environ['FINANCE_LLM_FIXED_SNAPSHOT_ROOT'])\n"
        "workspace = Path(os.environ['FINANCE_LLM_RUN_WORKSPACE'])\n"
        "data_root = workspace / 'isolated-data'\n"
        "catalog = snapshot / 'projected_catalog.sqlite3'\n"
        "target_catalog = data_root / 'retrieval' / 'v2' / 'catalog.sqlite3'\n"
        "target_catalog.parent.mkdir(parents=True, exist_ok=True)\n"
        "shutil.copyfile(catalog, target_catalog)\n"
        "shutil.copyfile(snapshot / 'subset.faiss', data_root / 'subset.faiss')\n"
        "shutil.copyfile(snapshot / 'manifest.json', data_root / 'fixed-snapshot-manifest.json')\n"
        "floor_path = data_root / 'retrieval' / 'v2' / 'evidence' / 'fixed-snapshot-projection' / 'committed-floor.json'\n"
        "floor = json.loads(floor_path.read_text(encoding='utf-8'))\n"
        "checkpoint = data_root / floor['checkpoint_relative_path']\n"
        "payload = {\n"
        "    'schema_version': floor['schema_version'],\n"
        "    'publication_generation': floor['publication_generation'],\n"
        "    'write_epoch': floor['write_epoch'],\n"
        "    'active_snapshot_id': floor['active_snapshot_id'],\n"
        "    'checkpoint_sha256': hashlib.sha256(checkpoint.read_bytes()).hexdigest(),\n"
        "    'floor_checkpoint_sha256': floor['checkpoint_sha256'],\n"
        "    'checkpoint_matches_catalog': checkpoint.read_bytes() == catalog.read_bytes(),\n"
        "    'empty_dotenv': (workspace / '.env').read_bytes() == b'',\n"
        "}\n"
        "Path(os.environ['FINANCE_LLM_RUN_ARTIFACT_PATH']).write_text(json.dumps(payload), encoding='utf-8')\n",
        encoding="utf-8",
    )
    managed_root = tmp_path / "managed"
    staged = release_assets.prepare_release_stage(
        managed_root,
        app_version="0.6.1",
        git_revision="aac850769e97388884e49c0068ea97f691e06d9e",
        **inputs,
    )
    descriptor = release_assets.register_release_stage(
        managed_root, staged, expected_tag_version="v0.6.1"
    )
    snapshot = _fixed_snapshot(managed_root / "snapshots" / "fixed")
    catalog_digest = hashlib.sha256(
        (snapshot / "projected_catalog.sqlite3").read_bytes()
    ).hexdigest()

    result = release_assets.execute_registered_release(
        managed_root,
        descriptor,
        snapshot_root=snapshot,
        run_id="run-old-reader",
        python_executable=sys.executable,
    )

    assert json.loads(result.artifact_path.read_text(encoding="utf-8")) == {
        "schema_version": 2,
        "publication_generation": 1,
        "write_epoch": 0,
        "active_snapshot_id": "fixed-snapshot",
        "checkpoint_sha256": catalog_digest,
        "floor_checkpoint_sha256": catalog_digest,
        "checkpoint_matches_catalog": True,
        "empty_dotenv": True,
    }
    assert release_assets.inspect_release(descriptor).value == "AVAILABLE"


def test_fixed_snapshot_runtime_mismatch_fails_before_process_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root, descriptor = _registered_release(tmp_path)
    snapshot = _fixed_snapshot(
        managed_root / "snapshots" / "mismatch",
        manifest_build_id="manifest-build",
        runtime_build_id="catalog-build",
    )
    process_started = False

    def unexpected_popen(*args, **kwargs):
        nonlocal process_started
        process_started = True
        raise AssertionError("Popen must not be called")

    monkeypatch.setattr(release_assets.subprocess, "Popen", unexpected_popen)

    with pytest.raises(
        release_assets.ReleaseAssetError, match="runtime does not match manifest"
    ):
        release_assets.execute_registered_release(
            managed_root,
            descriptor,
            snapshot_root=snapshot,
            run_id="run-fixed-mismatch",
            python_executable=sys.executable,
        )

    assert process_started is False
    assert not (managed_root / "workspaces" / "run-fixed-mismatch").exists()


def test_runner_environment_is_allowlisted_and_explicit_secrets_are_ephemeral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _write_bundle_inputs(tmp_path / "inputs")
    app_source = inputs["app_source"]
    assert isinstance(app_source, Path)
    (app_source / "runner.py").write_text(
        "import json, os\n"
        "payload = {\n"
        "    'inherited_admin_secret': os.environ.get('SUPABASE_SERVICE_ROLE_KEY'),\n"
        "    'explicit_model_secret_present': bool(os.environ.get('OPENROUTER_API_KEY')),\n"
        "    'pythonpath': os.environ.get('PYTHONPATH'),\n"
        "}\n"
        "with open(os.environ['FINANCE_LLM_RUN_ARTIFACT_PATH'], 'w', encoding='utf-8') as handle:\n"
        "    json.dump(payload, handle)\n",
        encoding="utf-8",
    )
    managed_root = tmp_path / "managed"
    staged = release_assets.prepare_release_stage(
        managed_root,
        app_version="0.6.1",
        git_revision="aac850769e97388884e49c0068ea97f691e06d9e",
        **inputs,
    )
    descriptor = release_assets.register_release_stage(
        managed_root,
        staged,
        expected_tag_version="v0.6.1",
    )
    snapshot = managed_root / "snapshots" / "snapshot-1"
    snapshot.mkdir(parents=True)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "must-not-cross-boundary")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "current-checkout"))

    result = release_assets.execute_registered_release(
        managed_root,
        descriptor,
        snapshot_root=snapshot,
        run_id="run-env-boundary",
        extra_environment={"OPENROUTER_API_KEY": "ephemeral-model-key"},
    )
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))
    assert payload == {
        "inherited_admin_secret": None,
        "explicit_model_secret_present": True,
        "pythonpath": None,
    }

    with pytest.raises(release_assets.ReleaseAssetError, match="not allowed"):
        release_assets.execute_registered_release(
            managed_root,
            descriptor,
            snapshot_root=snapshot,
            run_id="run-env-rejected",
            extra_environment={"SUPABASE_SERVICE_ROLE_KEY": "forbidden"},
        )


def test_runner_cannot_load_ancestor_dotenv_file(tmp_path: Path) -> None:
    inputs = _write_bundle_inputs(tmp_path / "inputs")
    app_source = inputs["app_source"]
    assert isinstance(app_source, Path)
    (app_source / "runner.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "from dotenv import load_dotenv\n"
        "load_dotenv()\n"
        "payload = {\n"
        "    'dotenv_disabled': os.environ.get('PYTHON_DOTENV_DISABLED'),\n"
        "    'ancestor_secret': os.environ.get('ANCESTOR_SECRET'),\n"
        "}\n"
        "Path(os.environ['FINANCE_LLM_RUN_ARTIFACT_PATH']).write_text(json.dumps(payload), encoding='utf-8')\n",
        encoding="utf-8",
    )
    managed_root = tmp_path / "managed"
    staged = release_assets.prepare_release_stage(
        managed_root,
        app_version="0.6.1",
        git_revision="aac850769e97388884e49c0068ea97f691e06d9e",
        **inputs,
    )
    descriptor = release_assets.register_release_stage(
        managed_root, staged, expected_tag_version="v0.6.1"
    )
    (managed_root / ".env").write_text(
        "ANCESTOR_SECRET=must-not-load\n", encoding="utf-8"
    )
    snapshot = managed_root / "snapshots" / "snapshot-1"
    snapshot.mkdir(parents=True)

    result = release_assets.execute_registered_release(
        managed_root,
        descriptor,
        snapshot_root=snapshot,
        run_id="run-dotenv-boundary",
        python_executable=sys.executable,
    )

    assert json.loads(result.artifact_path.read_text(encoding="utf-8")) == {
        "dotenv_disabled": "1",
        "ancestor_secret": None,
    }


def test_communication_interrupt_kills_reaps_and_cleans_up_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root, descriptor = _registered_release(tmp_path)
    snapshot = managed_root / "snapshots" / "snapshot-1"
    snapshot.mkdir(parents=True)

    class InterruptedProcess:
        returncode: int | None = None
        killed = False
        reaped = False

        def communicate(self, timeout=None):
            raise KeyboardInterrupt

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

        def wait(self):
            assert self.killed
            self.reaped = True
            return self.returncode

    process = InterruptedProcess()
    monkeypatch.setattr(
        release_assets.subprocess, "Popen", lambda *args, **kwargs: process
    )

    with pytest.raises(KeyboardInterrupt):
        release_assets.execute_registered_release(
            managed_root,
            descriptor,
            snapshot_root=snapshot,
            run_id="run-interrupted",
            python_executable=sys.executable,
        )

    assert process.killed is True
    assert process.reaped is True
    assert process.poll() == -9
    assert not (managed_root / "workspaces" / "run-interrupted").exists()


def test_artifact_is_saved_before_cleanup_and_cleanup_failure_is_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root, descriptor = _registered_release(tmp_path)
    snapshot = managed_root / "snapshots" / "snapshot-1"
    snapshot.mkdir(parents=True)
    real_cleanup = release_assets.safe_cleanup

    def fail_workspace_cleanup(path: Path, *, managed_root: Path, process=None) -> None:
        if "workspaces" in path.parts:
            assert any((managed_root / "run-artifacts").rglob("*.json"))
            raise OSError("locked workspace")
        real_cleanup(path, managed_root=managed_root, process=process)

    monkeypatch.setattr(release_assets, "safe_cleanup", fail_workspace_cleanup)
    result = release_assets.execute_registered_release(
        managed_root,
        descriptor,
        snapshot_root=snapshot,
        run_id="run-cleanup-warning",
        python_executable=sys.executable,
    )

    assert result.returncode == 0
    assert result.artifact_path.exists()
    assert "locked workspace" in (result.cleanup_warning or "")


def test_cleanup_refuses_outside_root_and_running_process(tmp_path: Path) -> None:
    managed_root = tmp_path / "managed"
    workspace = managed_root / "workspaces" / "run"
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(release_assets.ReleaseAssetError, match="managed root"):
        release_assets.safe_cleanup(outside, managed_root=managed_root)

    class Running:
        def poll(self):
            return None

    with pytest.raises(release_assets.ReleaseAssetError, match="running process"):
        release_assets.safe_cleanup(workspace, managed_root=managed_root, process=Running())
    assert workspace.exists()


def test_canonical_tree_digest_is_order_stable_and_rejects_symlinks(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "b.txt").write_bytes(b"b")
    (left / "a.txt").write_bytes(b"a")
    (right / "a.txt").write_bytes(b"a")
    (right / "b.txt").write_bytes(b"b")
    assert release_assets.canonical_tree_digest(left) == release_assets.canonical_tree_digest(right)

    try:
        (left / "link.txt").symlink_to(left / "a.txt")
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(release_assets.ReleaseAssetError, match="symbolic link"):
        release_assets.canonical_tree_digest(left)
