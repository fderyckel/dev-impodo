"""Fixed, read-only schema and Stage-H capture through a local Odoo shell.

Local Odoo does not need a bearer API key when Impodo can use the exact local
``odoo.conf`` and Odoo Python installation selected by the operator.  This
adapter starts an isolated ``odoo-bin shell`` process, executes one fixed read
operation, emits a bounded JSON envelope, and relies on Odoo's shell command to
roll the transaction back when the process exits.

The adapter is intentionally not a generic shell capability. Callers can only
request the model catalogue, allowlisted ``fields_get`` metadata, or one exact
bounded preflight request plan. The fixed script paginates by model rather than
source row, and Odoo rolls its transaction back when the process exits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping, Sequence

from .connectors import (
    ConnectorError,
    MetadataRequest,
    MetadataSnapshot,
    RecordRequest,
    RecordSnapshot,
)
from .local_stack import LocalStackProfile
from .models import (
    FieldMetadata,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
    UniqueConstraintMetadata,
    target_identity_hash,
)
from .projects import MigrationProject, OdooConnectionMode


_OUTPUT_MARKER = "__IMPODO_LOCAL_ODOO_JSON__"
_MAX_OUTPUT_BYTES = 32 * 1024 * 1024
_MODEL_NAME = re.compile(r"^[a-z][a-z0-9_.]{0,199}$")
_FIELD_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,199}$")
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
    """Capture bounded process output from one fixed local Odoo shell call."""

    returncode: int
    stdout: str
    stderr: str


LocalShellRunner = Callable[
    [tuple[str, ...], str, Path, int],
    LocalShellResult,
]


class LocalOdooMetadataReader:
    """Read Odoo 19 schema or bounded preflight snapshots without an API key."""

    def __init__(
        self,
        *,
        runner: LocalShellRunner | None = None,
        timeout_seconds: int = 120,
    ) -> None:
        self._runner = runner or _run_local_shell
        self._timeout_seconds = timeout_seconds

    def get_target_fingerprint(
        self,
        project: MigrationProject,
        profile: LocalStackProfile,
    ) -> TargetFingerprint:
        """Read only target identity/version evidence for the selected local stack."""

        payload = self._invoke(project, profile, _fingerprint_script())
        return self._fingerprint(project, payload)

    def get_model_catalog(
        self,
        project: MigrationProject,
        profile: LocalStackProfile,
    ) -> RecordSnapshot:
        """Read the bounded persistent ``ir.model`` catalogue for Stage C."""

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
        """Read ``fields_get`` only for the explicit validated model allowlist."""

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
            raw_constraints = raw_model.get("unique_constraints", ())
            if not isinstance(raw_constraints, (list, tuple)):
                raise LocalOdooReaderError(
                    "The local Odoo constraint response is invalid."
                )
            constraints: list[UniqueConstraintMetadata] = []
            for item in raw_constraints:
                if not isinstance(item, Mapping):
                    raise LocalOdooReaderError(
                        "The local Odoo constraint response is invalid."
                    )
                constraints.append(
                    UniqueConstraintMetadata(
                        name=str(item.get("name") or ""),
                        definition=str(item.get("definition") or ""),
                    )
                )
            parsed[model_name] = ModelMetadata(
                model=model_name,
                description=str(raw_model.get("description") or model_name),
                fields=fields,
                unique_constraints=tuple(constraints),
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

    def get_preflight_snapshots(
        self,
        project: MigrationProject,
        profile: LocalStackProfile,
        metadata_requests: Sequence[MetadataRequest],
        record_requests: Sequence[RecordRequest],
        *,
        related_models: Sequence[str] = (),
    ) -> tuple[MetadataSnapshot, RecordSnapshot]:
        """Capture one consistent, read-only metadata and record snapshot.

        The fixed shell script loops over planned models, never source rows.
        Record reads are deterministically paginated and the shell transaction
        is rolled back before exit.
        """

        metadata = tuple(metadata_requests)
        records = tuple(record_requests)
        permitted_related_models = set(related_models)
        if any(
            _MODEL_NAME.fullmatch(model) is None
            for model in permitted_related_models
        ):
            raise LocalOdooReaderError(
                "The local related-model allowlist is invalid."
            )
        permitted_models = {
            *project.intended_models,
            *permitted_related_models,
        }
        requested_models = {
            *(item.model for item in metadata),
            *(item.model for item in records),
        }
        if not requested_models or not requested_models.issubset(permitted_models):
            raise LocalOdooReaderError(
                "Local readiness requests must stay inside the project or "
                "linked-model scope."
            )
        for request in metadata:
            if (
                _MODEL_NAME.fullmatch(request.model) is None
                or request.all_fields
                or any(_FIELD_NAME.fullmatch(field) is None for field in request.fields)
            ):
                raise LocalOdooReaderError(
                    "The local readiness metadata request is invalid."
                )
        for request in records:
            if (
                _MODEL_NAME.fullmatch(request.model) is None
                or any(_FIELD_NAME.fullmatch(field) is None for field in request.fields)
            ):
                raise LocalOdooReaderError(
                    "The local readiness record request is invalid."
                )
        payload = self._invoke(
            project,
            profile,
            _preflight_script(metadata, records),
        )
        fingerprint = self._fingerprint(project, payload)
        raw_models = payload.get("models")
        raw_records = payload.get("records")
        if not isinstance(raw_models, Mapping) or not isinstance(
            raw_records, Mapping
        ):
            raise LocalOdooReaderError(
                "The local readiness response is invalid."
            )

        parsed_models: dict[str, ModelMetadata] = {}
        for request in metadata:
            raw_model = raw_models.get(request.model)
            if not isinstance(raw_model, Mapping):
                raise LocalOdooReaderError(
                    "The local readiness metadata response is incomplete."
                )
            raw_fields = raw_model.get("fields")
            if not isinstance(raw_fields, Mapping):
                raise LocalOdooReaderError(
                    "The local readiness metadata response is invalid."
                )
            parsed_models[request.model] = ModelMetadata(
                model=request.model,
                description=str(raw_model.get("description") or request.model),
                fields={
                    str(name): _field_metadata(str(name), details)
                    for name, details in raw_fields.items()
                },
            )

        parsed_records: dict[str, tuple[TargetRecord, ...]] = {}
        requested_fields: dict[str, tuple[str, ...]] = {}
        request_by_model: dict[str, RecordRequest] = {}
        for request in records:
            previous = request_by_model.setdefault(request.model, request)
            if previous.fields != request.fields:
                raise LocalOdooReaderError(
                    "Local readiness chunks use inconsistent record fields."
                )
        for request in request_by_model.values():
            raw_items = raw_records.get(request.model)
            if not isinstance(raw_items, list):
                raise LocalOdooReaderError(
                    "The local readiness record response is incomplete."
                )
            model_records: dict[int, TargetRecord] = {}
            for item in raw_items:
                if not isinstance(item, Mapping):
                    raise LocalOdooReaderError(
                        "The local readiness record response is invalid."
                    )
                try:
                    odoo_id = int(item["id"])
                except (KeyError, TypeError, ValueError) as error:
                    raise LocalOdooReaderError(
                        "The local readiness record response is invalid."
                    ) from error
                if odoo_id <= 0:
                    raise LocalOdooReaderError(
                        "The local readiness record response has invalid IDs."
                    )
                record = TargetRecord(
                    model=request.model,
                    odoo_id=odoo_id,
                    values={
                        field: item.get(field)
                        for field in request.fields
                        if field in item
                    },
                )
                previous = model_records.setdefault(odoo_id, record)
                if previous != record:
                    raise LocalOdooReaderError(
                        "The local readiness record chunks conflict."
                    )
            parsed_records[request.model] = tuple(
                sorted(model_records.values(), key=lambda item: item.odoo_id)
            )
            requested_fields[request.model] = tuple(request.fields)

        return (
            MetadataSnapshot(
                fingerprint=fingerprint,
                models=parsed_models,
                complete=True,
                limitations=(
                    "Captured through a fixed local Odoo shell transaction that "
                    "is rolled back on exit.",
                ),
            ),
            RecordSnapshot(
                fingerprint=fingerprint,
                records=parsed_records,
                requested_fields=requested_fields,
                complete=True,
            ),
        )

    def _invoke(
        self,
        project: MigrationProject,
        profile: LocalStackProfile,
        script: str,
    ) -> Mapping[str, Any]:
        """Run one fixed script against the validated local stack and parse it."""

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
    ) -> TargetFingerprint:
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
        assert project.odoo_connection_mode is not None
        return TargetFingerprint(
            target_hash=target_identity_hash(
                connection_mode=project.odoo_connection_mode.value,
                base_url=project.odoo_base_url,
                database=project.odoo_database,
            ),
            connection_mode=project.odoo_connection_mode.value,
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
constraints_by_model = {{model_name: [] for model_name in requested_models}}
constraints = env["ir.model.constraint"].sudo().search(
    [("model.model", "in", requested_models), ("type", "=", "u")],
    order="id asc",
)
for constraint in constraints:
    constraints_by_model[constraint.model.model].append({{
        "name": constraint.name,
        "definition": constraint.definition or "",
    }})
for model_name in requested_models:
    model = env[model_name].sudo()
    captured_models[model_name] = {{
        "description": model._description,
        "fields": model.fields_get(
            allfields=[],
            attributes={list(_FIELD_ATTRIBUTES)!r},
        ),
        "unique_constraints": constraints_by_model[model_name],
    }}
payload = {{
    "database": env.cr.dbname,
    "version": release.version,
    "models": captured_models,
}}
"""
    )


