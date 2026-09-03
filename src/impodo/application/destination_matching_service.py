"""Build bounded, destination-aware matching evidence without Odoo writes."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Mapping, Protocol, Sequence

from impodo.domain.odoo.contracts import (
    MetadataRequest,
    MetadataSnapshot,
    RecordRequest,
    RecordSnapshot,
    metadata_snapshot_payload,
    record_snapshot_payload,
)
from impodo.domain.serialization import content_hash
from impodo.domain.odoo_provenance import OdooOriginBatch
from impodo.domain.shared.models import OdooReadIdentity
from impodo.domain.shared.models import target_record_binding_hash
from impodo.domain.source_binding import OdooSourceBinding
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SourceSelection,
)
from impodo.domain.workspace.destination_matching import (
    DestinationMatchPlan,
    DestinationModelMatch,
    DestinationRelationshipMatch,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import (
    WorkspaceState,
    transfer_destination_identity_hash,
    transfer_destination_workspace,
)


DESTINATION_MATCH_MAX_DISTINCT_KEYS = 1_000
DESTINATION_MATCH_RECORD_LIMIT = 1_001
_TEXT_KEY_TYPES = frozenset({"char", "text", "selection"})


class DestinationSourceValueReader(Protocol):
    def source_value_choices(
        self,
        workspace_id: str,
        dataset_id: str,
        source_column_key: str,
    ) -> tuple[dict[str, object], ...]: ...

    def source_key_rows(
        self,
        workspace_id: str,
        dataset_id: str,
        source_column_key: str,
    ) -> tuple[str | None, ...]: ...


class DestinationMatchReader(Protocol):
    def __call__(
        self,
        workspace_state: WorkspaceState,
        api_key: str,
        metadata_requests: tuple[MetadataRequest, ...],
        record_requests: tuple[RecordRequest, ...],
    ) -> tuple[MetadataSnapshot, RecordSnapshot]: ...


@dataclass(frozen=True, slots=True)
class DestinationMatchKeyChoice:
    dataset_id: str
    source_column_key: str


@dataclass(frozen=True, slots=True)
class _PreparedModel:
    dataset_id: str
    dataset_name: str
    model: str
    model_label: str
    source_column_key: str
    key_field: str
    key_field_label: str
    source_row_count: int
    source_counts: Counter[str]
    source_fields: tuple[SchemaField, ...]


@dataclass(frozen=True, slots=True)
class _PreparedRelationship:
    owner: _PreparedModel
    field: SchemaField
    related: _PreparedModel
    inverse_field: str | None


class DestinationMatchingService:
    """Check same-name destination fields and exact natural-key matches."""

    def __init__(self, source_values: DestinationSourceValueReader) -> None:
        self._source_values = source_values

    def check(
        self,
        workspace: WorkspaceState,
        selection: SourceSelection,
        source_schema: OdooSchemaCatalog,
        choices: Sequence[DestinationMatchKeyChoice],
        *,
        api_key: str,
        credential_binding_hash: str,
        read_identity: OdooReadIdentity,
        reader: DestinationMatchReader,
        recorded_by: str,
        source_origins: Mapping[str, tuple[OdooOriginBatch, ...]] | None = None,
    ) -> DestinationMatchPlan:
        """Return a current plan from one bounded metadata/record read."""

        if not workspace.destination_verified:
            raise WorkspaceError("Verify the destination Odoo connection first")
        selected = {item.dataset_id: item.source_column_key for item in choices}
        if len(selected) != len(choices):
            raise WorkspaceError("Choose one matching field for each source table")
        if set(selected) != {item.dataset_id for item in selection.datasets}:
            raise WorkspaceError("Choose one matching field for each source table")

        source_models = {item.name: item for item in source_schema.models}
        selected_model_names = {
            dataset.source.model
            for dataset in selection.datasets
            if isinstance(dataset.source, OdooSourceBinding)
        }
        prepared: list[_PreparedModel] = []
        for dataset in selection.datasets:
            if not isinstance(dataset.source, OdooSourceBinding):
                raise WorkspaceError(
                    "Destination matching currently requires frozen Odoo source tables"
                )
            source_model = source_models.get(dataset.source.model)
            if source_model is None:
                raise WorkspaceError(
                    f"Refresh the source fields for {dataset.source.model} first"
                )
            source_column_key = selected[dataset.dataset_id]
            source_column = next(
                (
                    item
                    for item in dataset.columns
                    if item.stable_key == source_column_key
                ),
                None,
            )
            if source_column is None:
                raise WorkspaceError(
                    f"Choose a current matching field for {dataset.name}"
                )
            key_field = next(
                (
                    item
                    for item in source_model.fields
                    if item.name == source_column.source_name
                ),
                None,
            )
            if key_field is None or key_field.type not in _TEXT_KEY_TYPES:
                raise WorkspaceError(
                    f"Choose a text matching field for {dataset.name}"
                )
            raw_choices = self._source_values.source_value_choices(
                workspace.workspace_id,
                dataset.dataset_id,
                source_column.stable_key,
            )
            if len(raw_choices) > DESTINATION_MATCH_MAX_DISTINCT_KEYS:
                raise WorkspaceError(
                    f"{dataset.name} has too many distinct matching values for this stage"
                )
            source_counts: Counter[str] = Counter()
            for item in raw_choices:
                value = _match_value(item.get("value"))
                count = item.get("count")
                if (
                    not value
                    or not isinstance(count, int)
                    or isinstance(count, bool)
                    or count <= 0
                ):
                    raise WorkspaceError(
                        f"The frozen matching values for {dataset.name} are invalid"
                    )
                source_counts[value] += count
            if sum(source_counts.values()) > dataset.row_count:
                raise WorkspaceError(
                    f"The frozen matching counts for {dataset.name} are inconsistent"
                )
            selected_source_names = {
                column.source_name for column in dataset.columns
            }
            prepared.append(
                _PreparedModel(
                    dataset_id=dataset.dataset_id,
                    dataset_name=dataset.name,
                    model=dataset.source.model,
                    model_label=source_model.label,
                    source_column_key=source_column.stable_key,
                    key_field=key_field.name,
                    key_field_label=key_field.label,
                    source_row_count=dataset.row_count,
                    source_counts=source_counts,
                    source_fields=tuple(
                        field
                        for field in source_model.fields
                        if field.name in selected_source_names
                        or (
                            field.type in {"many2one", "many2many"}
                            and field.relation in selected_model_names
                            and field.related is not True
                            and field.company_dependent is False
                            and field.exportable is True
                        )
                    ),
                )
            )

        model_names = tuple(sorted(item.model for item in prepared))
        if len(model_names) != len(set(model_names)):
            raise WorkspaceError("Each Odoo record type must have one frozen source table")
        destination = replace(
            transfer_destination_workspace(workspace),
            intended_models=model_names,
        )
        expected_target_hash = transfer_destination_identity_hash(workspace)
        if (
            credential_binding_hash
            != workspace.destination_verified_credential_binding_hash
            or read_identity.target_hash != expected_target_hash
            or read_identity.principal_hash
            != workspace.destination_verified_read_principal_hash
            or read_identity.readable_models != model_names
        ):
            raise WorkspaceError(
                "The destination access changed; verify the destination connection again"
            )

        metadata_requests = tuple(
            MetadataRequest(
                model=item.model,
                fields=tuple(sorted(field.name for field in item.source_fields)),
                include_unique_constraints=True,
            )
            for item in sorted(prepared, key=lambda current: current.model)
        )
        record_requests = tuple(
            RecordRequest(
                model=item.model,
                fields=(item.key_field,),
                domain=((item.key_field, "in", tuple(sorted(item.source_counts))),),
                limit=DESTINATION_MATCH_RECORD_LIMIT,
            )
            for item in sorted(prepared, key=lambda current: current.model)
            if item.source_counts
        )
        metadata, records = reader(
            destination,
            api_key,
            metadata_requests,
            record_requests,
        )
        for snapshot in (metadata, records):
            if (
                not snapshot.complete
                or snapshot.fingerprint.target_hash != expected_target_hash
                or not snapshot.fingerprint.odoo_version.startswith("19.")
            ):
                raise WorkspaceError(
                    "The destination matching read returned a different Odoo target"
                )

        model_results: list[DestinationModelMatch] = []
        destination_counts: dict[str, Counter[str]] = {}
        for item in sorted(prepared, key=lambda current: current.model):
            result, counts = self._result(item, metadata, records)
            model_results.append(result)
            destination_counts[item.model] = counts
        relationships = self._relationship_results(
            workspace.workspace_id,
            tuple(prepared),
            source_schema,
            source_origins or {},
            destination_counts,
        )
        return DestinationMatchPlan(
            workspace_id=workspace.workspace_id,
            source_selection_hash=selection.content_hash,
            source_schema_hash=source_schema.content_hash,
            destination_target_hash=expected_target_hash,
            destination_credential_binding_hash=credential_binding_hash,
            destination_read_principal_hash=read_identity.principal_hash,
            destination_read_permission_hash=read_identity.permission_hash,
            destination_read_context_hash=read_identity.context_hash,
            destination_schema_snapshot_hash=content_hash(
                metadata_snapshot_payload(metadata)
            ),
            destination_record_snapshot_hash=content_hash(
                record_snapshot_payload(records)
            ),
            model_matches=tuple(model_results),
            recorded_at=datetime.now(UTC),
            recorded_by=recorded_by,
            relationship_matches=relationships,
        )

    def _result(
        self,
        item: _PreparedModel,
        metadata: MetadataSnapshot,
        records: RecordSnapshot,
    ) -> tuple[DestinationModelMatch, Counter[str]]:
        destination_model = metadata.models.get(item.model)
        compatible: list[str] = []
        missing: list[str] = []
        incompatible: list[str] = []
        destination_fields = destination_model.fields if destination_model else {}
        for source_field in item.source_fields:
            destination_field = destination_fields.get(source_field.name)
            if destination_field is None:
                missing.append(source_field.name)
            elif (
                destination_field.type != source_field.type
                or destination_field.relation != source_field.relation
                or destination_field.readonly
                or (destination_field.required and not source_field.required)
            ):
                incompatible.append(source_field.name)
            else:
                compatible.append(source_field.name)

        target_rows = records.records.get(item.model, ())
        destination_counts: Counter[str] = Counter()
        source_keys = set(item.source_counts)
        for row in target_rows:
            value = _match_value(row.values.get(item.key_field))
            if value in source_keys:
                destination_counts[value] += 1
        matched_keys = set(destination_counts)
        source_value_rows = sum(item.source_counts.values())
        result = DestinationModelMatch(
            dataset_id=item.dataset_id,
            dataset_name=item.dataset_name,
            model=item.model,
            model_label=item.model_label,
            source_column_key=item.source_column_key,
            key_field=item.key_field,
            key_field_label=item.key_field_label,
            source_row_count=item.source_row_count,
            source_distinct_key_count=len(item.source_counts),
            source_blank_row_count=max(0, item.source_row_count - source_value_rows),
            source_duplicate_key_count=sum(
                1 for count in item.source_counts.values() if count > 1
            ),
            destination_existing_key_count=len(matched_keys),
            destination_duplicate_key_count=sum(
                1 for count in destination_counts.values() if count > 1
            ),
            destination_create_key_count=len(source_keys - matched_keys),
            destination_key_binding_hash=_destination_key_binding_hash(
                item,
                target_rows,
            ),
            compatible_fields=tuple(sorted(compatible)),
            missing_fields=tuple(sorted(missing)),
            incompatible_fields=tuple(sorted(incompatible)),
            destination_limit_reached=(
                len(target_rows) >= DESTINATION_MATCH_RECORD_LIMIT
            ),
        )
        return result, destination_counts

    def _relationship_results(
        self,
        workspace_id: str,
        prepared: tuple[_PreparedModel, ...],
        source_schema: OdooSchemaCatalog,
        source_origins: Mapping[str, tuple[OdooOriginBatch, ...]],
        destination_counts: Mapping[str, Counter[str]],
    ) -> tuple[DestinationRelationshipMatch, ...]:
        """Resolve protected source IDs to portable selected business keys."""

        relationships = _prepare_relationships(prepared, source_schema)
        if not relationships:
            return ()
        id_to_key: dict[str, dict[int, str | None]] = {}
        for item in prepared:
            rows = self._source_values.source_key_rows(
                workspace_id,
                item.dataset_id,
                item.source_column_key,
            )
            if len(rows) != item.source_row_count:
                raise WorkspaceError(
                    f"The frozen matching rows for {item.dataset_name} are inconsistent"
                )
            batches = source_origins.get(item.dataset_id)
            if batches is not None:
                identifiers = _ordered_origin_ids(
                    batches,
                    expected_rows=item.source_row_count,
                    dataset_name=item.dataset_name,
                )
                id_to_key[item.dataset_id] = dict(zip(identifiers, rows, strict=True))

        results: list[DestinationRelationshipMatch] = []
        for relationship in relationships:
            owner_batches = source_origins.get(relationship.owner.dataset_id)
            related_keys = id_to_key.get(relationship.related.dataset_id)
            if owner_batches is None or related_keys is None:
                results.append(_unavailable_relationship_result(relationship))
                continue
            columns = _ordered_relationship_values(
                owner_batches,
                relationship,
            )
            if columns is None:
                if relationship.owner.source_row_count == 0:
                    columns = ()
                else:
                    results.append(_unavailable_relationship_result(relationship))
                    continue
            if len(columns) != relationship.owner.source_row_count:
                raise WorkspaceError(
                    "The protected relationship rows for "
                    f"{relationship.owner.dataset_name} are inconsistent"
                )
            source_links = 0
            source_blanks = 0
            reused = 0
            incoming = 0
            missing = 0
            ambiguous = 0
            related_destination_counts = destination_counts.get(
                relationship.related.model,
                Counter(),
            )
            for members in columns:
                if not members:
                    source_blanks += 1
                source_links += len(members)
                for identifier in members:
                    key = related_keys.get(identifier)
                    if not key:
                        missing += 1
                        continue
                    destination_count = related_destination_counts.get(key, 0)
                    if destination_count > 1:
                        ambiguous += 1
                    elif destination_count == 1:
                        reused += 1
                    else:
                        incoming += 1
            results.append(
                DestinationRelationshipMatch(
                    dataset_id=relationship.owner.dataset_id,
                    dataset_name=relationship.owner.dataset_name,
                    model=relationship.owner.model,
                    model_label=relationship.owner.model_label,
                    field_name=relationship.field.name,
                    field_label=relationship.field.label,
                    kind=relationship.field.type,
                    related_dataset_id=relationship.related.dataset_id,
                    related_dataset_name=relationship.related.dataset_name,
                    related_model=relationship.related.model,
                    related_model_label=relationship.related.model_label,
                    related_key_field=relationship.related.key_field,
                    operation=(
                        "set" if relationship.field.type == "many2one" else "replace"
                    ),
                    inverse_field=relationship.inverse_field,
                    source_owner_count=relationship.owner.source_row_count,
                    source_link_count=source_links,
                    source_blank_owner_count=source_blanks,
                    destination_reused_link_count=reused,
                    incoming_link_count=incoming,
                    missing_related_record_count=missing,
                    ambiguous_destination_link_count=ambiguous,
                    source_evidence_available=True,
                    required=relationship.field.required,
                )
            )
        return tuple(
            sorted(results, key=lambda item: (item.model, item.field_name))
        )


def _prepare_relationships(
    prepared: tuple[_PreparedModel, ...],
    source_schema: OdooSchemaCatalog,
) -> tuple[_PreparedRelationship, ...]:
    """Normalize many2one/many2many and inverse one2many metadata."""

    by_model = {item.model: item for item in prepared}
    schema_by_model = {item.name: item for item in source_schema.models}
    result: list[_PreparedRelationship] = []
    for owner in prepared:
        for field in owner.source_fields:
            if field.type not in {"many2one", "many2many"}:
                continue
            related = by_model.get(field.relation or "")
            if related is None:
                continue
            inverse_field = None
            if field.type == "many2one":
                related_schema = schema_by_model.get(related.model)
                inverse_fields = tuple(
                    sorted(
                        candidate.name
                        for candidate in (related_schema.fields if related_schema else ())
                        if candidate.type == "one2many"
                        and candidate.relation == owner.model
                        and candidate.relation_field == field.name
                    )
                )
                inverse_field = inverse_fields[0] if inverse_fields else None
            result.append(
                _PreparedRelationship(
                    owner=owner,
                    field=field,
                    related=related,
                    inverse_field=inverse_field,
                )
            )
    return tuple(
        sorted(result, key=lambda item: (item.owner.model, item.field.name))
    )


def _ordered_origin_ids(
    batches: tuple[OdooOriginBatch, ...],
    *,
    expected_rows: int,
    dataset_name: str,
) -> tuple[int, ...]:
    identifiers: list[int] = []
    expected_ordinal = 1
    for batch in sorted(batches, key=lambda item: item.first_row_ordinal):
        if batch.first_row_ordinal != expected_ordinal:
            raise WorkspaceError(
                f"The protected source order for {dataset_name} is inconsistent"
            )
        identifiers.extend(batch.odoo_ids)
        expected_ordinal += batch.row_count
    if len(identifiers) != expected_rows or len(set(identifiers)) != len(identifiers):
        raise WorkspaceError(
            f"The protected source identifiers for {dataset_name} are inconsistent"
        )
    return tuple(identifiers)


def _ordered_relationship_values(
    batches: tuple[OdooOriginBatch, ...],
    relationship: _PreparedRelationship,
) -> tuple[tuple[int, ...], ...] | None:
    rows: list[tuple[int, ...]] = []
    expected_ordinal = 1
    for batch in sorted(batches, key=lambda item: item.first_row_ordinal):
        if batch.first_row_ordinal != expected_ordinal:
            raise WorkspaceError(
                "The protected relationship order for "
                f"{relationship.owner.dataset_name} is inconsistent"
            )
        column = next(
            (
                item
                for item in batch.relationships
                if item.field_name == relationship.field.name
            ),
            None,
        )
        if column is None:
            return None
        if (
            column.kind != relationship.field.type
            or column.relation_model != relationship.related.model
        ):
            raise WorkspaceError(
                f"The protected relationship contract for {relationship.field.label} changed"
            )
        rows.extend(column.values)
        expected_ordinal += batch.row_count
    return tuple(rows)


def _unavailable_relationship_result(
    relationship: _PreparedRelationship,
) -> DestinationRelationshipMatch:
    return DestinationRelationshipMatch(
        dataset_id=relationship.owner.dataset_id,
        dataset_name=relationship.owner.dataset_name,
        model=relationship.owner.model,
        model_label=relationship.owner.model_label,
        field_name=relationship.field.name,
        field_label=relationship.field.label,
        kind=relationship.field.type,
        related_dataset_id=relationship.related.dataset_id,
        related_dataset_name=relationship.related.dataset_name,
        related_model=relationship.related.model,
        related_model_label=relationship.related.model_label,
        related_key_field=relationship.related.key_field,
        operation="set" if relationship.field.type == "many2one" else "replace",
        inverse_field=relationship.inverse_field,
        source_owner_count=0,
        source_link_count=0,
        source_blank_owner_count=0,
        destination_reused_link_count=0,
        incoming_link_count=0,
        missing_related_record_count=0,
        ambiguous_destination_link_count=0,
        source_evidence_available=False,
        required=relationship.field.required,
    )


def destination_match_key_candidates(
    selection: SourceSelection,
    source_schema: OdooSchemaCatalog,
) -> dict[str, tuple[tuple[str, str, str], ...]]:
    """Return dataset -> (stable key, technical field, label) choices."""

    models = {item.name: item for item in source_schema.models}
    result: dict[str, tuple[tuple[str, str, str], ...]] = {}
    for dataset in selection.datasets:
        if not isinstance(dataset.source, OdooSourceBinding):
            result[dataset.dataset_id] = ()
            continue
        model = models.get(dataset.source.model)
        fields = {item.name: item for item in model.fields} if model else {}
        choices = []
        for column in dataset.columns:
            field = fields.get(column.source_name)
            if field is not None and field.type in _TEXT_KEY_TYPES:
                choices.append((column.stable_key, field.name, field.label))
        result[dataset.dataset_id] = tuple(
            sorted(
                choices,
                key=lambda choice: (
                    _key_rank(choice[1]),
                    choice[2].casefold(),
                    choice[1],
                ),
            )
        )
    return result


def _key_rank(field_name: str) -> int:
    preferred = {
        "default_code": 0,
        "x_external_code": 1,
        "ref": 2,
        "name": 3,
    }
    return preferred.get(field_name, 10)


def _match_value(value: object) -> str:
    if value is None or value is False:
        return ""
    return str(value).strip()


def _destination_key_binding_hash(
    item: _PreparedModel,
    target_rows,
) -> str:
    """Bind every source key to zero, one, or several destination records.

    Business-key values and numeric Odoo identifiers exist only while this
    one-way digest is calculated. The persisted match plan receives the digest
    and cannot disclose either input.
    """

    bindings: dict[str, list[str]] = {
        value: [] for value in sorted(item.source_counts)
    }
    for row in target_rows:
        value = _match_value(row.values.get(item.key_field))
        if value in bindings:
            bindings[value].append(
                target_record_binding_hash(item.model, row.odoo_id)
            )
    return content_hash(
        {
            "model": item.model,
            "key_field": item.key_field,
            "classifications": [
                {
                    "key": value,
                    "target_bindings": sorted(bindings[value]),
                }
                for value in sorted(bindings)
            ],
        }
    )
