"""Runner-owned provenance for immutable retrieval benchmark evidence."""

from __future__ import annotations

import hashlib
import importlib
import inspect
import platform
import re
import stat
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from src.retrieval.identity import canonical_json


_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MODULE_NAME = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_ATTRIBUTE_NAME = re.compile(r"^[A-Za-z_]\w*$")
_LAYOUT_ALGORITHM = "sha256-canonical-runtime-python-v1"
_RUNNER_ENTRYPOINT = "scripts.migrations.v2.run_v2_retrieval_benchmark:main"


class BenchmarkProvenanceError(ValueError):
    """Raised when benchmark code or interpreter provenance is unsafe."""


def build_benchmark_provenance(
    factory_entrypoint: str,
    factory_callable: Callable[..., Any],
    *,
    runner_path: str | Path,
) -> dict[str, Any]:
    """Hash the actual adapter, runtime Python tree, runner, and interpreter."""

    declared = normalize_factory_entrypoint(factory_entrypoint)
    resolved = callable_identity(factory_callable)
    if resolved != declared:
        raise BenchmarkProvenanceError(
            "declared benchmark factory does not match the resolved adapter callable"
        )

    adapter_path_value = inspect.getsourcefile(factory_callable)
    if not adapter_path_value:
        raise BenchmarkProvenanceError("benchmark adapter source is unavailable")
    adapter_path = _safe_regular_file(Path(adapter_path_value), label="adapter source")
    runner = _safe_regular_file(Path(runner_path), label="benchmark runner source")
    runtime_root = Path(__file__).resolve(strict=True).parents[3]
    records: dict[str, str] = {}
    for source in sorted(runtime_root.rglob("*.py"), key=lambda item: item.as_posix()):
        safe_source = _safe_regular_file(source, label="runtime source")
        logical_path = safe_source.relative_to(runtime_root.parent).as_posix()
        records[logical_path] = _sha256_file(safe_source)

    records["scripts/migrations/v2/run_v2_retrieval_benchmark.py"] = _sha256_file(runner)
    try:
        adapter_path.relative_to(runtime_root)
    except ValueError:
        adapter_logical_path = (
            "adapter/" + declared.partition(":")[0].replace(".", "/") + ".py"
        )
        records[adapter_logical_path] = _sha256_file(adapter_path)

    layout_records = [
        {"logical_path": logical_path, "sha256": digest}
        for logical_path, digest in sorted(records.items())
    ]
    layout_sha256 = hashlib.sha256(
        canonical_json(layout_records).encode("utf-8")
    ).hexdigest()
    executable = _safe_regular_file(Path(sys.executable), label="Python interpreter")
    provenance = {
        "schema_version": 1,
        "kind": "v2_retrieval_benchmark_provenance",
        "factory_entrypoint": declared,
        "adapter_callable": resolved,
        "adapter_module_sha256": _sha256_file(adapter_path),
        "runner_entrypoint": _RUNNER_ENTRYPOINT,
        "runner_module_sha256": _sha256_file(runner),
        "runtime_code_layout_sha256": layout_sha256,
        "runtime_code_file_count": len(layout_records),
        "layout_algorithm": _LAYOUT_ALGORITHM,
        "interpreter": {
            "implementation": platform.python_implementation().lower(),
            "version": platform.python_version(),
            "cache_tag": sys.implementation.cache_tag,
            "executable_sha256": _sha256_file(executable),
        },
    }
    validate_benchmark_provenance(provenance)
    return provenance


def normalize_factory_entrypoint(value: Any) -> str:
    """Require the direct ``module:function`` syntax used by the worker CLI."""

    if not isinstance(value, str) or value != value.strip():
        raise BenchmarkProvenanceError(
            "benchmark factory entrypoint must be an exact module:function string"
        )
    module_name, separator, attribute = value.partition(":")
    if (
        separator != ":"
        or not _MODULE_NAME.fullmatch(module_name)
        or not _ATTRIBUTE_NAME.fullmatch(attribute)
    ):
        raise BenchmarkProvenanceError(
            "benchmark factory entrypoint must be an exact module:function string"
        )
    return value


def callable_identity(value: Callable[..., Any]) -> str:
    """Return a stable identity and reject wrappers or nested callables."""

    module_name = getattr(value, "__module__", None)
    qualname = getattr(value, "__qualname__", None)
    if (
        not isinstance(module_name, str)
        or not isinstance(qualname, str)
        or not _MODULE_NAME.fullmatch(module_name)
        or not _ATTRIBUTE_NAME.fullmatch(qualname)
    ):
        raise BenchmarkProvenanceError(
            "benchmark adapter must be a direct module-level callable"
        )
    return f"{module_name}:{qualname}"


