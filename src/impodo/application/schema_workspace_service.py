"""Orchestrate Stage C read-only Odoo schema discovery and governance.

Layer: application service.

The schema router supplies closed, project-bound snapshots.
``SchemaWorkspaceService`` verifies their completeness, target identity,
connection mode, database, Odoo version, and permitted-model coverage before
publishing catalogs. Governance then binds explicit business keys to the exact
captured schema. This module cannot call Odoo by itself.

See ``docs/architecture/python-code-map.md``,
``docs/contracts/02-workspace.md``, and ``tests/test_workspace.py``.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import re
from typing import Iterable, Mapping, Protocol
from uuid import uuid4

from ..access import Actor, AuthorizationPolicy, Capability
from ..connectors import MetadataSnapshot, RecordSnapshot
from ..domain.schema.governance import (
    BusinessKeyDefinition,
    SchemaGovernance,
)
from ..models import target_identity_hash
from ..projects import (
    MigrationProject,
    OdooConnectionMode,
    ProjectStatus,
    SourceMode,
)
from ..workspace_contracts import (
    OdooModelCatalog,
    OdooModelSummary,
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceSelection,
)
from ..workspace_errors import WorkspaceError
from ..domain.serialization import content_hash


_TECHNICAL_MODEL = re.compile(r"^[a-z_][a-z0-9_.]{0,127}$")
_CONTENT_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class SchemaProjectReader(Protocol):
    """Read the registered project and configured target identity."""

    def get(self, project_id: str) -> MigrationProject:
        """Return registration, target identity, and permitted-model scope."""
        ...


class SourceSelectionReader(Protocol):
    """Prove that Stage B dataset freezing precedes schema capture."""

    def get_source_selection(self, project_id: str) -> SourceSelection | None:
        """Return Stage B evidence required before detailed schema capture."""
        ...


class SchemaWorkspaceRepository(Protocol):
    """Persist model/schema catalogs and versioned key governance."""

    def get_odoo_model_catalog(
        self,
        project_id: str,
    ) -> OdooModelCatalog | None:
        """Return current lightweight model discovery for the target."""
        ...

    def save_odoo_model_catalog(
        self,
        project_id: str,
        catalog: OdooModelCatalog,
        *,
        actor: Actor,
    ) -> None:
        """Publish current target-bound model choices and an audit event."""
        ...

    def get_odoo_schema_catalog(
        self,
        project_id: str,
    ) -> OdooSchemaCatalog | None:
        """Return the current detailed permitted-model schema catalog."""
        ...

    def save_odoo_schema_catalog(
        self,
        project_id: str,
        catalog: OdooSchemaCatalog,
        *,
        actor: Actor,
    ) -> None:
        """Publish detailed schema and retire governance/mapping dependents."""
        ...

    def get_schema_governance(
        self,
        project_id: str,
    ) -> SchemaGovernance | None:
        """Return current key governance for the current schema, if present."""
        ...

    def save_schema_governance(
        self,
        project_id: str,
        governance: SchemaGovernance,
        *,
        actor: Actor,
    ) -> None:
        """Append the next exact key-governance revision and invalidate mapping."""
        ...


class SchemaWorkspaceService:
    """Validate target-bound snapshots and govern mapping-visible identities.

    Model discovery, schema capture, and key governance are separate evidence
    transitions. A manual local draft is explicitly unverified and may support
    mapping experiments, but the mapping service blocks its submission.
    """

    def __init__(
        self,
        projects: SchemaProjectReader,
        sources: SourceSelectionReader,
        schemas: SchemaWorkspaceRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.projects = projects
        self.sources = sources
        self.schemas = schemas
        self.authorization = authorization

    def discover_models(
        self,
        project_id: str,
        snapshot: RecordSnapshot,
        *,
        read_credential_binding_hash: str,
        actor: Actor,
    ) -> OdooModelCatalog:
        """Store concrete model choices returned by the connected Odoo."""

        self.authorization.require(
            actor,
            Capability.SCHEMA_DISCOVER,
            project_id=project_id,
        )
        project = self.projects.get(project_id)
        if project.status is not ProjectStatus.REGISTERED:
            raise WorkspaceError(
                "Register the project before discovering Odoo models"
            )
        if project.odoo_connection_mode is None:
            raise WorkspaceError(
                "Configure the Odoo target before discovering models"
            )
        _validate_read_credential_binding_hash(read_credential_binding_hash)
        if not snapshot.complete:
            raise WorkspaceError("Odoo model discovery response is incomplete")
        if snapshot.fingerprint.target_hash != _target_identity_hash(project):
            raise WorkspaceError("Odoo model target does not match the project")
        if (
            snapshot.fingerprint.connection_mode
            != project.odoo_connection_mode.value
        ):
            raise WorkspaceError(
                "Odoo model connection mode does not match the project"
            )
        if snapshot.fingerprint.database != project.odoo_database:
            raise WorkspaceError(
                "Odoo model database does not match the project"
            )
        if not snapshot.fingerprint.odoo_version.startswith("19."):
            raise WorkspaceError("Odoo model discovery requires Odoo 19")
        if set(snapshot.records) != {"ir.model"}:
            raise WorkspaceError(
                "Odoo model discovery returned an unexpected model"
            )

        models: list[OdooModelSummary] = []
        seen: set[str] = set()
        for record in snapshot.records["ir.model"]:
            values = record.values
            if bool(values.get("abstract")) or bool(values.get("transient")):
                continue
            name = str(values.get("model") or "").strip()
            label = str(values.get("name") or name).strip()
            if not _TECHNICAL_MODEL.fullmatch(name):
                raise WorkspaceError(
                    "Odoo model discovery returned an invalid name"
                )
            if not label:
                raise WorkspaceError(f"Odoo model {name} has no label")
            if name in seen:
                raise WorkspaceError(
                    f"Odoo model {name} was returned more than once"
                )
            seen.add(name)
            models.append(
                OdooModelSummary(
                    name=name,
                    label=label,
                    modules=_split_module_names(values.get("modules")),
                    state=str(values.get("state") or "base"),
                )
            )
        if not models:
            raise WorkspaceError(
                "No persistent Odoo models are visible through this connected "
                "read-only metadata boundary"
            )
        ordered = tuple(
            sorted(models, key=lambda item: (item.label.casefold(), item.name))
        )
        target_hash = _target_identity_hash(project)
        content = {
            "target_hash": target_hash,
            "read_credential_binding_hash": read_credential_binding_hash,
            "fingerprint": snapshot.fingerprint.portable_dict(),
            "models": [asdict(model) for model in ordered],
        }
        catalog = OdooModelCatalog(
            project_id=project_id,
            target_hash=target_hash,
            captured_at=datetime.now(timezone.utc),
            captured_by=actor.identity.display_name,
            connection_mode=snapshot.fingerprint.connection_mode,
            database=snapshot.fingerprint.database,
            odoo_version=snapshot.fingerprint.odoo_version,
            models=ordered,
            content_hash=content_hash(content),
            read_credential_binding_hash=read_credential_binding_hash,
        )
        self.schemas.save_odoo_model_catalog(
            project_id,
            catalog,
            actor=actor,
        )
        return catalog

    def capture(
        self,
        project_id: str,
        snapshot: MetadataSnapshot,
        *,
        read_credential_binding_hash: str,
        actor: Actor,
    ) -> OdooSchemaCatalog:
        """Capture a verified catalog through the connected read-only reader."""

        project, permitted = self._capture_context(project_id, actor=actor)
        _validate_read_credential_binding_hash(read_credential_binding_hash)
        if not snapshot.complete:
            raise WorkspaceError("Odoo schema response is incomplete")
        if set(snapshot.models) != permitted:
            raise WorkspaceError(
                "Odoo schema response does not match permitted models"
            )
        if snapshot.fingerprint.target_hash != _target_identity_hash(project):
            raise WorkspaceError("Odoo schema target does not match the project")
        if (
            snapshot.fingerprint.connection_mode
            != project.odoo_connection_mode.value
        ):
            raise WorkspaceError(
                "Odoo schema connection mode does not match the project"
            )
        if snapshot.fingerprint.database != project.odoo_database:
            raise WorkspaceError(
                "Odoo schema database does not match the project"
            )
        if not snapshot.fingerprint.odoo_version.startswith("19."):
            raise WorkspaceError("Odoo schema capture requires Odoo 19")
        discovered = self.schemas.get_odoo_model_catalog(project_id)
        if (
            discovered is not None
            and discovered.read_credential_binding_hash
            != read_credential_binding_hash
        ):
            raise WorkspaceError(
                "The Odoo read credential changed; refresh the model catalogue "
                "before capturing schema"
            )
        discovered_labels = (
            {model.name: model.label for model in discovered.models}
            if discovered
            and discovered.target_hash == _target_identity_hash(project)
            else {}
        )
        missing_discovered = permitted - set(discovered_labels)
        if discovered and missing_discovered:
            missing = sorted(missing_discovered)[0]
            raise WorkspaceError(
                f"{missing} is no longer in the refreshed Odoo model catalogue; "
                "save the permitted model scope again"
            )
        models = tuple(
            SchemaModel(
                name=name,
                label=discovered_labels.get(name) or model.description or name,
                fields=tuple(
                    SchemaField(
                        name=field_name,
                        label=field.label or field_name,
                        type=field.type,
                        required=field.required,
                        readonly=field.readonly,
                        relation=field.relation,
                        relation_field=field.relation_field,
                        selection=field.selection,
                    )
                    for field_name, field in sorted(model.fields.items())
                ),
                unique_constraints=model.unique_constraints,
            )
            for name, model in sorted(snapshot.models.items())
        )
        self._validate_schema_models(models, permitted)
        return self._store_catalog(
            project,
            models=models,
            connection_mode=snapshot.fingerprint.connection_mode,
            database=snapshot.fingerprint.database,
            odoo_version=snapshot.fingerprint.odoo_version,
            fingerprint=snapshot.fingerprint.portable_dict(),
            origin=SchemaOrigin.LIVE_API,
            read_credential_binding_hash=read_credential_binding_hash,
            actor=actor,
        )

    def capture_local_manual(
        self,
        project_id: str,
        models: Iterable[SchemaModel],
        *,
        read_credential_binding_hash: str,
        actor: Actor,
    ) -> OdooSchemaCatalog:
        """Store an explicitly unverified schema draft for local work."""

        project, permitted = self._capture_context(project_id, actor=actor)
        _validate_read_credential_binding_hash(read_credential_binding_hash)
        if project.odoo_connection_mode is not OdooConnectionMode.LOCAL:
            raise WorkspaceError(
                "A manual schema draft is available only for Local Odoo"
            )
        declared_models = tuple(sorted(models, key=lambda item: item.name))
        self._validate_schema_models(declared_models, permitted)
        return self._store_catalog(
            project,
            models=declared_models,
            connection_mode=project.odoo_connection_mode.value,
            database=project.odoo_database,
            odoo_version="unverified local draft (expected Odoo 19)",
            fingerprint={
                "target_hash": _target_identity_hash(project),
                "connection_mode": project.odoo_connection_mode.value,
                "database": project.odoo_database,
                "odoo_version": "unverified local draft (expected Odoo 19)",
                "snapshot_timestamp": "not captured",
                "module_versions": {},
            },
            origin=SchemaOrigin.LOCAL_MANUAL,
            read_credential_binding_hash=read_credential_binding_hash,
            actor=actor,
        )

    def _capture_context(
        self,
        project_id: str,
        *,
        actor: Actor,
    ) -> tuple[MigrationProject, set[str]]:
        self.authorization.require(
            actor,
            Capability.SCHEMA_DISCOVER,
            project_id=project_id,
        )
        project = self.projects.get(project_id)
        if project.status is not ProjectStatus.REGISTERED:
            raise WorkspaceError(
                "Register the project before capturing Odoo schema"
            )
        if (
            project.source_mode is SourceMode.FILE
            and self.sources.get_source_selection(project_id) is None
        ):
            raise WorkspaceError(
                "Freeze source datasets before capturing Odoo schema"
            )
        permitted = set(project.intended_models)
        if not permitted:
            raise WorkspaceError(
                "Add at least one permitted technical Odoo model to the project"
            )
        if project.odoo_connection_mode is None:
            raise WorkspaceError(
                "Configure the Odoo target before capturing schema"
            )
        return project, permitted

    @staticmethod
    def _validate_schema_models(
        models: tuple[SchemaModel, ...],
        permitted: set[str],
    ) -> None:
        if {model.name for model in models} != permitted:
            raise WorkspaceError(
                "Schema models do not match the permitted scope"
            )
        if len(models) != len(permitted):
            raise WorkspaceError("Schema models must be unique")
        if any(not model.label or not model.fields for model in models):
            raise WorkspaceError(
                "Each permitted model must have a label and field"
            )
        for model in models:
            names = [field.name for field in model.fields]
            if len(names) != len(set(names)):
                raise WorkspaceError(
                    f"Schema fields for {model.name} must be unique"
                )
            if any(
                not field.name or not field.label or not field.type
                for field in model.fields
            ):
                raise WorkspaceError(
                    f"Schema fields for {model.name} need a name, label, and type"
                )

    def _store_catalog(
        self,
        project: MigrationProject,
        *,
        models: tuple[SchemaModel, ...],
        connection_mode: str,
        database: str,
        odoo_version: str,
        fingerprint: Mapping[str, object],
        origin: SchemaOrigin,
        read_credential_binding_hash: str,
        actor: Actor,
    ) -> OdooSchemaCatalog:
        target_hash = content_hash(
            {
                "mode": (
                    project.odoo_connection_mode.value
                    if project.odoo_connection_mode
                    else None
                ),
                "url": project.odoo_base_url,
                "database": project.odoo_database,
                "models": sorted(model.name for model in models),
            }
        )
        content = {
            "target_hash": target_hash,
            "read_credential_binding_hash": read_credential_binding_hash,
            "fingerprint": fingerprint,
            "origin": origin.value,
            "models": [asdict(model) for model in models],
        }
        catalog = OdooSchemaCatalog(
            project_id=project.project_id,
            target_hash=target_hash,
            captured_at=datetime.now(timezone.utc),
            captured_by=actor.identity.display_name,
            connection_mode=connection_mode,
            database=database,
            odoo_version=odoo_version,
            models=models,
            content_hash=content_hash(content),
            origin=origin,
            read_credential_binding_hash=read_credential_binding_hash,
        )
        self.schemas.save_odoo_schema_catalog(
            project.project_id,
            catalog,
            actor=actor,
        )
        return catalog

    def govern(
        self,
        project_id: str,
        *,
        business_keys: Iterable[BusinessKeyDefinition],
        actor: Actor,
    ) -> SchemaGovernance:
        """Confirm explicit natural keys for the current captured schema."""

        self.authorization.require(
            actor,
            Capability.SCHEMA_GOVERN,
            project_id=project_id,
        )
        project = self.projects.get(project_id)
        if (
            project.source_mode is SourceMode.ODOO
            and self.sources.get_source_selection(project_id) is None
        ):
            raise WorkspaceError(
                "Freeze the selected Odoo source records before confirming keys"
            )
        schema = self.schemas.get_odoo_schema_catalog(project_id)
        if schema is None:
            raise WorkspaceError(
                "Capture the Odoo schema before confirming keys"
            )
        models = {model.name: model for model in schema.models}
        normalized = tuple(
            sorted(
                business_keys,
                key=lambda item: (
                    item.model,
                    item.key_fields,
                    item.scope_fields,
                    item.key_id,
                ),
            )
        )
        if not normalized:
            raise WorkspaceError(
                "Confirm at least one governed business key"
            )
        seen_ids: set[str] = set()
        seen_shapes: set[
            tuple[str, tuple[str, ...], tuple[str, ...]]
        ] = set()
        for definition in normalized:
            if definition.key_id in seen_ids:
                raise WorkspaceError("Business-key IDs must be unique")
            seen_ids.add(definition.key_id)
            shape = (
                definition.model,
                definition.key_fields,
                definition.scope_fields,
            )
            if shape in seen_shapes:
                raise WorkspaceError(
                    "Business-key definitions must be unique"
                )
            seen_shapes.add(shape)
            model = models.get(definition.model)
            if model is None:
                raise WorkspaceError(
                    f"Business-key model {definition.model} is not captured"
                )
            available = {field.name for field in model.fields}
            missing = [
                item
                for item in (
                    *definition.key_fields,
                    *definition.scope_fields,
                )
                if item not in available
            ]
            if missing:
                raise WorkspaceError(
                    f"Business-key field {definition.model}.{missing[0]} "
                    "is not captured"
                )
        previous = self.schemas.get_schema_governance(project_id)
        governance = SchemaGovernance(
            governance_id=(
                previous.governance_id if previous else str(uuid4())
            ),
            version=previous.version + 1 if previous else 1,
            project_id=project_id,
            catalog_hash=schema.content_hash,
            permitted_models=tuple(sorted(models)),
            business_keys=normalized,
            recorded_at=datetime.now(timezone.utc),
            recorded_by=actor.identity.display_name,
        )
        self.schemas.save_schema_governance(
            project_id,
            governance,
            actor=actor,
        )
        return governance


def _target_identity_hash(project: MigrationProject) -> str:
    return target_identity_hash(
        connection_mode=(
            project.odoo_connection_mode.value
            if project.odoo_connection_mode
            else ""
        ),
        base_url=project.odoo_base_url,
        database=project.odoo_database,
    )


def _validate_read_credential_binding_hash(value: str) -> None:
    if not _CONTENT_HASH.fullmatch(value):
        raise WorkspaceError("Odoo read credential binding is invalid")


def _split_module_names(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        candidates = re.split(r"[,;\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        candidates = [str(item) for item in value]
    else:
        candidates = [str(value)]
    return tuple(
        sorted(
            {
                candidate.strip()
                for candidate in candidates
                if candidate.strip()
            }
        )
    )
