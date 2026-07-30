"""Fixed, read-only metadata capture through a selected local Odoo shell.

Local Odoo does not need a bearer API key when Impodo can use the exact local
``odoo.conf`` and Odoo Python installation selected by the operator.  This
adapter starts an isolated ``odoo-bin shell`` process, executes one fixed read
operation, emits a bounded JSON envelope, and relies on Odoo's shell command to
roll the transaction back when the process exits.

The adapter is intentionally not a generic shell capability.  Callers can only
request the model catalogue or ``fields_get`` metadata for an explicit,
validated model allowlist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping, Sequence

from .connectors import ConnectorError, MetadataSnapshot, RecordSnapshot
from .local_stack import LocalStackProfile
from .models import (
    EnvironmentFingerprint,
    FieldMetadata,
    ModelMetadata,
    TargetRecord,
)
from .projects import MigrationProject, OdooConnectionMode


_OUTPUT_MARKER = "__IMPODO_LOCAL_ODOO_JSON__"
_MAX_OUTPUT_BYTES = 32 * 1024 * 1024
_MODEL_NAME = re.compile(r"^[a-z][a-z0-9_.]{0,199}$")
_DATABASE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")
_MODEL_FIELDS = (
    "name",
    "model",
    "abstract",
    "transient",
    "modules",
    "state",
)
_FIELD_ATTRIBUTES = (
    "string",
    "type",
    "required",
    "readonly",
    "relation",
    "relation_field",
    "selection",
)


class LocalOdooReaderError(ConnectorError):
    """Raised when the fixed local Odoo metadata capture cannot complete."""


@dataclass(frozen=True, slots=True)
class LocalShellResult:
    returncode: int
    stdout: str
    stderr: str


LocalShellRunner = Callable[
    [tuple[str, ...], str, Path, int],
    LocalShellResult,
]


class LocalOdooMetadataReader:
    """Read effective Odoo 19 metadata without a bearer API key."""

    def __init__(
        self,
        *,
        runner: LocalShellRunner | None = None,
        timeout_seconds: int = 120,
    ) -> None:
        self._runner = runner or _run_local_shell
        self._timeout_seconds = timeout_seconds

    def get_environment_fingerprint(
        self,
        project: MigrationProject,
        profile: LocalStackProfile,
    ) -> EnvironmentFingerprint:
        payload = self._invoke(project, profile, _fingerprint_script())
        return self._fingerprint(project, payload)

    def get_model_catalog(
        self,
        project: MigrationProject,
        profile: LocalStackProfile,
    ) -> RecordSnapshot:
        payload = self._invoke(project, profile, _model_catalog_script())
        fingerprint = self._fingerprint(project, payload)
        raw_records = payload.get("records")
        if not isinstance(raw_records, list):
            raise LocalOdooReaderError(
                "The local Odoo model catalogue response is invalid."
            )
        records: list[TargetRecord] = []
        seen_ids: set[int] = set()
        for item in raw_records:
            if not isinstance(item, Mapping):
                raise LocalOdooReaderError(
                    "The local Odoo model catalogue response is invalid."
                )
            try:
                odoo_id = int(item["id"])
            except (KeyError, TypeError, ValueError) as error:
                raise LocalOdooReaderError(
                    "The local Odoo model catalogue response is invalid."
                ) from error
            if odoo_id <= 0 or odoo_id in seen_ids:
                raise LocalOdooReaderError(
                    "The local Odoo model catalogue contains invalid identifiers."
                )
            seen_ids.add(odoo_id)
            records.append(
                TargetRecord(
                    model="ir.model",
                    odoo_id=odoo_id,
                    values={
                        field_name: item.get(field_name)
                        for field_name in _MODEL_FIELDS
                    },
                )
            )
        return RecordSnapshot(
            fingerprint=fingerprint,
            records={
                "ir.model": tuple(
                    sorted(records, key=lambda record: record.odoo_id)
                )
            },
            requested_fields={"ir.model": _MODEL_FIELDS},
            complete=True,
        )

    def get_model_metadata(
        self,
        project: MigrationProject,
        profile: LocalStackProfile,
        models: Sequence[str],
    ) -> MetadataSnapshot:
        requested = tuple(dict.fromkeys(models))
        if not requested:
            raise LocalOdooReaderError(
                "Choose at least one local Odoo model before field capture."
            )
        if any(_MODEL_NAME.fullmatch(model) is None for model in requested):
            raise LocalOdooReaderError(
                "The local Odoo model allowlist contains an invalid name."
            )
        payload = self._invoke(
            project,
            profile,
            _model_metadata_script(requested),
        )
        fingerprint = self._fingerprint(project, payload)
        raw_models = payload.get("models")
        if not isinstance(raw_models, Mapping) or set(raw_models) != set(requested):
            raise LocalOdooReaderError(
                "The local Odoo field response does not match the model allowlist."
            )
        parsed: dict[str, ModelMetadata] = {}
        for model_name in sorted(requested):
            raw_model = raw_models[model_name]
            if not isinstance(raw_model, Mapping):
                raise LocalOdooReaderError(
                    "The local Odoo field response is invalid."
                )
            raw_fields = raw_model.get("fields")
            if not isinstance(raw_fields, Mapping):
                raise LocalOdooReaderError(
                    "The local Odoo field response is invalid."
                )
            fields = {
                str(field_name): _field_metadata(str(field_name), details)
                for field_name, details in raw_fields.items()
            }
            parsed[model_name] = ModelMetadata(
                model=model_name,
                description=str(raw_model.get("description") or model_name),
                fields=fields,
            )
        return MetadataSnapshot(
            fingerprint=fingerprint,
            models=parsed,
            complete=True,
            limitations=(
                "Captured through a fixed local Odoo shell transaction that "
                "is rolled back on exit.",
            ),
        )

    def _invoke(
        self,
        project: MigrationProject,
        profile: LocalStackProfile,
        script: str,
    ) -> Mapping[str, Any]:
        _validate_local_binding(project, profile)
        assert profile.python_path is not None
        assert profile.odoo_bin_path is not None
        command = (
            str(profile.python_path),
            str(profile.odoo_bin_path),
            "shell",
            "-c",
            str(profile.config_path),
            "-d",
            project.odoo_database,
            "--no-http",
            "--log-level=error",
        )
        try:
            result = self._runner(
                command,
                script,
                profile.workspace_root,
                self._timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise LocalOdooReaderError(
                "The fixed local Odoo metadata reader could not start."
            ) from error
        if result.returncode != 0:
            raise LocalOdooReaderError(
                "Local Odoo metadata capture failed. Verify odoo.conf, the "
                "database, PostgreSQL readiness, and the Odoo 19 installation."
            )
        encoded = result.stdout.encode("utf-8", errors="replace")
        if len(encoded) > _MAX_OUTPUT_BYTES:
            raise LocalOdooReaderError(
                "The local Odoo metadata response exceeded the safety limit."
            )
        marker_lines = [
            line[len(_OUTPUT_MARKER) :]
            for line in result.stdout.splitlines()
            if line.startswith(_OUTPUT_MARKER)
        ]
        if len(marker_lines) != 1:
            raise LocalOdooReaderError(
                "The local Odoo metadata reader returned no valid result."
            )
        try:
            payload = json.loads(marker_lines[0])
        except (TypeError, json.JSONDecodeError) as error:
            raise LocalOdooReaderError(
                "The local Odoo metadata reader returned invalid JSON."
            ) from error
        if not isinstance(payload, Mapping):
            raise LocalOdooReaderError(
                "The local Odoo metadata reader returned an invalid result."
            )
        return payload

    @staticmethod
    def _fingerprint(
        project: MigrationProject,
        payload: Mapping[str, Any],
    ) -> EnvironmentFingerprint:
        database = str(payload.get("database") or "")
        version = str(payload.get("version") or "")
        if database != project.odoo_database:
            raise LocalOdooReaderError(
                "The local Odoo shell opened a different database."
            )
        if not version.startswith("19."):
            raise LocalOdooReaderError(
                f"Local schema capture requires Odoo 19; received {version or 'unknown'}."
            )
        assert project.target_environment is not None
        return EnvironmentFingerprint(
            environment=project.target_environment.value,
            database=database,
            odoo_version=version,
            snapshot_timestamp=datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            module_versions={},
        )


def _validate_local_binding(
    project: MigrationProject,
    profile: LocalStackProfile,
) -> None:
    if project.odoo_connection_mode is not OdooConnectionMode.LOCAL:
        raise LocalOdooReaderError(
            "The local Odoo reader is available only in Local mode."
        )
    if project.target_environment is None:
        raise LocalOdooReaderError("Choose a DEV or TEST target environment.")
    if project.odoo_base_url.rstrip("/") != profile.base_url.rstrip("/"):
        raise LocalOdooReaderError(
            "The selected odoo.conf does not match this project's local URL."
        )
    if _DATABASE_NAME.fullmatch(project.odoo_database) is None:
        raise LocalOdooReaderError("The local Odoo database name is invalid.")
    if profile.python_path is None or profile.odoo_bin_path is None:
        raise LocalOdooReaderError(
            "The selected local workspace has no supported Python/Odoo executable."
        )
    workspace = profile.workspace_root.resolve(strict=True)
    for executable in (profile.python_path, profile.odoo_bin_path):
        try:
            executable.resolve(strict=True).relative_to(workspace)
        except (OSError, ValueError) as error:
            raise LocalOdooReaderError(
                "The selected local Odoo executable is outside its workspace."
            ) from error


def _field_metadata(name: str, details: Any) -> FieldMetadata:
    if not isinstance(details, Mapping):
        raise LocalOdooReaderError("The local Odoo field response is invalid.")
    selection: list[tuple[str, str]] = []
    raw_selection = details.get("selection") or ()
    if not isinstance(raw_selection, (list, tuple)):
        raise LocalOdooReaderError("The local Odoo selection metadata is invalid.")
    for item in raw_selection:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise LocalOdooReaderError(
                "The local Odoo selection metadata is invalid."
            )
        selection.append((str(item[0]), str(item[1])))
    return FieldMetadata(
        name=name,
        type=str(details.get("type") or ""),
        label=str(details.get("string") or name),
        required=bool(details.get("required", False)),
        readonly=bool(details.get("readonly", False)),
        relation=(
            str(details["relation"]) if details.get("relation") else None
        ),
        relation_field=(
            str(details["relation_field"])
            if details.get("relation_field")
            else None
        ),
        selection=tuple(selection),
    )


def _run_local_shell(
    command: tuple[str, ...],
    script: str,
    cwd: Path,
    timeout_seconds: int,
) -> LocalShellResult:
    creation_flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if os.name == "nt"
        else 0
    )
    completed = subprocess.run(
        command,
        input=script,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
        creationflags=creation_flags,
    )
    return LocalShellResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _fingerprint_script() -> str:
    return _script(
        """
