"""Read live selection choices without publishing unreviewed schema changes.

Local preparation and candidate storage end before and begin after the Odoo
read. Protected artifacts survive process restarts and are accepted only with
the exact mapping save that references their hash.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from starlette.concurrency import run_in_threadpool

from impodo.application.odoo_read_failures import OdooReadCredentialMissingError
from impodo.application.schema_workspace_service import _schema_field_from_metadata
from impodo.domain.odoo.contracts import MetadataRequest
from impodo.domain.schema.governance import SchemaGovernance
from impodo.domain.shared.access import Actor, Capability
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaOrigin,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.selection_choices import (
    SelectionChoiceCandidate,
    SelectionSchemaRefresh,
    selection_access_binding,
    selection_schema_refresh,
)
from impodo.domain.workspace.workbench import OdooConnectionMode, WorkspaceState
from impodo.web.composition.page_reads import run_local_operation
from impodo.web.composition.target_readers import (
    _read_selection_metadata,
    _selected_local_profile,
)
from impodo.web.context import WebContext
from impodo.web.target_credentials import (
    TargetCredentialRole,
    get_target_credential,
    local_read_credential_binding_hash,
)

_FILENAME = "selection-choices.json"


@dataclass(frozen=True, slots=True)
class _SelectionInputs:
    state: WorkspaceState
    schema: OdooSchemaCatalog
    governance: SchemaGovernance | None
    source_hash: str
    working_version: int | None


def _inputs(context: WebContext, workspace_id: str, actor: Actor) -> _SelectionInputs:
    context.mapping_workspace.authorization.require(
        actor, Capability.MAPPING_EDIT, workspace_id=workspace_id
    )
    state = context.queries.get(workspace_id)
    schema = context.queries.get_odoo_schema_catalog(workspace_id)
    source = context.queries.get_mapping_source_selection(workspace_id)
    working = context.mapping_workspace.mappings.get_mapping_working_draft(workspace_id)
    if (
        schema is None
        or source is None
        or schema.origin is not SchemaOrigin.LIVE_API
        or schema.pending_refresh
    ):
        raise WorkspaceError("Review the current Odoo fields before refreshing choices")
    return _SelectionInputs(
        state,
        schema,
        context.queries.get_schema_governance(workspace_id),
        source.content_hash,
        working.version if working else None,
    )


def _credential(context: WebContext, inputs: _SelectionInputs, actor: Actor):
    credential = get_target_credential(
        context.secret_store,
        context.target_credential_workspace(
            inputs.state.workspace_id, workspace_state=inputs.state, actor=actor
        ),
        TargetCredentialRole.READ,
    )
    if context.readiness_reader is None:
        binding = (
            credential.binding_hash
            if credential
            else (
                local_read_credential_binding_hash(inputs.state)
                if inputs.state.odoo_connection_mode is OdooConnectionMode.LOCAL
                else ""
            )
        )
        if not binding:
            raise OdooReadCredentialMissingError(
                "Enter the checking key to refresh Odoo choices."
            )
        if binding != inputs.schema.read_credential_binding_hash:
            raise WorkspaceError(
                "The checking key changed. Check Odoo access before refreshing choices."
            )
    return credential


async def refresh_selection_choices(
    context: WebContext,
    workspace_id: str,
    model_name: str,
    field_name: str,
    *,
    actor: Actor,
) -> dict[str, object]:
    """Return one live field's choices and an opaque, unaccepted candidate."""

    def prepare():
        inputs = _inputs(context, workspace_id, actor)
        field = next(
            (
                field
                for model in inputs.schema.models
                if model.name == model_name
                for field in model.fields
                if field.name == field_name
            ),
            None,
        )
        if field is None or field.type != "selection":
            raise WorkspaceError("Choose one current Odoo choice field")
        return (
            inputs,
            field,
            _credential(context, inputs, actor),
            _selected_local_profile(context, inputs.state),
        )

    inputs, previous, credential, profile = await run_local_operation(prepare)
    requests = (MetadataRequest(model_name, (field_name,)),)

    def read() -> SchemaField:
        state, schema = inputs.state, inputs.schema
        if context.readiness_reader is not None:
            snapshot, _records = context.readiness_reader(state, requests, ())
        elif credential is None and profile is not None:
            snapshot, _records = context.local_odoo_reader.get_preflight_snapshots(
                state, profile, requests, ()
            )
        elif credential is not None:
            identity = context.read_identity_probe(
                state,
                credential.secret,
                tuple(sorted(model.name for model in schema.models)),
            )
            if (
                identity.target_hash,
                credential.binding_hash,
                identity.principal_hash,
                identity.permission_hash,
                identity.context_hash,
            ) != selection_access_binding(schema):
                raise WorkspaceError(
                    "Odoo access changed. Review the Odoo fields before refreshing choices."
                )
            snapshot = _read_selection_metadata(state, credential.secret, requests)
        else:
            raise WorkspaceError(
                "Choose the local Odoo configuration before refreshing choices."
            )
        fingerprint = snapshot.fingerprint
        if (
            not snapshot.complete
            or fingerprint.target_hash != schema.connection_target_hash
            or fingerprint.connection_mode != schema.connection_mode
            or fingerprint.database != schema.database
            or fingerprint.odoo_version != schema.odoo_version
        ):
            raise WorkspaceError(
                "The Odoo target changed. Review its fields before refreshing choices."
            )
        model = snapshot.models.get(model_name)
        metadata = model.fields.get(field_name) if model else None
        if metadata is None:
            raise WorkspaceError(
                "This Odoo field is no longer available. Review the Odoo fields."
            )
        field = _schema_field_from_metadata(field_name, metadata, None)
        structure = replace(
            field,
            label=previous.label,
            selection=previous.selection,
            create_default_present=previous.create_default_present,
            create_default_value=previous.create_default_value,
        )
        if structure != previous:
            raise WorkspaceError(
                "This Odoo field changed beyond its choices. Review the Odoo fields."
            )
        return field

    field = await run_in_threadpool(read)
    candidate = SelectionChoiceCandidate(
        workspace_id,
        inputs.schema.content_hash,
        inputs.governance.content_hash if inputs.governance else "",
        selection_access_binding(inputs.schema),
        inputs.source_hash,
        inputs.working_version,
        inputs.state.revision,
        model_name,
        field_name,
        field.selection,
        datetime.now(UTC),
    )

    def publish():
        current = _inputs(context, workspace_id, actor)
        _credential(context, current, actor)
        selection_schema_refresh(
            current.schema,
            current.governance,
            (candidate,),
            source_hash=current.source_hash,
            working_version=current.working_version,
            workspace_revision=current.state.revision,
            actor_name=actor.identity.display_name,
        )
        candidate_id = str(uuid4())
        data = json.dumps(
            asdict(candidate),
            default=lambda value: value.isoformat(),
            ensure_ascii=False,
        ).encode("utf-8")
        if len(data) > 512_000:
            raise WorkspaceError("Odoo returned too many choices for this field")
        context.artifacts.write_report(workspace_id, candidate_id, _FILENAME, data)
        return f"{candidate_id}:{hashlib.sha256(data).hexdigest()}"

    reference = await run_local_operation(publish)
    previous_codes = {code for code, _label in previous.selection}
    current_codes = {code for code, _label in field.selection}
    return {
        "previous_target_choices": [{"value": code, "label": label} for code, label in previous.selection],
        "target_choices": [
            {"value": code, "label": label} for code, label in field.selection
        ],
        "target_checked_at": candidate.checked_at.isoformat(),
        "target_choices_reused": False,
        "selection_refresh": reference,
        "selection_added": len(current_codes - previous_codes),
        "selection_removed": len(previous_codes - current_codes),
    }


