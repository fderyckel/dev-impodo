"""Select project-bound read-only target adapters for browser workflows.

Stage C helpers capture schema/model information. For Stage H,
``_read_readiness_snapshots`` receives the already bounded planner requests and
chooses either the fixed local Odoo-shell reader, the closed remote JSON-2
reader, or an injected test reader. Credentials remain in the web composition
boundary and are never passed into the preflight domain or report.
"""

from __future__ import annotations

from collections import Counter

from starlette.concurrency import run_in_threadpool

from ..artifacts import ArtifactStoreError
from ..adapters.odoo_source_capture import Json2OdooSourceCapture
from ..connectors import (
    Json2Config,
    Json2ReadConnector,
    MetadataRequest,
    MetadataSnapshot,
    RecordRequest,
    RecordSnapshot,
    target_record_read_config,
)
from ..local_stack import LocalStackProfile
from ..domain.schema.governance import BusinessKeyDefinition
from ..models import OdooReadIdentity, TargetFingerprint, target_identity_hash
from ..domain.odoo_source_policy import ODOO_SOURCE_POLICY_HASH
from ..projects import MigrationProject, OdooConnectionMode, ProjectError, SourceMode
from ..reference_keys import standard_reference_key
from ..secrets import SecretStoreError
from ..source_snapshot_io import (
    load_source_snapshot_table,
    validate_snapshot_for_dataset,
)
from ..workspace_contracts import OdooModelCatalog, SchemaField, SchemaOrigin
from ..workspace_errors import WorkspaceError
from .constants import (
    VALUE_MATCH_MAX_SOURCE_CHOICES,
    VALUE_MATCH_MAX_TARGET_CHOICES,
)
from .context import WebContext
from .target_credentials import (
    TargetCredentialRole,
    get_target_credential,
    local_read_credential_binding_hash,
)


class LocalOdooRecoveryRequired(WorkspaceError):
    """Raised when a live local Odoo read needs a matching session profile."""


def _target_json2_config(
    project: MigrationProject,
    api_key: str,
) -> Json2Config:
    """Build the one archived-inclusive context for target-side reads."""

    if project.odoo_connection_mode is None:
        raise ProjectError("Configure the Odoo target before reading it")
    return target_record_read_config(
        Json2Config(
            base_url=project.odoo_base_url,
            database=project.odoo_database,
            api_key=api_key,
            connection_mode=project.odoo_connection_mode.value,
        )
    )


def _source_capture_reader(
    project: MigrationProject,
    api_key: str,
) -> Json2OdooSourceCapture:
    """Build the one governed JSON-2 business-record capture adapter."""

    if project.odoo_connection_mode is None:
        raise ProjectError("Configure the Odoo target before source capture")
    return Json2OdooSourceCapture(
        Json2Config(
            base_url=project.odoo_base_url,
            database=project.odoo_database,
            api_key=api_key,
            connection_mode=project.odoo_connection_mode.value,
        )
    )


def _test_connection(
    project: MigrationProject,
    api_key: str,
) -> TargetFingerprint:
    if project.odoo_connection_mode is None:
        raise ProjectError("Choose Local Odoo or Remote Odoo")
    connector = Json2ReadConnector(_target_json2_config(project, api_key))
    metadata = connector.get_model_metadata(
        (MetadataRequest(model="res.partner", fields=("id",)),)
    )
    return metadata.fingerprint


def _probe_read_identity(
    project: MigrationProject,
    api_key: str,
    models: tuple[str, ...],
) -> OdooReadIdentity:
    """Run the fixed remote principal/context/model-read probe."""

    if project.odoo_connection_mode is None:
        raise ProjectError("Configure the Odoo target before identity probing")
    connector = Json2ReadConnector(_target_json2_config(project, api_key))
    return connector.probe_read_identity(models)


