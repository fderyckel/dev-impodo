"""Load a scenario and verify every referenced local artifact.

Migration stages: cross-cutting source through reconciliation. Layer: adapter.

The adapter reads YAML only through ``yaml.safe_load``. It rejects secret-like
properties before domain validation and resolves fixture, profile, and target
projection paths inside the scenario directory. Validation never contacts
Odoo and never creates Project state.

See ``docs/plans/end-to-end-trial-and-scenario-qualification.md`` and
``tests/integration/scenarios/test_loader.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

from pydantic import ValidationError
import yaml

from impodo.domain.scenarios import (
    FileScenarioSource,
    ScenarioDefinition,
    TargetProjection,
)


MAX_SCENARIO_FILES = 200
MAX_SCENARIO_BYTES = 512 * 1024 * 1024
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "credential",
    "credentials",
    "password",
    "secret",
    "secret_value",
    "token",
}


class ScenarioLoadError(ValueError):
    """Report a safe scenario-definition or contained-artifact failure."""


@dataclass(frozen=True, slots=True)
class LoadedScenario:
    """Return the definition with verified absolute artifact paths and hashes."""

    definition: ScenarioDefinition
    definition_path: Path
    fixture_directory: Path | None
    profile_path: Path | None
    target_projection_path: Path | None
    target_projection: TargetProjection | None
    fixture_hash: str
    fixture_file_count: int
    fixture_bytes: int

    @property
    def scenario_hash(self) -> str:
        return self.definition.semantic_hash


def load_scenario(path: str | Path) -> LoadedScenario:
    """Load one scenario without resolving credentials or contacting Odoo."""

    definition_path = Path(path).resolve(strict=True)
    if not definition_path.is_file() or definition_path.suffix.casefold() not in {
        ".yml",
        ".yaml",
    }:
        raise ScenarioLoadError("scenario definition must be a YAML file")
    try:
        loaded = yaml.safe_load(definition_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ScenarioLoadError(
            f"cannot read scenario {definition_path.name}: {exc}"
        ) from exc
    if not isinstance(loaded, dict):
        raise ScenarioLoadError("scenario definition must contain a YAML object")
    secret_path = _secret_property_path(loaded)
    if secret_path is not None:
        raise ScenarioLoadError(
            f"scenario definitions cannot contain credentials ({secret_path})"
        )
    try:
        definition = ScenarioDefinition.model_validate(loaded)
    except ValidationError as exc:
        errors = []
        for error in exc.errors(include_url=False):
            location = ".".join(str(item) for item in error["loc"])
            errors.append(f"{location or '<root>'}: {error['msg']}")
        raise ScenarioLoadError("invalid scenario:\n- " + "\n- ".join(errors)) from exc

    root = definition_path.parent.resolve()
    fixture_directory: Path | None = None
    fixture_hash = "sha256:" + sha256(b"").hexdigest()
    fixture_file_count = 0
    fixture_bytes = 0
    if isinstance(definition.source, FileScenarioSource):
        fixture_directory = _contained_path(
            root,
            definition.source.fixture_set,
            kind="directory",
        )
        fixture_hash, fixture_file_count, fixture_bytes = _hash_fixture_directory(
            fixture_directory
        )
        if fixture_hash != definition.source.fixture_hash:
            raise ScenarioLoadError("scenario fixture bytes do not match the definition")

    profile_path = (
        _contained_path(root, definition.rules.profile, kind="file")
        if definition.rules.profile is not None
        else None
    )
    if profile_path is not None and _file_hash(profile_path) != definition.rules.profile_hash:
        raise ScenarioLoadError("scenario profile bytes do not match the definition")
    target_projection_path = (
        _contained_path(
            root,
            definition.expectations.target_projection,
            kind="file",
        )
        if definition.expectations.target_projection is not None
        else None
    )
    if (
        target_projection_path is not None
        and _file_hash(target_projection_path)
        != definition.expectations.target_projection_hash
    ):
        raise ScenarioLoadError(
            "scenario target projection bytes do not match the definition"
        )
    target_projection = (
        _load_target_projection(target_projection_path)
        if target_projection_path is not None
        else None
    )
    return LoadedScenario(
        definition=definition,
        definition_path=definition_path,
        fixture_directory=fixture_directory,
        profile_path=profile_path,
        target_projection_path=target_projection_path,
        target_projection=target_projection,
        fixture_hash=fixture_hash,
        fixture_file_count=fixture_file_count,
        fixture_bytes=fixture_bytes,
    )


def _load_target_projection(path: Path) -> TargetProjection:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ScenarioLoadError("cannot read scenario target projection") from exc
    if not isinstance(loaded, dict):
        raise ScenarioLoadError("scenario target projection must contain a JSON object")
    secret_path = _secret_property_path(loaded)
    if secret_path is not None:
        raise ScenarioLoadError("scenario target projection cannot contain credentials")
    try:
        return TargetProjection.model_validate(loaded)
    except ValidationError as exc:
        raise ScenarioLoadError("scenario target projection is invalid") from exc


def _secret_property_path(value: object, prefix: str = "") -> str | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key)
            path = f"{prefix}.{name}" if prefix else name
            normalized = name.casefold().replace("-", "_")
            if normalized in _SECRET_KEYS or normalized.endswith(
                ("_api_key", "_credential", "_credentials", "_password", "_secret", "_token")
            ):
                return path
            found = _secret_property_path(item, path)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            path = f"{prefix}.{index}" if prefix else str(index)
            found = _secret_property_path(item, path)
            if found is not None:
                return found
    return None


def _contained_path(root: Path, relative: str, *, kind: str) -> Path:
    try:
        candidate = (root / relative).resolve(strict=True)
    except OSError as exc:
        raise ScenarioLoadError(f"scenario {kind} reference does not exist") from exc
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ScenarioLoadError(
            f"scenario {kind} reference leaves its containing directory"
        ) from exc
    valid = candidate.is_file() if kind == "file" else candidate.is_dir()
    if not valid:
        raise ScenarioLoadError(f"scenario {kind} reference is not a {kind}")
    return candidate


def _hash_fixture_directory(root: Path) -> tuple[str, int, int]:
    digest = sha256()
    files: list[Path] = []
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ScenarioLoadError("scenario fixtures cannot contain symbolic links")
        if not path.is_file():
            continue
        resolved = path.resolve(strict=True)
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise ScenarioLoadError("scenario fixture leaves its directory") from exc
        files.append(resolved)
        if len(files) > MAX_SCENARIO_FILES:
            raise ScenarioLoadError("scenario fixture contains too many files")
        size = resolved.stat().st_size
        total_bytes += size
        if total_bytes > MAX_SCENARIO_BYTES:
            raise ScenarioLoadError("scenario fixture is larger than the supported bound")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with resolved.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    if not files:
        raise ScenarioLoadError("scenario fixture directory is empty")
    return f"sha256:{digest.hexdigest()}", len(files), total_bytes


def _file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"
