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
from ..domain.mapping.create_field_policy import supports_create_default_capture
from ..models import FieldMetadata, OdooReadIdentity, target_identity_hash
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
    OdooSchemaChange,
    OdooSchemaCatalog,
    OdooSchemaRefreshCandidate,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceSelection,
)
from ..workspace_errors import WorkspaceError
from ..domain.serialization import content_hash


_TECHNICAL_MODEL = re.compile(r"^[a-z_][a-z0-9_.]{0,127}$")
_CONTENT_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_SCHEMA_REFRESH_CHANGES = 50


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

    def save_odoo_schema_check(
        self,
        workspace_id: str,
        catalog: OdooSchemaCatalog,
        *,
        expected_content_hash: str,
        expected_read_credential_binding_hash: str,
        actor: Actor,
    ) -> None:
        """Record refresh status without replacing current schema meaning."""
        ...

    def save_odoo_create_defaults(
        self,
        workspace_id: str,
        catalog: OdooSchemaCatalog,
        *,
        expected_content_hash: str,
        expected_read_credential_binding_hash: str,
        actor: Actor,
    ) -> None:
        """Store supplemental create defaults without retiring dependents."""
        ...

    def confirm_odoo_schema_refresh(
        self,
        workspace_id: str,
        catalog: OdooSchemaCatalog,
        *,
        expected_current_content_hash: str,
        expected_candidate_id: str,
        expected_candidate_semantic_hash: str,
        actor: Actor,
    ) -> None:
        """Promote the exact reviewed candidate and retire its dependents."""
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

        catalog = self._validated_live_catalog(
            workspace_id,
            snapshot,
            read_credential_binding_hash=read_credential_binding_hash,
            read_identity=read_identity,
            actor=actor,
        )
        self.schemas.save_odoo_schema_catalog(
            workspace_id,
            catalog,
            actor=actor,
        )
        return catalog

    def check_refresh(
        self,
        workspace_id: str,
        snapshot: MetadataSnapshot,
        *,
        read_credential_binding_hash: str,
        read_identity: OdooReadIdentity | None = None,
        actor: Actor,
    ) -> OdooSchemaCatalog:
        """Check current Odoo details without replacing current schema meaning."""

        candidate_catalog = self._validated_live_catalog(
            workspace_id,
            snapshot,
            read_credential_binding_hash=read_credential_binding_hash,
            read_identity=read_identity,
            actor=actor,
        )
        current = self.schemas.get_odoo_schema_catalog(workspace_id)
        if current is None or current.origin is not SchemaOrigin.LIVE_API:
            raise WorkspaceError(
                "Load verified Odoo details before checking them for changes"
            )
        current_semantic_hash = _schema_semantic_hash(current)
        candidate_semantic_hash = _schema_semantic_hash(candidate_catalog)
        checked_at = candidate_catalog.captured_at

        if current_semantic_hash == candidate_semantic_hash:
            checked = replace(
                current,
                read_credential_binding_hash=(
                    candidate_catalog.read_credential_binding_hash
                ),
                read_principal_hash=candidate_catalog.read_principal_hash,
                read_permission_hash=candidate_catalog.read_permission_hash,
                read_context_hash=candidate_catalog.read_context_hash,
                last_checked_at=checked_at,
                last_checked_by=actor.identity.display_name,
                pending_refresh=None,
            )
        else:
            changes = _schema_refresh_changes(current, candidate_catalog)
            pending = OdooSchemaRefreshCandidate(
                candidate_id=str(uuid4()),
                checked_at=checked_at,
                checked_by=actor.identity.display_name,
                expected_current_content_hash=current.content_hash,
                semantic_hash=candidate_semantic_hash,
                connection_target_hash=candidate_catalog.connection_target_hash,
                policy_hash=candidate_catalog.policy_hash,
                connection_mode=candidate_catalog.connection_mode,
                database=candidate_catalog.database,
                odoo_version=candidate_catalog.odoo_version,
                models=candidate_catalog.models,
                content_hash=candidate_catalog.content_hash,
                origin=candidate_catalog.origin,
                read_credential_binding_hash=(
                    candidate_catalog.read_credential_binding_hash
                ),
                read_principal_hash=candidate_catalog.read_principal_hash,
                read_permission_hash=candidate_catalog.read_permission_hash,
                read_context_hash=candidate_catalog.read_context_hash,
                change_count=len(changes),
                changes=changes[:_MAX_SCHEMA_REFRESH_CHANGES],
            )
            checked = replace(
                current,
                last_checked_at=checked_at,
                last_checked_by=actor.identity.display_name,
                pending_refresh=pending,
            )
        self.schemas.save_odoo_schema_check(
            workspace_id,
            checked,
            expected_content_hash=current.content_hash,
            expected_read_credential_binding_hash=(
                current.read_credential_binding_hash
            ),
            actor=actor,
        )
        return checked

    def refresh_create_defaults(
        self,
        workspace_id: str,
        snapshot: MetadataSnapshot,
        *,
        requested_fields: Mapping[str, tuple[str, ...]],
        read_credential_binding_hash: str,
        read_identity: OdooReadIdentity | None = None,
        actor: Actor,
    ) -> OdooSchemaCatalog:
        """Add exact ``default_get`` evidence without changing schema ownership."""

        workspace_state, permitted = self._capture_context(workspace_id, actor=actor)
        current = self.schemas.get_odoo_schema_catalog(workspace_id)
        if current is None or current.origin is not SchemaOrigin.LIVE_API:
            raise WorkspaceError(
                "Load verified Odoo fields before asking Odoo to decide"
            )
        if current.pending_refresh is not None:
            raise WorkspaceError(
                "Review the checked Odoo field changes before asking Odoo to decide"
            )
        _validate_read_credential_binding_hash(read_credential_binding_hash)
        if read_credential_binding_hash != current.read_credential_binding_hash:
            raise WorkspaceError(
                "The Odoo read key changed; check the Odoo fields again"
            )
        normalized = {
            str(model_name): tuple(dict.fromkeys(field_names))
            for model_name, field_names in requested_fields.items()
            if field_names
        }
        if not normalized or set(normalized) - permitted:
            raise WorkspaceError(
                "The required Odoo default check is outside this migration scope"
            )
        if not snapshot.complete or set(snapshot.models) != set(normalized):
            raise WorkspaceError(
                "Odoo did not return every required field default check"
            )
        if snapshot.fingerprint.target_hash != current.connection_target_hash:
            raise WorkspaceError("Odoo defaults came from a different target")
        if (
            snapshot.fingerprint.connection_mode != current.connection_mode
            or snapshot.fingerprint.database != current.database
            or snapshot.fingerprint.odoo_version != current.odoo_version
        ):
            raise WorkspaceError(
                "The Odoo target details changed; check the Odoo fields again"
            )
        if workspace_state.odoo_connection_mode is not OdooConnectionMode.LOCAL:
            identity_hashes = _validate_read_identity(
                workspace_state,
                read_identity,
                required_models=tuple(sorted(permitted)),
            )
            if (
                identity_hashes["read_principal_hash"]
                != current.read_principal_hash
                or identity_hashes["read_permission_hash"]
                != current.read_permission_hash
                or identity_hashes["read_context_hash"]
                != current.read_context_hash
            ):
                raise WorkspaceError(
                    "The Odoo reader, permissions, or company context changed; "
                    "check the Odoo fields again"
                )

        current_models = {model.name: model for model in current.models}
        replacements: dict[str, dict[str, SchemaField]] = {}
        recovered_count = 0
        for model_name, field_names in normalized.items():
            current_model = current_models.get(model_name)
            returned_model = snapshot.models[model_name]
            current_fields = {
                field.name: field for field in current_model.fields
            } if current_model is not None else {}
            if set(returned_model.fields) != set(field_names):
                raise WorkspaceError(
                    "Odoo did not return the exact required fields requested"
                )
            recovered_fields: dict[str, SchemaField] = {}
            for field_name in field_names:
                current_field = current_fields.get(field_name)
                returned_field = returned_model.fields[field_name]
                if (
                    current_field is None
                    or not _supports_default_refresh(current_field)
                ):
                    raise WorkspaceError(
                        f"{model_name}.{field_name} cannot use an Odoo create default"
                    )
                observed = _schema_field_from_metadata(
                    field_name,
                    returned_field,
                    snapshot.create_defaults.get(model_name),
                )
                if _schema_field_structure(current_field) != _schema_field_structure(
                    observed
                ):
                    raise WorkspaceError(
                        f"{model_name}.{field_name} changed in Odoo; check the "
                        "Odoo fields again"
                    )
                if not observed.create_default_present:
                    raise WorkspaceError(
                        f"Odoo did not return a usable default for "
                        f"{model_name}.{field_name}. Match this field yourself."
                    )
                recovered_fields[field_name] = replace(
                    current_field,
                    create_default_present=True,
                    create_default_value=observed.create_default_value,
                )
                recovered_count += 1
            replacements[model_name] = recovered_fields

        checked_at = datetime.now(timezone.utc)
        updated = replace(
            current,
            models=tuple(
                replace(
                    model,
                    fields=tuple(
                        replacements.get(model.name, {}).get(field.name, field)
                        for field in model.fields
                    ),
                )
                for model in current.models
            ),
            last_checked_at=checked_at,
            last_checked_by=actor.identity.display_name,
            pending_refresh=None,
        )
        self.schemas.save_odoo_create_defaults(
            workspace_id,
            updated,
            expected_content_hash=current.content_hash,
            expected_read_credential_binding_hash=(
                current.read_credential_binding_hash
            ),
            actor=actor,
        )
        if recovered_count < 1:
            raise WorkspaceError("Odoo did not return a usable create default")
        return updated

    def confirm_refresh(
        self,
        workspace_id: str,
        *,
        expected_current_content_hash: str,
        expected_candidate_id: str,
        expected_candidate_semantic_hash: str,
        actor: Actor,
    ) -> OdooSchemaCatalog:
        """Promote one exact checked candidate after explicit confirmation."""

        workspace_state, permitted = self._capture_context(workspace_id, actor=actor)
        current = self.schemas.get_odoo_schema_catalog(workspace_id)
        if current is None or current.pending_refresh is None:
            raise WorkspaceError(
                "Check Odoo for changes again before confirming updated details"
            )
        pending = current.pending_refresh
        if (
            current.content_hash != expected_current_content_hash
            or pending.expected_current_content_hash
            != expected_current_content_hash
            or pending.candidate_id != expected_candidate_id
            or pending.semantic_hash != expected_candidate_semantic_hash
        ):
            raise WorkspaceError(
                "The checked Odoo details changed in another request; check again"
            )
        if (
            pending.connection_target_hash != _target_identity_hash(workspace_state)
            or pending.policy_hash != ODOO_SOURCE_POLICY_HASH
            or {model.name for model in pending.models} != permitted
        ):
            raise WorkspaceError(
                "The checked Odoo details no longer match this migration scope"
            )
        confirmed = OdooSchemaCatalog(
            workspace_id=workspace_id,
            policy_hash=pending.policy_hash,
            captured_at=pending.checked_at,
            captured_by=pending.checked_by,
            connection_mode=pending.connection_mode,
            database=pending.database,
            odoo_version=pending.odoo_version,
            models=pending.models,
            content_hash=pending.content_hash,
            origin=pending.origin,
            read_credential_binding_hash=pending.read_credential_binding_hash,
            read_principal_hash=pending.read_principal_hash,
            read_permission_hash=pending.read_permission_hash,
            read_context_hash=pending.read_context_hash,
            connection_target_hash=pending.connection_target_hash,
            last_checked_at=pending.checked_at,
            last_checked_by=actor.identity.display_name,
            pending_refresh=None,
        )
        self.schemas.confirm_odoo_schema_refresh(
            workspace_id,
            confirmed,
            expected_current_content_hash=expected_current_content_hash,
            expected_candidate_id=expected_candidate_id,
            expected_candidate_semantic_hash=expected_candidate_semantic_hash,
            actor=actor,
        )
        return confirmed

    def _validated_live_catalog(
        self,
        workspace_id: str,
        snapshot: MetadataSnapshot,
        *,
        read_credential_binding_hash: str,
        read_identity: OdooReadIdentity | None,
        actor: Actor,
    ) -> OdooSchemaCatalog:
        """Build one validated live candidate without publishing it."""

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
        return self._build_catalog(
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
        candidate = self._build_catalog(
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
        semantic_access_matches = (
            _schema_semantic_hash(current) == _schema_semantic_hash(candidate)
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
            last_checked_at=candidate.captured_at,
            last_checked_by=actor.identity.display_name,
            pending_refresh=None,
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
                    _schema_field_from_metadata(
                        field_name,
                        field,
                        snapshot.create_defaults.get(name),
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
        catalog = self._build_catalog(
            workspace_state,
            models=models,
            connection_mode=connection_mode,
            database=database,
            odoo_version=odoo_version,
            fingerprint=fingerprint,
            origin=origin,
            read_credential_binding_hash=read_credential_binding_hash,
            identity_hashes=identity_hashes,
            actor=actor,
        )
        self.schemas.save_odoo_schema_catalog(
            workspace_state.workspace_id,
            catalog,
            actor=actor,
        )
        return catalog

    def _build_catalog(
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
        """Build current-shaped evidence without publishing a current pointer."""

        content = {
            "connection_target_hash": str(fingerprint["target_hash"]),
            "policy_hash": ODOO_SOURCE_POLICY_HASH,
            "read_credential_binding_hash": read_credential_binding_hash,
            **identity_hashes,
            "fingerprint": fingerprint,
            "origin": origin.value,
            "models": [asdict(model) for model in models],
        }
        observed_at = datetime.now(timezone.utc)
        return OdooSchemaCatalog(
            workspace_id=workspace_state.workspace_id,
            policy_hash=ODOO_SOURCE_POLICY_HASH,
            captured_at=observed_at,
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
            last_checked_at=observed_at,
            last_checked_by=actor.identity.display_name,
        )

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


def _schema_field_from_metadata(
    field_name: str,
    field: FieldMetadata,
    model_defaults: Mapping[str, object] | None,
) -> SchemaField:
    """Bind only a usable required scalar default to captured field evidence."""

    default_present, default_value = _usable_create_default(
        field,
        model_defaults,
    )
    return SchemaField(
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
        create_default_present=default_present,
        create_default_value=default_value,
    )


def _usable_create_default(
    field: FieldMetadata,
    model_defaults: Mapping[str, object] | None,
) -> tuple[bool, bool | int | float | str | None]:
    """Accept only exact scalar values that can satisfy this required field."""

    if (
        not field.required
        or field.readonly
        or model_defaults is None
        or field.name not in model_defaults
    ):
        return False, None
    value = model_defaults[field.name]
    if field.type == "boolean":
        return (True, value) if isinstance(value, bool) else (False, None)
    if field.type == "integer":
        return (
            (True, value)
            if isinstance(value, int) and not isinstance(value, bool)
            else (False, None)
        )
    if field.type in {"float", "monetary"}:
        return (
            (True, value)
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else (False, None)
        )
    if field.type == "selection":
        codes = {str(item[0]) for item in field.selection}
        return (
            (True, value)
            if isinstance(value, str) and value in codes
            else (False, None)
        )
    if field.type in {"char", "date", "datetime", "html", "text"}:
        return (
            (True, value)
            if isinstance(value, str) and bool(value.strip())
            else (False, None)
        )
    return False, None


def _supports_default_refresh(field: SchemaField) -> bool:
    """Keep supplemental reads aligned with the connector's scalar allowlist."""

    return supports_create_default_capture(field)


def _schema_field_structure(field: SchemaField) -> dict[str, object]:
    """Compare captured field behavior while excluding supplemental defaults."""

    semantics = _schema_field_semantics(field)
    semantics.pop("create_default_present")
    semantics.pop("create_default_value")
    return semantics


def _schema_semantic_hash(catalog: OdooSchemaCatalog) -> str:
    """Hash schema meaning while excluding observation and display metadata."""

    return content_hash(
        {
            "connection_target_hash": catalog.connection_target_hash,
            "policy_hash": catalog.policy_hash,
            "connection_mode": catalog.connection_mode,
            "database": catalog.database,
            "odoo_version": catalog.odoo_version,
            "origin": catalog.origin.value,
            "read_principal_hash": catalog.read_principal_hash,
            "read_permission_hash": catalog.read_permission_hash,
            "read_context_hash": catalog.read_context_hash,
            "models": [
                _schema_model_semantics(model)
                for model in sorted(catalog.models, key=lambda item: item.name)
            ],
        }
    )


def _schema_model_semantics(model: SchemaModel) -> dict[str, object]:
    return {
        "name": model.name,
        "fields": [
            _schema_field_semantics(field)
            for field in sorted(model.fields, key=lambda item: item.name)
        ],
        "unique_constraints": _schema_unique_constraint_semantics(model),
    }


def _schema_unique_constraint_semantics(
    model: SchemaModel,
) -> list[dict[str, object]]:
    return [
        asdict(item)
        for item in sorted(
            model.unique_constraints,
            key=lambda item: (item.name, item.definition),
        )
    ]


def _schema_field_semantics(field: SchemaField) -> dict[str, object]:
    return {
        "name": field.name,
        "type": field.type,
        "required": field.required,
        "readonly": field.readonly,
        "relation": field.relation,
        "relation_field": field.relation_field,
        "selection_codes": [item[0] for item in field.selection],
        "stored": field.stored,
        "computed": field.computed,
        "has_inverse": field.has_inverse,
        "related": field.related,
        "translated": field.translated,
        "company_dependent": field.company_dependent,
        "searchable": field.searchable,
        "sortable": field.sortable,
        "exportable": field.exportable,
        "digits": field.digits,
        "currency_field": field.currency_field,
        "create_default_present": field.create_default_present,
        "create_default_value": field.create_default_value,
    }


def _schema_refresh_changes(
    current: OdooSchemaCatalog,
    candidate: OdooSchemaCatalog,
) -> tuple[OdooSchemaChange, ...]:
    """Describe semantic differences without treating translated labels as meaning."""

    changes: list[OdooSchemaChange] = []

    def add(
        kind: str,
        model: SchemaModel | None,
        description: str,
        field: SchemaField | None = None,
    ) -> None:
        changes.append(
            OdooSchemaChange(
                kind=kind,
                model_name=model.name if model is not None else "",
                model_label=model.label if model is not None else "Odoo target",
                field_name=field.name if field is not None else None,
                field_label=field.label if field is not None else None,
                description=description,
            )
        )

    target_facts = (
        (
            current.connection_target_hash,
            candidate.connection_target_hash,
            "The Odoo target changed.",
        ),
        (current.policy_hash, candidate.policy_hash, "The schema policy changed."),
        (
            current.connection_mode,
            candidate.connection_mode,
            "The Odoo connection mode changed.",
        ),
        (current.database, candidate.database, "The Odoo database changed."),
        (current.odoo_version, candidate.odoo_version, "The Odoo version changed."),
        (current.origin, candidate.origin, "The schema evidence origin changed."),
        (
            current.read_principal_hash,
            candidate.read_principal_hash,
            "The verified Odoo reader changed.",
        ),
        (
            current.read_permission_hash,
            candidate.read_permission_hash,
            "The verified Odoo read permissions changed.",
        ),
        (
            current.read_context_hash,
            candidate.read_context_hash,
            "The verified Odoo company or access context changed.",
        ),
    )
    for previous, observed, description in target_facts:
        if previous != observed:
            add("TARGET_CHANGED", None, description)

    current_models = {model.name: model for model in current.models}
    candidate_models = {model.name: model for model in candidate.models}
    for name in sorted(current_models.keys() - candidate_models.keys()):
        add("MODEL_REMOVED", current_models[name], "This Odoo record type was removed.")
    for name in sorted(candidate_models.keys() - current_models.keys()):
        add("MODEL_ADDED", candidate_models[name], "This Odoo record type was added.")
    for name in sorted(current_models.keys() & candidate_models.keys()):
        previous_model = current_models[name]
        observed_model = candidate_models[name]
        previous_fields = {field.name: field for field in previous_model.fields}
        observed_fields = {field.name: field for field in observed_model.fields}
        for field_name in sorted(previous_fields.keys() - observed_fields.keys()):
            add(
                "FIELD_REMOVED",
                previous_model,
                "This field was removed.",
                previous_fields[field_name],
            )
        for field_name in sorted(observed_fields.keys() - previous_fields.keys()):
            add(
                "FIELD_ADDED",
                observed_model,
                "This field was added.",
                observed_fields[field_name],
            )
        for field_name in sorted(previous_fields.keys() & observed_fields.keys()):
            previous_field = previous_fields[field_name]
            observed_field = observed_fields[field_name]
            previous_semantics = _schema_field_semantics(previous_field)
            observed_semantics = _schema_field_semantics(observed_field)
            if previous_semantics == observed_semantics:
                continue
            changed_facts = [
                label
                for key, label in _FIELD_SEMANTIC_LABELS
                if previous_semantics[key] != observed_semantics[key]
            ]
            add(
                "FIELD_CHANGED",
                observed_model,
                "Field behavior changed: " + ", ".join(changed_facts) + ".",
                observed_field,
            )
        if _schema_unique_constraint_semantics(
            previous_model
        ) != _schema_unique_constraint_semantics(observed_model):
            add(
                "CONSTRAINTS_CHANGED",
                observed_model,
                "The available uniqueness evidence changed.",
            )
    return tuple(changes)


_FIELD_SEMANTIC_LABELS = (
    ("type", "field type"),
    ("required", "required setting"),
    ("readonly", "read-only setting"),
    ("relation", "linked record type"),
    ("relation_field", "linked field"),
    ("selection_codes", "available choice codes"),
    ("stored", "stored behavior"),
    ("computed", "calculated behavior"),
    ("has_inverse", "write-back behavior"),
    ("related", "related-field behavior"),
    ("translated", "translation behavior"),
    ("company_dependent", "company-specific behavior"),
    ("searchable", "search behavior"),
    ("sortable", "sorting behavior"),
    ("exportable", "export behavior"),
    ("digits", "number precision"),
    ("currency_field", "currency field"),
    ("create_default_present", "Odoo create default"),
    ("create_default_value", "Odoo create default value"),
)


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