def load_selection_refresh(
    context: WebContext,
    workspace_id: str,
    raw_references: str,
    *,
    actor: Actor,
) -> SelectionSchemaRefresh | None:
    """Verify protected candidates and current access before a mapping command."""
    if not raw_references or raw_references == "[]":
        return None
    try:
        references = json.loads(raw_references)
        if not isinstance(references, list) or not 1 <= len(references) <= 100:
            raise ValueError
        candidates = []
        for reference in references:
            candidate_id, digest = reference.split(":")
            if str(UUID(candidate_id)) != candidate_id or len(digest) != 64:
                raise ValueError
            with context.artifacts.materialize_report(
                workspace_id, candidate_id, _FILENAME
            ) as path:
                if path.stat().st_size > 512_000:
                    raise ValueError
                data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError
            payload = json.loads(data)
            payload["checked_at"] = datetime.fromisoformat(payload["checked_at"])
            payload["access_binding"] = tuple(payload["access_binding"])
            payload["choices"] = tuple(tuple(item) for item in payload["choices"])
            candidates.append(SelectionChoiceCandidate(**payload))
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as error:
        raise WorkspaceError(
            "The refreshed choices are unavailable. Refresh them again."
        ) from error
    inputs = _inputs(context, workspace_id, actor)
    _credential(context, inputs, actor)
    return selection_schema_refresh(
        inputs.schema,
        inputs.governance,
        tuple(candidates),
        source_hash=inputs.source_hash,
        working_version=inputs.working_version,
        workspace_revision=inputs.state.revision,
        actor_name=actor.identity.display_name,
    )