def _selected_local_profile(
    context: WebContext,
    project: MigrationProject,
) -> LocalStackProfile | None:
    """Return the session-bound profile only when it matches this target."""

    if project.odoo_connection_mode is not OdooConnectionMode.LOCAL:
        return None
    status = context.local_stack.get(project.project_id)
    profile = status.profile
    if profile is None:
        return None
    if profile.base_url.rstrip("/") != project.odoo_base_url.rstrip("/"):
        raise LocalOdooRecoveryRequired(
            "The selected odoo.conf points to "
            f"{profile.base_url}, but this project targets "
            f"{project.odoo_base_url}. Choose the matching configuration or "
            "correct the project target."
        )
    if (
        profile.database_hint
        and project.odoo_database
        and profile.database_hint != project.odoo_database
    ):
        raise LocalOdooRecoveryRequired(
            "The selected odoo.conf points to database "
            f"{profile.database_hint}, but this project targets "
            f"{project.odoo_database}. Choose the matching configuration."
        )
    return profile


def _missing_schema_reader_message(project: MigrationProject) -> str:
    if project.odoo_connection_mode is OdooConnectionMode.LOCAL:
        return (
            "Local mode does not require an API key. Choose and validate "
            "odoo.conf on this page before loading models or fields."
        )
    return "No API key is stored for this exact remote Odoo target."


def _read_schema(project: MigrationProject, api_key: str) -> MetadataSnapshot:
    """Read all fields once per explicitly permitted Odoo model."""

    if project.odoo_connection_mode is None:
        raise ProjectError("Configure the Odoo target before schema capture")
    if not project.intended_models:
        raise ProjectError("Add at least one permitted technical Odoo model")
    connector = Json2ReadConnector(_target_json2_config(project, api_key))
    return connector.get_model_metadata(
        tuple(
            MetadataRequest(
                model=model,
                fields=(),
                all_fields=True,
                include_unique_constraints=True,
            )
            for model in project.intended_models
        )
    )


def _read_model_catalog(
    project: MigrationProject,
    api_key: str,
) -> RecordSnapshot:
    """Read lightweight persistent-model choices from the exact Odoo target."""

    if project.odoo_connection_mode is None:
        raise ProjectError("Configure the Odoo target before model discovery")
    connector = Json2ReadConnector(_target_json2_config(project, api_key))
    return connector.get_records(
        (
            RecordRequest(
                model="ir.model",
                fields=(
                    "name",
                    "model",
                    "abstract",
                    "transient",
                    "modules",
                    "state",
                ),
                domain=(
                    ("abstract", "=", False),
                    ("transient", "=", False),
                ),
            ),
        )
    )


async def _refresh_model_catalog(
    context: WebContext,
    project: MigrationProject,
) -> OdooModelCatalog:
    """Refresh persistent model choices through the configured read-only target."""

    local_profile = _selected_local_profile(context, project)
    credential = get_target_credential(
        context.secret_store,
        project,
        TargetCredentialRole.READ,
    )
    if local_profile is not None and credential is None:
        snapshot = await run_in_threadpool(
            context.local_odoo_reader.get_model_catalog,
            project,
            local_profile,
        )
        read_credential_binding_hash = local_read_credential_binding_hash(project)
        read_identity = None
    else:
        if credential is None:
            raise WorkspaceError(_missing_schema_reader_message(project))
        read_identity = await run_in_threadpool(
            context.read_identity_probe,
            project,
            credential.secret,
            ("ir.model",),
        )
        snapshot = await run_in_threadpool(
            context.model_catalog_reader,
            project,
            credential.secret,
        )
        read_credential_binding_hash = credential.binding_hash
    catalog = context.schema_workspace.discover_models(
        project.project_id,
        snapshot,
        read_credential_binding_hash=read_credential_binding_hash,
        read_identity=read_identity,
        actor=context.actor,
    )
    if local_profile is not None and credential is None:
        context.local_stack.mark_metadata_ready(
            project.project_id,
            database=catalog.database,
            odoo_version=catalog.odoo_version,
            model_count=len(catalog.models),
        )
    return catalog


