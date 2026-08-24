"""Orchestrate Stage C read-only Odoo schema discovery and governance.

Layer: application service.

The schema router supplies closed, project-bound snapshots.
``SchemaWorkspaceService`` verifies their completeness, target identity,
connection mode, database, Odoo version, and permitted-model coverage before
publishing catalogs. Governance then binds explicit business keys to the exact
captured schema. This module cannot call Odoo by itself.

See ``docs/architecture/python-code-map.md``,
``docs/developer/contracts/evidence-lifecycle.md``, and ``tests/test_workspace.py``.
"""

from __future__ import annotations

from dataclasses import asdict, replace
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
from ..models import OdooReadIdentity, target_identity_hash
from ..domain.odoo_source_policy import ODOO_SOURCE_POLICY_HASH
from ..workspace_state import (
    WorkspaceState,
    OdooConnectionMode,
    WorkspaceStatus,
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


class SchemaWorkspaceReader(Protocol):
    """Read the registered workspace projection and target identity."""

    def get(self, workspace_id: str) -> WorkspaceState:
        """Return registration, target identity, and permitted-model scope."""
        ...


class SourceSelectionReader(Protocol):
    """Prove that Stage B dataset freezing precedes schema capture."""

    def get_source_selection(self, workspace_id: str) -> SourceSelection | None:
        """Return Stage B evidence required before detailed schema capture."""
        ...


class SchemaWorkspaceRepository(Protocol):
    """Persist model/schema catalogs and versioned key governance."""

    def get_odoo_model_catalog(
        self,
        workspace_id: str,
    ) -> OdooModelCatalog | None:
        """Return current lightweight model discovery for the target."""
        ...

    def save_odoo_model_catalog(
        self,
        workspace_id: str,
        catalog: OdooModelCatalog,
        *,
        actor: Actor,
    ) -> None:
        """Publish current target-bound model choices and an audit event."""
        ...

    def get_odoo_schema_catalog(
        self,
        workspace_id: str,
    ) -> OdooSchemaCatalog | None:
        """Return the current detailed permitted-model schema catalog."""
        ...

    def save_odoo_schema_catalog(
        self,
        workspace_id: str,
        catalog: OdooSchemaCatalog,
        *,
        actor: Actor,
    ) -> None:
        """Publish detailed schema and retire governance/mapping dependents."""
        ...

    def rebind_odoo_schema_access(
        self,
        workspace_id: str,
        catalog: OdooSchemaCatalog,
        *,
        expected_content_hash: str,
        expected_read_credential_binding_hash: str,
        actor: Actor,
    ) -> None:
        """Update verified access evidence without retiring semantic dependents."""
        ...

    def get_schema_governance(
        self,
        workspace_id: str,
    ) -> SchemaGovernance | None:
        """Return current key governance for the current schema, if present."""
        ...

    def save_schema_governance(
        self,
        workspace_id: str,
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
        workspaces: SchemaWorkspaceReader,
        sources: SourceSelectionReader,
        schemas: SchemaWorkspaceRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.workspaces = workspaces
        self.sources = sources
        self.schemas = schemas
        self.authorization = authorization

    def discover_models(
        self,
        workspace_id: str,
        snapshot: RecordSnapshot,
        *,
        read_credential_binding_hash: str,
        read_identity: OdooReadIdentity | None = None,
        actor: Actor,
    ) -> OdooModelCatalog:
        """Store concrete model choices returned by the connected Odoo."""

        self.authorization.require(
            actor,
            Capability.SCHEMA_DISCOVER,
            workspace_id=workspace_id,
        )
        workspace_state = self.workspaces.get(workspace_id)
        if workspace_state.status is not WorkspaceStatus.REGISTERED:
            raise WorkspaceError(
                "Register the project before discovering Odoo models"
            )
        if workspace_state.odoo_connection_mode is None:
            raise WorkspaceError(
                "Configure the Odoo target before discovering models"
            )
        _validate_read_credential_binding_hash(read_credential_binding_hash)
        if not snapshot.complete:
            raise WorkspaceError("Odoo model discovery response is incomplete")
        if snapshot.fingerprint.target_hash != _target_identity_hash(workspace_state):
            raise WorkspaceError("Odoo model target does not match the project")
        if (
            snapshot.fingerprint.connection_mode
            != workspace_state.odoo_connection_mode.value
        ):
            raise WorkspaceError(
                "Odoo model connection mode does not match the project"
            )
        if snapshot.fingerprint.database != workspace_state.odoo_database:
            raise WorkspaceError(
                "Odoo model database does not match the project"
            )
        if not snapshot.fingerprint.odoo_version.startswith("19."):
            raise WorkspaceError("Odoo model discovery requires Odoo 19")
        if set(snapshot.records) != {"ir.model"}:
            raise WorkspaceError(
                "Odoo model discovery returned an unexpected model"
            )
        identity_hashes = _validate_read_identity(
            workspace_state,
            read_identity,
            required_models=("ir.model",),
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
        connection_target_hash = _target_identity_hash(workspace_state)
        content = {
            "connection_target_hash": connection_target_hash,
            "policy_hash": ODOO_SOURCE_POLICY_HASH,
            "read_credential_binding_hash": read_credential_binding_hash,
            **identity_hashes,
            "fingerprint": snapshot.fingerprint.portable_dict(),
            "models": [asdict(model) for model in ordered],
        }
        catalog = OdooModelCatalog(
            workspace_id=workspace_id,
            connection_target_hash=connection_target_hash,
            policy_hash=ODOO_SOURCE_POLICY_HASH,
            captured_at=datetime.now(timezone.utc),
            captured_by=actor.identity.display_name,
            connection_mode=snapshot.fingerprint.connection_mode,
            database=snapshot.fingerprint.database,
            odoo_version=snapshot.fingerprint.odoo_version,
            models=ordered,
            content_hash=content_hash(content),
            read_credential_binding_hash=read_credential_binding_hash,
            read_principal_hash=identity_hashes["read_principal_hash"],
            read_permission_hash=identity_hashes["read_permission_hash"],
            read_context_hash=identity_hashes["read_context_hash"],
        )
        self.schemas.save_odoo_model_catalog(
            workspace_id,
            catalog,
            actor=actor,
        )
        return catalog

    def capture(
        self,
        workspace_id: str,
        snapshot: MetadataSnapshot,
        *,
        read_credential_binding_hash: str,
        read_identity: OdooReadIdentity | None = None,
        actor: Actor,
    ) -> OdooSchemaCatalog:
        """Capture a verified catalog through the connected read-only reader."""

        workspace_state, permitted = self._capture_context(workspace_id, actor=actor)
        _validate_read_credential_binding_hash(read_credential_binding_hash)
        if not snapshot.complete:
            raise WorkspaceError("Odoo schema response is incomplete")
        if set(snapshot.models) != permitted:
            raise WorkspaceError(
                "Odoo schema response does not match permitted models"
            )
        if snapshot.fingerprint.target_hash != _target_identity_hash(workspace_state):
            raise WorkspaceError("Odoo schema target does not match the project")
        if (
            snapshot.fingerprint.connection_mode
            != workspace_state.odoo_connection_mode.value
        ):
            raise WorkspaceError(
                "Odoo schema connection mode does not match the project"
            )
        if snapshot.fingerprint.database != workspace_state.odoo_database:
            raise WorkspaceError(
                "Odoo schema database does not match the project"
            )
        if not snapshot.fingerprint.odoo_version.startswith("19."):
            raise WorkspaceError("Odoo schema capture requires Odoo 19")
        identity_hashes = _validate_read_identity(
            workspace_state,
            read_identity,
            required_models=tuple(sorted(permitted)),
        )
        discovered = self.schemas.get_odoo_model_catalog(workspace_id)
        if discovered is not None:
            if read_identity is None:
                if (
                    discovered.read_credential_binding_hash
                    != read_credential_binding_hash
                ):
                    raise WorkspaceError(
                        "The Odoo read credential changed; refresh the model "
                        "catalogue before capturing schema"
                    )
            elif (
                discovered.read_principal_hash
                != identity_hashes["read_principal_hash"]
                or discovered.read_context_hash
                != identity_hashes["read_context_hash"]
            ):
                raise WorkspaceError(
                    "The Odoo read principal or context changed; refresh the "
                    "model catalogue before capturing schema"
                )
        models = self._schema_models_from_snapshot(
            workspace_state,
            permitted,
            snapshot,
            discovered=discovered,
        )
        self._validate_schema_models(models, permitted)
        return self._store_catalog(
            workspace_state,
            models=models,
            connection_mode=snapshot.fingerprint.connection_mode,
            database=snapshot.fingerprint.database,
            odoo_version=snapshot.fingerprint.odoo_version,
            fingerprint=snapshot.fingerprint.portable_dict(),
            origin=SchemaOrigin.LIVE_API,
            read_credential_binding_hash=read_credential_binding_hash,
            identity_hashes=identity_hashes,
            actor=actor,
        )

    def rebind_current_access(
        self,
        workspace_id: str,
        snapshot: MetadataSnapshot,
        *,
        read_credential_binding_hash: str,
        read_identity: OdooReadIdentity,
        actor: Actor,
    ) -> OdooSchemaCatalog:
        """Rebind a new read-key generation only after semantic equivalence.

        A credential envelope receives a new random binding whenever a key is
        entered. Re-entry alone must not invalidate approved mapping or prepared
        rows, but it also must not silently bless changed fields or access.
        """

        workspace_state, permitted = self._capture_context(workspace_id, actor=actor)
        _validate_read_credential_binding_hash(read_credential_binding_hash)
        if not snapshot.complete:
            raise WorkspaceError("Odoo schema response is incomplete")
        if set(snapshot.models) != permitted:
            raise WorkspaceError(
                "Odoo schema response does not match permitted models"
            )
        if snapshot.fingerprint.target_hash != _target_identity_hash(workspace_state):
            raise WorkspaceError("Odoo schema target does not match the project")
        if (
            snapshot.fingerprint.connection_mode
            != workspace_state.odoo_connection_mode.value
        ):
            raise WorkspaceError(
                "Odoo schema connection mode does not match the project"
            )
        if snapshot.fingerprint.database != workspace_state.odoo_database:
            raise WorkspaceError("Odoo schema database does not match the project")
        if not snapshot.fingerprint.odoo_version.startswith("19."):
            raise WorkspaceError("Odoo schema capture requires Odoo 19")
        identity_hashes = _validate_read_identity(
            workspace_state,
            read_identity,
            required_models=tuple(sorted(permitted)),
        )
        current = self.schemas.get_odoo_schema_catalog(workspace_id)
        if current is None:
            raise WorkspaceError(
                "Capture the Odoo schema before reconnecting read access"
            )
        if current.origin is not SchemaOrigin.LIVE_API:
            raise WorkspaceError(
                "Refresh the live Odoo schema before reconnecting read access"
            )
        models = self._schema_models_from_snapshot(
            workspace_state,
            permitted,
            snapshot,
        )
        self._validate_schema_models(models, permitted)
        semantic_access_matches = (
            current.workspace_id == workspace_id
            and current.policy_hash == ODOO_SOURCE_POLICY_HASH
            and current.connection_target_hash == _target_identity_hash(workspace_state)
            and current.connection_mode == snapshot.fingerprint.connection_mode
            and current.database == snapshot.fingerprint.database
            and current.odoo_version == snapshot.fingerprint.odoo_version
            and current.models == models
            and current.read_principal_hash
            == identity_hashes["read_principal_hash"]
            and current.read_permission_hash
            == identity_hashes["read_permission_hash"]
            and current.read_context_hash == identity_hashes["read_context_hash"]
        )
        if not semantic_access_matches:
            raise WorkspaceError(
                "The available Odoo fields or read access changed. Impodo kept "
                "your saved matching and prepared data unchanged. Review the "
                "Odoo connection before comparing again. Nothing was changed "
                "in Odoo."
            )
        rebound = replace(
            current,
            captured_at=datetime.now(timezone.utc),
            captured_by=actor.identity.display_name,
            read_credential_binding_hash=read_credential_binding_hash,
            read_principal_hash=identity_hashes["read_principal_hash"],
            read_permission_hash=identity_hashes["read_permission_hash"],
            read_context_hash=identity_hashes["read_context_hash"],
        )
        self.schemas.rebind_odoo_schema_access(
            workspace_id,
            rebound,
            expected_content_hash=current.content_hash,
            expected_read_credential_binding_hash=(
                current.read_credential_binding_hash
            ),
            actor=actor,
        )
        return rebound

    def capture_local_manual(
        self,
        workspace_id: str,
        models: Iterable[SchemaModel],
        *,
        read_credential_binding_hash: str,
        read_identity: OdooReadIdentity | None = None,
        actor: Actor,
    ) -> OdooSchemaCatalog:
        """Store an explicitly unverified schema draft for local work."""

        workspace_state, permitted = self._capture_context(workspace_id, actor=actor)
        _validate_read_credential_binding_hash(read_credential_binding_hash)
        identity_hashes = _validate_read_identity(
            workspace_state,
            read_identity,
            required_models=tuple(sorted(permitted)),
        )
        if workspace_state.odoo_connection_mode is not OdooConnectionMode.LOCAL:
            raise WorkspaceError(
                "A manual schema draft is available only for Local Odoo"
            )
        declared_models = tuple(sorted(models, key=lambda item: item.name))
        self._validate_schema_models(declared_models, permitted)
        return self._store_catalog(
            workspace_state,
            models=declared_models,
            connection_mode=workspace_state.odoo_connection_mode.value,
            database=workspace_state.odoo_database,
            odoo_version="unverified local draft (expected Odoo 19)",
            fingerprint={
                "target_hash": _target_identity_hash(workspace_state),
                "connection_mode": workspace_state.odoo_connection_mode.value,
                "database": workspace_state.odoo_database,
                "odoo_version": "unverified local draft (expected Odoo 19)",
                "snapshot_timestamp": "not captured",
                "module_versions": {},
            },
            origin=SchemaOrigin.LOCAL_MANUAL,
            read_credential_binding_hash=read_credential_binding_hash,
            identity_hashes=identity_hashes,
            actor=actor,
        )

    def _capture_context(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> tuple[WorkspaceState, set[str]]:
        self.authorization.require(
            actor,
            Capability.SCHEMA_DISCOVER,
            workspace_id=workspace_id,
        )
        workspace_state = self.workspaces.get(workspace_id)
        if workspace_state.status is not WorkspaceStatus.REGISTERED:
            raise WorkspaceError(
                "Register the project before capturing Odoo schema"
            )
        if (
            workspace_state.source_mode is SourceMode.FILE
            and self.sources.get_source_selection(workspace_id) is None
        ):
            raise WorkspaceError(
                "Freeze source datasets before capturing Odoo schema"
            )
        permitted = set(workspace_state.intended_models)
        if not permitted:
            raise WorkspaceError(
                "Add at least one permitted technical Odoo model to the project"
            )
        if workspace_state.odoo_connection_mode is None:
            raise WorkspaceError(
                "Configure the Odoo target before capturing schema"
            )
        return workspace_state, permitted

    def _schema_models_from_snapshot(
        self,
        workspace_state: WorkspaceState,
        permitted: set[str],
        snapshot: MetadataSnapshot,
        *,
        discovered: OdooModelCatalog | None = None,
    ) -> tuple[SchemaModel, ...]:
        """Project live metadata into the exact semantic schema contract."""

        model_catalog = discovered
        if model_catalog is None:
            model_catalog = self.schemas.get_odoo_model_catalog(
                workspace_state.workspace_id
            )
        discovered_labels = (
            {model.name: model.label for model in model_catalog.models}
            if model_catalog
            and model_catalog.connection_target_hash
            == _target_identity_hash(workspace_state)
            and model_catalog.policy_hash == ODOO_SOURCE_POLICY_HASH
            else {}
        )
        missing_discovered = permitted - set(discovered_labels)
        if model_catalog and missing_discovered:
            missing = sorted(missing_discovered)[0]
            raise WorkspaceError(
                f"{missing} is no longer in the refreshed Odoo model catalogue; "
                "save the permitted model scope again"
            )
        return tuple(
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
                        stored=field.stored,
                        computed=field.computed,
                        has_inverse=field.has_inverse,
                        related=field.related,
                        translated=field.translated,
                        company_dependent=field.company_dependent,
                        searchable=field.searchable,
                        sortable=field.sortable,
                        exportable=field.exportable,
                        digits=field.digits,
                        currency_field=field.currency_field,
                    )
                    for field_name, field in sorted(model.fields.items())
                ),
                unique_constraints=model.unique_constraints,
            )
            for name, model in sorted(snapshot.models.items())
        )

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
        workspace_state: WorkspaceState,
        *,
        models: tuple[SchemaModel, ...],
        connection_mode: str,
        database: str,
        odoo_version: str,
        fingerprint: Mapping[str, object],
        origin: SchemaOrigin,
        read_credential_binding_hash: str,
        identity_hashes: Mapping[str, str],
        actor: Actor,
    ) -> OdooSchemaCatalog:
        content = {
            "connection_target_hash": str(fingerprint["target_hash"]),
            "policy_hash": ODOO_SOURCE_POLICY_HASH,
            "read_credential_binding_hash": read_credential_binding_hash,
            **identity_hashes,
            "fingerprint": fingerprint,
            "origin": origin.value,
            "models": [asdict(model) for model in models],
        }
        catalog = OdooSchemaCatalog(
            workspace_id=workspace_state.workspace_id,
            policy_hash=ODOO_SOURCE_POLICY_HASH,
            captured_at=datetime.now(timezone.utc),
            captured_by=actor.identity.display_name,
            connection_mode=connection_mode,
            database=database,
            odoo_version=odoo_version,
            models=models,
            content_hash=content_hash(content),
            origin=origin,
            read_credential_binding_hash=read_credential_binding_hash,
            read_principal_hash=identity_hashes["read_principal_hash"],
            read_permission_hash=identity_hashes["read_permission_hash"],
            read_context_hash=identity_hashes["read_context_hash"],
            connection_target_hash=str(fingerprint["target_hash"]),
        )
        self.schemas.save_odoo_schema_catalog(
            workspace_state.workspace_id,
            catalog,
            actor=actor,
        )
        return catalog

    def govern(
        self,
        workspace_id: str,
        *,
        business_keys: Iterable[BusinessKeyDefinition],
        actor: Actor,
    ) -> SchemaGovernance:
        """Confirm explicit natural keys for the current captured schema."""

        self.authorization.require(
            actor,
            Capability.SCHEMA_GOVERN,
            workspace_id=workspace_id,
        )
        workspace_state = self.workspaces.get(workspace_id)
        if (
            workspace_state.source_mode is SourceMode.ODOO
            and self.sources.get_source_selection(workspace_id) is None
        ):
            raise WorkspaceError(
                "Freeze the selected Odoo source records before confirming keys"
            )
        schema = self.schemas.get_odoo_schema_catalog(workspace_id)
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
        previous = self.schemas.get_schema_governance(workspace_id)
        governance = SchemaGovernance(
            governance_id=(
                previous.governance_id if previous else str(uuid4())
            ),
            version=previous.version + 1 if previous else 1,
            workspace_id=workspace_id,
            catalog_hash=schema.content_hash,
            permitted_models=tuple(sorted(models)),
            business_keys=normalized,
            recorded_at=datetime.now(timezone.utc),
            recorded_by=actor.identity.display_name,
        )
        self.schemas.save_schema_governance(
            workspace_id,
            governance,
            actor=actor,
        )
        return governance