payload = {
    "database": env.cr.dbname,
    "version": release.version,
}
"""
    )


def _model_catalog_script() -> str:
    return _script(
        f"""
records = env["ir.model"].sudo().search_read(
    [("abstract", "=", False), ("transient", "=", False)],
    {list(_MODEL_FIELDS)!r},
    order="id asc",
)
payload = {{
    "database": env.cr.dbname,
    "version": release.version,
    "records": records,
}}
"""
    )


def _model_metadata_script(models: tuple[str, ...]) -> str:
    return _script(
        f"""
requested_models = {models!r}
captured_models = {{}}
for model_name in requested_models:
    model = env[model_name].sudo()
    captured_models[model_name] = {{
        "description": model._description,
        "fields": model.fields_get(
            allfields=[],
            attributes={list(_FIELD_ATTRIBUTES)!r},
        ),
    }}
payload = {{
    "database": env.cr.dbname,
    "version": release.version,
    "models": captured_models,
}}
"""
    )


def _script(body: str) -> str:
    return f"""
import json
from odoo import release

try:
{_indent(body.strip(), 4)}
    print(
        {_OUTPUT_MARKER!r}
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    )
finally:
    env.cr.rollback()
"""


def _indent(value: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(f"{prefix}{line}" if line else line for line in value.splitlines())