def _existing_catalog_model(
    context: WebContext,
    project: MigrationProject,
    model_name: str,
) -> str:
    """Require one model advertised by the current exact Odoo target."""

    catalog = context.queries.get_odoo_model_catalog(project.project_id)
    if catalog is None:
        raise WorkspaceError(
            "Show the available Odoo record types before choosing one"
        )
    expected_target_hash = target_identity_hash(
        connection_mode=(
            project.odoo_connection_mode.value
            if project.odoo_connection_mode is not None
            else ""
        ),
        base_url=project.odoo_base_url,
        database=project.odoo_database,
    )
    if (
        catalog.connection_target_hash != expected_target_hash
        or catalog.policy_hash != ODOO_SOURCE_POLICY_HASH
    ):
        raise WorkspaceError(
            "The saved Odoo record list belongs to a different target; "
            "refresh it before choosing a record type"
        )
    selected = model_name.strip()
    if selected not in {model.name for model in catalog.models}:
        raise WorkspaceError(
            "Choose an existing Odoo record type from the loaded list"
        )
    return selected


def _read_readiness_snapshots(
    context: WebContext,
    project: MigrationProject,
    metadata_requests: tuple[MetadataRequest, ...],
    record_requests: tuple[RecordRequest, ...],
) -> tuple[MetadataSnapshot, RecordSnapshot]:
    """Read one consistent snapshot using the project's configured boundary.

    Local mode validates a selected ``odoo.conf`` and delegates the exact
    requests to ``LocalOdooMetadataReader``. Remote mode retrieves the API key
    and exposes only ``fields_get``/``search_read`` through
    ``Json2ReadConnector``. This function does not widen planner domains.
    """

    if project.source_mode is SourceMode.ODOO:
        return _read_pinned_odoo_snapshots(
            context,
            project,
            metadata_requests,
            record_requests,
        )

    local_profile = _selected_local_profile(context, project)
    if (
        context.readiness_reader is not None
        and project.odoo_connection_mode is OdooConnectionMode.LOCAL
    ):
        return context.readiness_reader(
            project,
            metadata_requests,
            record_requests,
        )
    if project.odoo_connection_mode is OdooConnectionMode.LOCAL:
        if local_profile is None:
            raise LocalOdooRecoveryRequired(
                "Choose and validate the matching local odoo.conf before "
                "checking data."
            )
        schema = context.queries.get_odoo_schema_catalog(project.project_id)
        related_models = tuple(
            sorted(
                {
                    field.relation
                    for model in (schema.models if schema is not None else ())
                    for field in model.fields
                    if field.relation
                }
            )
        )
        return context.local_odoo_reader.get_preflight_snapshots(
            project,
            local_profile,
            metadata_requests,
            record_requests,
            related_models=related_models,
        )
    credential = get_target_credential(
        context.secret_store,
        project,
        TargetCredentialRole.READ,
    )
    if credential is None:
        raise SecretStoreError(
            "Enter the Odoo read API key for this remote target before checking data."
        )
    if project.odoo_connection_mode is None:
        raise WorkspaceError("Configure the Odoo target before checking data")
    probe_models = tuple(
        sorted(
            {
                *(request.model for request in metadata_requests),
                *(request.model for request in record_requests),
            }
        )
    )
    identity = context.read_identity_probe(
        project,
        credential.secret,
        probe_models,
    )
    schema = context.queries.get_odoo_schema_catalog(project.project_id)
    if schema is None or not schema.read_principal_hash:
        raise WorkspaceError(
            "Recapture the Odoo schema with verified read-principal evidence "
            "before checking data"
        )
    if (
        identity.principal_hash != schema.read_principal_hash
        or identity.context_hash != schema.read_context_hash
    ):
        raise WorkspaceError(
            "The Odoo read principal or context changed; recapture the schema "
            "before checking data"
        )
    if context.readiness_reader is not None:
        return context.readiness_reader(
            project,
            metadata_requests,
            record_requests,
        )
    connector = Json2ReadConnector(
        _target_json2_config(project, credential.secret)
    )
    metadata = connector.get_model_metadata(metadata_requests)
    records = connector.get_records(record_requests)
    return metadata, records