def _preflight_script(
    metadata_requests: tuple[MetadataRequest, ...],
    record_requests: tuple[RecordRequest, ...],
) -> str:
    metadata_payload = [
        {"model": item.model, "fields": list(item.fields)}
        for item in metadata_requests
    ]
    record_payload = [
        {
            "model": item.model,
            "fields": list(item.fields),
            "domain": _json_value(item.domain),
        }
        for item in record_requests
    ]
    encoded_metadata = json.dumps(metadata_payload, ensure_ascii=True)
    encoded_records = json.dumps(record_payload, ensure_ascii=True)
    if len(encoded_metadata) + len(encoded_records) > 8 * 1024 * 1024:
        raise LocalOdooReaderError(
            "The local readiness request exceeds the safe request limit."
        )
    return _script(
        f"""
metadata_requests = json.loads({encoded_metadata!r})
record_requests = json.loads({encoded_records!r})
captured_models = {{}}
for request in metadata_requests:
    model = env[request["model"]].sudo()
    captured_models[request["model"]] = {{
        "description": model._description,
        "fields": model.fields_get(
            allfields=request["fields"],
            attributes={list(_FIELD_ATTRIBUTES)!r},
        ),
    }}
captured_records = {{}}
for request in record_requests:
    model = env[request["model"]].sudo()
    offset = 0
    rows = []
    while True:
        page = model.search_read(
            request["domain"],
            ["id", *request["fields"]],
            offset=offset,
            limit=500,
            order="id asc",
        )
        rows.extend(page)
        if len(page) < 500:
            break
        offset += len(page)
    captured_records.setdefault(request["model"], []).extend(rows)
payload = {{
    "database": env.cr.dbname,
    "version": release.version,
    "models": captured_models,
    "records": captured_records,
}}
"""
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _script(body: str) -> str:
    return f"""
import json
from odoo import release

try:
{_indent(body.strip(), 4)}
    print(
        {_OUTPUT_MARKER!r}
        + json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    )
finally:
    env.cr.rollback()
"""


def _indent(value: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(f"{prefix}{line}" if line else line for line in value.splitlines())