def _target_identity_hash(workspace_state: WorkspaceState) -> str:
    return target_identity_hash(
        connection_mode=(
            workspace_state.odoo_connection_mode.value
            if workspace_state.odoo_connection_mode
            else ""
        ),
        base_url=workspace_state.odoo_base_url,
        database=workspace_state.odoo_database,
    )


def _validate_read_credential_binding_hash(value: str) -> None:
    if not _CONTENT_HASH.fullmatch(value):
        raise WorkspaceError("Odoo read credential binding is invalid")


def _validate_read_identity(
    workspace_state: WorkspaceState,
    identity: OdooReadIdentity | None,
    *,
    required_models: tuple[str, ...],
) -> dict[str, str]:
    """Validate remote probe evidence while keeping local sudo metadata honest."""

    empty = {
        "read_principal_hash": "",
        "read_permission_hash": "",
        "read_context_hash": "",
    }
    if workspace_state.odoo_connection_mode is OdooConnectionMode.LOCAL and identity is None:
        return empty
    if identity is None:
        raise WorkspaceError(
            "Verify the remote Odoo read principal before storing metadata"
        )
    if identity.target_hash != _target_identity_hash(workspace_state):
        raise WorkspaceError("Odoo read principal belongs to a different target")
    hashes = {
        "read_principal_hash": identity.principal_hash,
        "read_permission_hash": identity.permission_hash,
        "read_context_hash": identity.context_hash,
    }
    if any(_CONTENT_HASH.fullmatch(value) is None for value in hashes.values()):
        raise WorkspaceError("Odoo read identity evidence is invalid")
    if identity.readable_models != required_models:
        raise WorkspaceError(
            "Odoo read permission probe does not match the required model scope"
        )
    return hashes


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