def _read_pinned_odoo_snapshots(
    context: WebContext,
    project: MigrationProject,
    metadata_requests: tuple[MetadataRequest, ...],
    record_requests: tuple[RecordRequest, ...],
) -> tuple[MetadataSnapshot, RecordSnapshot]:
    """Use the exact capture credential and context for pinned-ID comparison."""

    credential = get_target_credential(
        context.secret_store,
        project,
        TargetCredentialRole.READ,
    )
    if credential is None:
        raise SecretStoreError(
            "Save the Odoo read API key before comparing captured records."
        )
    if project.odoo_connection_mode is None:
        raise WorkspaceError("Configure the Odoo target before comparing records")
    schema = context.queries.get_odoo_schema_catalog(project.project_id)
    if schema is None:
        raise WorkspaceError(
            "Refresh the captured Odoo fields before comparing records"
        )
    probe_models = tuple(sorted(item.name for item in schema.models))
    identity = context.read_identity_probe(
        project,
        credential.secret,
        probe_models,
    )
    if (
        identity.target_hash != schema.connection_target_hash
        or identity.principal_hash != schema.read_principal_hash
        or identity.permission_hash != schema.read_permission_hash
        or identity.context_hash != schema.read_context_hash
        or identity.readable_models != probe_models
    ):
        raise WorkspaceError(
            "The Odoo connection or access context changed. Refresh the captured "
            "records before comparing."
        )
    if context.readiness_reader is not None:
        return context.readiness_reader(
            project,
            metadata_requests,
            record_requests,
        )
    connector = Json2ReadConnector(
        _target_json2_config(project, credential.secret)
    )
    return (
        connector.get_model_metadata(metadata_requests),
        connector.get_records(record_requests),
    )


def _source_value_choices(
    context: WebContext,
    project_id: str,
    dataset_id: str,
    source_column_key: str,
) -> tuple[dict[str, object], ...]:
    """Count every non-empty source choice from one frozen physical column."""

    selection = context.queries.get_source_selection(project_id)
    dataset = next(
        (
            item
            for item in (selection.datasets if selection else ())
            if item.dataset_id == dataset_id
        ),
        None,
    )
    if dataset is None:
        raise WorkspaceError(
            "Value matching is available for original frozen datasets"
        )
    column = next(
        (
            item
            for item in dataset.columns
            if item.stable_key == source_column_key
        ),
        None,
    )
    if column is None:
        raise WorkspaceError("Choose one current source column")
    selection = context.queries.get_source_selection(project_id)
    if selection is None:
        raise WorkspaceError("Frozen source evidence is incomplete")
    snapshot = next(
        (
            item
            for item in context.queries.get_current_source_snapshots(project_id)
            if item.dataset_id == dataset.dataset_id
        ),
        None,
    )
    if snapshot is None:
        raise WorkspaceError("Frozen source snapshot is incomplete")
    try:
        validate_snapshot_for_dataset(selection, dataset, snapshot)
        with context.artifacts.materialize_source_snapshot(
            project_id,
            snapshot.parquet_storage_key,
            expected_sha256=snapshot.parquet_sha256,
        ) as path:
            table = load_source_snapshot_table(path, snapshot)
    except (ArtifactStoreError, OSError, ValueError) as error:
        raise WorkspaceError(
            "The frozen source snapshot could not be verified"
        ) from error
    expected_hash = dataset.source_evidence_hash
    if table.content_hash != expected_hash:
        raise WorkspaceError("Stored source content changed after selection")
    counts = Counter(
        str(row.values.get(column.source_name)).strip()
        for row in table.rows
        if row.values.get(column.source_name) is not None
        and str(row.values.get(column.source_name)).strip()
    )
    if len(counts) > VALUE_MATCH_MAX_SOURCE_CHOICES:
        raise WorkspaceError(
            "This column has too many distinct choices for quick matching"
        )
    return tuple(
        {"value": value, "count": count}
        for value, count in sorted(
            counts.items(),
            key=lambda item: item[0].casefold(),
        )
    )