def validate_benchmark_provenance(value: Any) -> None:
    """Validate the exact redacted provenance schema retained in evidence."""

    expected_fields = {
        "schema_version",
        "kind",
        "factory_entrypoint",
        "adapter_callable",
        "adapter_module_sha256",
        "runner_entrypoint",
        "runner_module_sha256",
        "runtime_code_layout_sha256",
        "runtime_code_file_count",
        "layout_algorithm",
        "interpreter",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise BenchmarkProvenanceError("benchmark provenance fields are invalid")
    if (
        value["schema_version"] != 1
        or value["kind"] != "v2_retrieval_benchmark_provenance"
    ):
        raise BenchmarkProvenanceError("benchmark provenance identity is invalid")
    declared = normalize_factory_entrypoint(value["factory_entrypoint"])
    resolved = normalize_factory_entrypoint(value["adapter_callable"])
    if resolved != declared:
        raise BenchmarkProvenanceError(
            "benchmark provenance factory and adapter identities differ"
        )
    for field in (
        "adapter_module_sha256",
        "runner_module_sha256",
        "runtime_code_layout_sha256",
    ):
        if not isinstance(value[field], str) or not _HEX_DIGEST.fullmatch(value[field]):
            raise BenchmarkProvenanceError(
                f"benchmark provenance {field} is not a SHA-256 digest"
            )
    if value["runner_entrypoint"] != _RUNNER_ENTRYPOINT:
        raise BenchmarkProvenanceError("benchmark runner identity is invalid")
    file_count = value["runtime_code_file_count"]
    if not isinstance(file_count, int) or isinstance(file_count, bool) or file_count <= 0:
        raise BenchmarkProvenanceError(
            "benchmark runtime code file count must be positive"
        )
    if value["layout_algorithm"] != _LAYOUT_ALGORITHM:
        raise BenchmarkProvenanceError("benchmark layout algorithm is invalid")
    interpreter = value["interpreter"]
    if not isinstance(interpreter, Mapping) or set(interpreter) != {
        "implementation",
        "version",
        "cache_tag",
        "executable_sha256",
    }:
        raise BenchmarkProvenanceError(
            "benchmark interpreter provenance fields are invalid"
        )
    for field in ("implementation", "version", "cache_tag"):
        item = interpreter[field]
        if not isinstance(item, str) or not item.strip():
            raise BenchmarkProvenanceError(
                f"benchmark interpreter {field} must be non-empty"
            )
    executable_sha256 = interpreter["executable_sha256"]
    if not isinstance(executable_sha256, str) or not _HEX_DIGEST.fullmatch(
        executable_sha256
    ):
        raise BenchmarkProvenanceError(
            "benchmark interpreter executable hash is invalid"
        )


def verify_current_benchmark_provenance(
    value: Any,
    *,
    runner_path: str | Path,
) -> None:
    """Recompute provenance in the current install and require exact equality."""

    validate_benchmark_provenance(value)
    factory_entrypoint = str(value["factory_entrypoint"])
    module_name, _separator, attribute = factory_entrypoint.partition(":")
    module = importlib.import_module(module_name)
    factory_callable = getattr(module, attribute, None)
    if not callable(factory_callable):
        raise BenchmarkProvenanceError("benchmark adapter callable is unavailable")
    expected = build_benchmark_provenance(
        factory_entrypoint,
        factory_callable,
        runner_path=runner_path,
    )
    if canonical_json(value) != canonical_json(expected):
        raise BenchmarkProvenanceError(
            "benchmark provenance does not match the current code and interpreter"
        )


def _safe_regular_file(path: Path, *, label: str) -> Path:
    if path.is_symlink():
        raise BenchmarkProvenanceError(f"{label} must not be a symlink")
    try:
        metadata = path.stat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise BenchmarkProvenanceError(f"{label} is unavailable") from exc
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or (reparse_flag and file_attributes & reparse_flag)
    ):
        raise BenchmarkProvenanceError(f"{label} must be a regular non-reparse file")
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "BenchmarkProvenanceError",
    "build_benchmark_provenance",
    "callable_identity",
    "normalize_factory_entrypoint",
    "validate_benchmark_provenance",
    "verify_current_benchmark_provenance",
]