def _relationship_value_choices(
    context: WebContext,
    project: MigrationProject,
    schema,
    field: SchemaField,
    key: BusinessKeyDefinition,
) -> tuple[tuple[dict[str, str], ...], tuple[str, ...]]:
    """Read existing Odoo choices once and expose only portable key values."""

    if schema.origin is not SchemaOrigin.LIVE_API:
        raise WorkspaceError(
            "Capture the live Odoo schema before loading existing choices"
        )
    if len(key.key_fields) != 1 or key.scope_fields:
        raise WorkspaceError(
            "Quick matching currently supports one Odoo key without scope"
        )
    related_model = next(
        (item for item in schema.models if item.name == field.relation),
        None,
    )
    standard_key = standard_reference_key(field.relation or "")
    uses_standard_key = bool(
        standard_key is not None
        and standard_key.key_fields == key.key_fields
        and standard_key.scope_fields == key.scope_fields
    )
    if related_model is None and not uses_standard_key:
        raise WorkspaceError("Capture the related Odoo model before matching")
    key_field = key.key_fields[0]
    field_by_name = (
        {item.name: item for item in related_model.fields}
        if related_model is not None
        else {}
    )
    if key_field not in field_by_name and not uses_standard_key:
        raise WorkspaceError("The confirmed Odoo key is no longer available")
    if (
        key_field in field_by_name
        and field_by_name[key_field].type not in {"char", "text", "selection"}
    ):
        raise WorkspaceError(
            "Quick matching currently supports text-based Odoo keys"
        )
    available_fields = set(field_by_name)
    display_field = (
        standard_key.display_field
        if uses_standard_key and standard_key is not None
        else ("name" if "name" in available_fields else key_field)
    )
    requested_fields = tuple(dict.fromkeys((key_field, display_field)))
    _metadata, snapshot = _read_readiness_snapshots(
        context,
        project,
        (),
        (
            RecordRequest(
                model=field.relation,
                fields=requested_fields,
            ),
        ),
    )
    expected_target_hash = target_identity_hash(
        connection_mode=(
            project.odoo_connection_mode.value
            if project.odoo_connection_mode is not None
            else ""
        ),
        base_url=project.odoo_base_url,
        database=project.odoo_database,
    )
    if snapshot.fingerprint.target_hash != expected_target_hash:
        raise WorkspaceError("Odoo choices came from a different target")
    records = snapshot.records.get(field.relation, ())
    if len(records) > VALUE_MATCH_MAX_TARGET_CHOICES:
        raise WorkspaceError(
            "This Odoo model has too many records for quick matching"
        )
    by_key: dict[str, list[object]] = {}
    for record in records:
        raw_key = record.values.get(key_field)
        if raw_key is None or str(raw_key).strip() == "":
            continue
        by_key.setdefault(str(raw_key), []).append(record)
    ambiguous = tuple(
        sorted(
            (value for value, matches in by_key.items() if len(matches) != 1),
            key=str.casefold,
        )
    )
    choices: list[dict[str, str]] = []
    for value, matches in by_key.items():
        if len(matches) != 1:
            continue
        label_value = matches[0].values.get(display_field)
        label = str(label_value or value)
        if display_field != key_field and label != value:
            label = f"{label} ({value})"
        choices.append({"value": value, "label": label})
    return (
        tuple(sorted(choices, key=lambda item: item["label"].casefold())),
        ambiguous,
    )
