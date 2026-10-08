"""Build bounded, destination-aware matching evidence without Odoo writes."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from math import isfinite
from typing import Mapping, Protocol, Sequence
from uuid import uuid4

from impodo.domain.execution.planner import MAX_KEYS_PER_RECORD_REQUEST
from impodo.domain.mapping.create_field_policy import (
    CreateFieldCoverage,
    VerifiedCreateDefaultAction,
    decide_verified_create_default,
    evaluate_create_field,
    required_create_hook_inputs,
)
from impodo.domain.odoo.compatibility import (
    OdooOperation,
    assess_odoo_operation,
    same_odoo_major,
)
from impodo.domain.odoo.contracts import (
    MetadataRequest,
    MetadataSnapshot,
    RecordRequest,
    RecordSnapshot,
    metadata_snapshot_payload,
    record_snapshot_payload,
)
from impodo.domain.odoo_provenance import OdooOriginBatch
from impodo.domain.odoo_source_policy import CURRENT_ODOO_SOURCE_POLICY
from impodo.domain.serialization import content_hash
from impodo.domain.shared.models import (
    OdooReadIdentity,
    TargetRecord,
    target_record_binding_hash,
)
from impodo.domain.source_binding import OdooSourceBinding
from impodo.domain.workspace.business_keys import recommend_business_key
from impodo.domain.workspace.portable_identity import (
    portable_components,
    portable_identity,
    record_identity,
)
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SourceSelection,
)
from impodo.domain.workspace.destination_matching import (
    DESTINATION_HANDLINGS,
    DESTINATION_NO_WRITE_HANDLINGS,
    DestinationCreateFieldDecision,
    DestinationCreateFieldEvidence,
    DestinationCreateFieldEvidenceValue,
    DestinationCreateIncomingReferenceCandidate,
    DestinationCreateReferenceCandidate,
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


DESTINATION_MATCH_MAX_DISTINCT_KEYS = CURRENT_ODOO_SOURCE_POLICY.max_rows
DESTINATION_MATCH_RECORD_LIMIT = MAX_KEYS_PER_RECORD_REQUEST + 1
DESTINATION_GOVERNED_IDENTITY_MODELS = frozenset({"product.category"})
_TEXT_KEY_TYPES = frozenset({"char", "text", "selection"})


class DestinationSourceValueReader(Protocol):
    def source_identity_counts(
        self,
        workspace_id: str,
        dataset_id: str,
        source_column_key: str,
        *,
        maximum_distinct_values: int,
    ) -> tuple[tuple[str, int], ...]: ...

    def source_key_rows(
        self,
        workspace_id: str,
        dataset_id: str,
        source_column_key: str,
    ) -> tuple[str | None, ...]: ...

    def source_key_tuples(
        self,
        workspace_id: str,
        dataset_id: str,
        source_column_keys: Sequence[str],
    ) -> tuple[tuple[str | None, ...], ...]: ...


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
    additional_source_column_keys: tuple[str, ...] = ()
    destination_handling: str = "transfer"

    def __post_init__(self) -> None:
        if self.destination_handling not in DESTINATION_HANDLINGS:
            raise ValueError("Destination handling is invalid")

    @property
    def source_column_keys(self) -> tuple[str, ...]:
        return (self.source_column_key, *self.additional_source_column_keys)


@dataclass(frozen=True, slots=True)
class DestinationGovernedIdentityComponent:
    """One required business component shown without making it optional."""

    field_name: str
    label: str
    scope: bool = False


@dataclass(frozen=True, slots=True)
class DestinationGovernedKeyChoice:
    """One model-aware identity that Stage 4 must apply exactly."""

    dataset_id: str
    source_column_keys: tuple[str, ...]
    key_fields: tuple[str, ...]
    title: str
    reason: str
    warning: str
    components: tuple[DestinationGovernedIdentityComponent, ...]
    comparison_label: str
    comparison_field: str
    relationship_scoped: bool = False


@dataclass(frozen=True, slots=True)
class _PreparedModel:
    dataset_id: str
    dataset_name: str
    model: str
    model_label: str
    source_column_key: str
    source_column_keys: tuple[str, ...]
    key_field: str
    key_fields: tuple[str, ...]
    key_field_label: str
    source_row_count: int
    source_counts: Counter[str]
    source_query_values: tuple[tuple[str, ...], ...]
    source_identity_values: tuple[tuple[bool | int | float | str, ...], ...]
    source_fields: tuple[SchemaField, ...]
    destination_managed_fields: tuple[str, ...]
    excluded_source_row_numbers: tuple[int, ...] = ()
    unreferenced_source_row_numbers: tuple[int, ...] = ()
    destination_handling: str = "transfer"

    @property
    def included_source_row_count(self) -> int:
        return self.source_row_count - len(self.omitted_source_row_numbers)

    @property
    def omitted_source_row_numbers(self) -> tuple[int, ...]:
        return tuple(
            sorted(
                self.excluded_source_row_numbers
                + self.unreferenced_source_row_numbers
            )
        )


@dataclass(frozen=True, slots=True)
class _PreparedRelationship:
    owner: _PreparedModel
    field: SchemaField
    related: _PreparedModel
    inverse_field: str | None


_ODOO_DESTINATION_MANAGED_SCALAR_FIELDS = frozenset({"parent_path"})


def _is_transferable_relationship_field(
    field: SchemaField,
    selected_model_names: set[str],
) -> bool:
    """Return whether captured relationship provenance may become a write.

    Odoo-source capture deliberately retains read-only and computed links as
    provenance. Destination matching must not reinterpret those Odoo-managed
    links as fields that a generic transfer should write.
    """

    return bool(
        field.type in {"many2one", "many2many"}
        and field.relation in selected_model_names
        and field.stored is not False
        and field.related is not True
        and field.company_dependent is False
        and field.exportable is True
        and not (field.computed is True and field.has_inverse is not True)
        and not (field.readonly and field.has_inverse is not True)
    )


def _is_transferable_scalar_field(
    field: SchemaField,
    selected_source_names: set[str],
) -> bool:
    """Keep captured business values while excluding Odoo-managed evidence."""

    return (
        field.name in selected_source_names
        and field.name not in _ODOO_DESTINATION_MANAGED_SCALAR_FIELDS
        and field.type not in {"many2one", "many2many", "one2many"}
        and not field.readonly
        and field.related is not True
        and not (field.computed is True and field.has_inverse is not True)
        and field.stored is not False
    )


def _is_destination_managed_scalar_field(
    field: SchemaField,
    selected_source_names: set[str],
) -> bool:
    """Identify captured framework values that destination Odoo must rebuild."""

    return bool(
        field.name in selected_source_names
        and field.type not in {"many2one", "many2many", "one2many"}
        and (
            field.name in _ODOO_DESTINATION_MANAGED_SCALAR_FIELDS
            or field.related is True
        )
    )


def _destination_record_requests(
    prepared: tuple[_PreparedModel, ...],
) -> tuple[RecordRequest, ...]:
    """Plan exact business-key reads in the shared bounded key size."""

    requests: list[RecordRequest] = []
    for item in sorted(prepared, key=lambda current: current.model):
        values = item.source_query_values
        for start in range(0, len(values), MAX_KEYS_PER_RECORD_REQUEST):
            batch = values[start : start + MAX_KEYS_PER_RECORD_REQUEST]
            requests.append(
                RecordRequest(
                    model=item.model,
                    fields=item.key_fields,
                    domain=_exact_identity_domain(item.key_fields, batch),
                    limit=DESTINATION_MATCH_RECORD_LIMIT,
                )
            )
    return tuple(requests)


def _exact_identity_domain(
    fields: tuple[str, ...],
    values: Sequence[tuple[str, ...]],
) -> tuple[object, ...]:
    """Return one exact Odoo domain for at most one governed key chunk."""

    if (
        not fields
        or not values
        or any(len(value) != len(fields) for value in values)
    ):
        raise WorkspaceError("Destination matching key evidence is incomplete")
    if len(fields) == 1:
        return ((fields[0], "in", tuple(value[0] for value in values)),)
    expressions: list[list[object]] = []
    for value in values:
        terms: list[object] = [
            (field, "=", component)
            for field, component in zip(fields, value, strict=True)
        ]
        expressions.append(["&"] * (len(terms) - 1) + terms)
    return tuple(
        ["|"] * (len(expressions) - 1)
        + [term for expression in expressions for term in expression]
    )


def _destination_chunk_limit_reached(
    item: _PreparedModel,
    target_rows: Sequence[TargetRecord],
) -> bool:
    """Detect a truncated exact-key chunk without exposing its key values."""

    chunk_by_key = {
        portable_identity(identity): index // MAX_KEYS_PER_RECORD_REQUEST
        for index, identity in enumerate(item.source_query_values)
    }
    counts: Counter[int] = Counter()
    for row in target_rows:
        value = record_identity(row.values, item.key_fields)
        chunk = chunk_by_key.get(value)
        if chunk is not None:
            counts[chunk] += 1
    return any(count >= DESTINATION_MATCH_RECORD_LIMIT for count in counts.values())


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
        excluded_source_rows: Mapping[str, Sequence[int]] | None = None,
    ) -> DestinationMatchPlan:
        """Return a current plan from one bounded metadata/record read."""

        if not workspace.destination_verified:
            raise WorkspaceError("Verify the destination Odoo connection first")
        selected = {item.dataset_id: item.source_column_keys for item in choices}
        destination_handlings = {
            item.dataset_id: item.destination_handling for item in choices
        }
        if len(selected) != len(choices):
            raise WorkspaceError("Choose one matching field for each source table")
        if set(selected) != {item.dataset_id for item in selection.datasets}:
            raise WorkspaceError("Choose one matching field for each source table")

        source_models = {item.name: item for item in source_schema.models}
        governed_choices = destination_governed_key_choices(
            selection,
            source_schema,
        )
        selected_model_names = {
            dataset.source.model
            for dataset in selection.datasets
            if isinstance(dataset.source, OdooSourceBinding)
        }
        prepared: list[_PreparedModel] = []
        exclusions_by_dataset = dict(excluded_source_rows or {})
        if set(exclusions_by_dataset) - {item.dataset_id for item in selection.datasets}:
            raise WorkspaceError("Excluded source rows do not belong to this selection")
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
            source_column_keys = selected[dataset.dataset_id]
            governed = governed_choices.get(dataset.dataset_id)
            if (
                source_model.name in DESTINATION_GOVERNED_IDENTITY_MODELS
                and governed is None
            ):
                raise WorkspaceError(
                    f"{source_model.label} requires captured Complete Name and "
                    "Parent Category evidence; return to the source data and "
                    "capture those fields before matching"
                )
            if (
                governed is not None
                and source_column_keys != governed.source_column_keys
            ):
                raise WorkspaceError(
                    f"{source_model.label} uses the governed identity "
                    f"{governed.title}; reload this page and try again"
                )
            if (
                not 1 <= len(source_column_keys) <= 3
                or len(set(source_column_keys)) != len(source_column_keys)
            ):
                raise WorkspaceError(
                    f"Choose one to three distinct matching fields for {dataset.name}"
                )
            columns_by_key = {item.stable_key: item for item in dataset.columns}
            source_columns = tuple(columns_by_key.get(key) for key in source_column_keys)
            if any(column is None for column in source_columns):
                raise WorkspaceError(
                    f"Choose a current matching field for {dataset.name}"
                )
            fields_by_name = {item.name: item for item in source_model.fields}
            key_fields = tuple(
                fields_by_name.get(column.source_name) for column in source_columns
            )
            if (
                key_fields[0] is None
                or key_fields[0].type not in _TEXT_KEY_TYPES
                or any(
                    field is None or field.type not in _TEXT_KEY_TYPES | {"integer"}
                    for field in key_fields
                )
            ):
                raise WorkspaceError(
                    f"Choose a text first matching field and scalar components for {dataset.name}"
                )
            key_field = key_fields[0]
            source_counts: Counter[str] = Counter()
            identity_by_key: dict[
                str, tuple[bool | int | float | str, ...]
            ] = {}
            raw_exclusions = exclusions_by_dataset.get(dataset.dataset_id, ())
            if any(
                not isinstance(number, int)
                or isinstance(number, bool)
                or number < 1
                or number > dataset.row_count
                for number in raw_exclusions
            ):
                raise WorkspaceError(
                    f"Choose current source records for {dataset.name}"
                )
            exclusions = tuple(sorted(set(raw_exclusions)))
            if exclusions:
                rows = self._source_values.source_key_tuples(
                    workspace.workspace_id,
                    dataset.dataset_id,
                    source_column_keys,
                )
                if len(rows) != dataset.row_count or any(
                    len(row) != len(key_fields) for row in rows
                ):
                    raise WorkspaceError(
                        f"The frozen matching rows for {dataset.name} are inconsistent"
                    )
                full_keys = tuple(portable_identity(row) for row in rows)
                full_counts = Counter(value for value in full_keys if value)
                issue_rows = {
                    index
                    for index, value in enumerate(full_keys, start=1)
                    if not value or full_counts[value] > 1
                }
                if not set(exclusions).issubset(issue_rows):
                    raise WorkspaceError(
                        f"Only current identity issues can be put aside for {dataset.name}"
                    )
                for index, row in enumerate(rows, start=1):
                    if index in exclusions:
                        continue
                    value = full_keys[index - 1]
                    if value:
                        source_counts[value] += 1
                        identity = portable_components(row)
                        if len(identity) == len(key_fields):
                            identity_by_key[value] = identity
            elif len(key_fields) == 1:
                raw_choices = self._source_values.source_identity_counts(
                    workspace.workspace_id,
                    dataset.dataset_id,
                    source_column_keys[0],
                    maximum_distinct_values=DESTINATION_MATCH_MAX_DISTINCT_KEYS,
                )
                for raw_value, count in raw_choices:
                    value = portable_identity((raw_value,))
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
                    identity = portable_components((raw_value,))
                    if len(identity) == 1:
                        identity_by_key[value] = identity
            else:
                rows = self._source_values.source_key_tuples(
                    workspace.workspace_id,
                    dataset.dataset_id,
                    source_column_keys,
                )
                if len(rows) != dataset.row_count:
                    raise WorkspaceError(
                        f"The frozen matching rows for {dataset.name} are inconsistent"
                    )
                for row in rows:
                    if len(row) != len(key_fields):
                        raise WorkspaceError(
                            f"The frozen matching rows for {dataset.name} are invalid"
                        )
                    value = portable_identity(row)
                    if value:
                        source_counts[value] += 1
                        identity = portable_components(row)
                        if len(identity) == len(key_fields):
                            identity_by_key[value] = identity
            if len(source_counts) > DESTINATION_MATCH_MAX_DISTINCT_KEYS:
                raise WorkspaceError(
                    f"{dataset.name} has {len(source_counts):,} distinct matching "
                    f"values; destination matching supports up to "
                    f"{DESTINATION_MATCH_MAX_DISTINCT_KEYS:,}"
                )
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
                    source_column_key=source_column_keys[0],
                    source_column_keys=source_column_keys,
                    key_field=key_field.name,
                    key_fields=tuple(field.name for field in key_fields),
                    key_field_label=key_field.label,
                    source_row_count=dataset.row_count,
                    source_counts=source_counts,
                    source_query_values=tuple(
                        identity_by_key[key] for key in sorted(identity_by_key)
                    ),
                    source_identity_values=tuple(
                        identity_by_key[key]
                        for key in sorted(identity_by_key)
                        if source_counts[key] == 1
                    ),
                    source_fields=tuple(
                        field
                        for field in source_model.fields
                        if _is_transferable_scalar_field(
                            field,
                            selected_source_names,
                        )
                        or _is_transferable_relationship_field(
                            field,
                            selected_model_names,
                        )
                    ),
                    destination_managed_fields=tuple(
                        sorted(
                            field.name
                            for field in source_model.fields
                            if _is_destination_managed_scalar_field(
                                field,
                                selected_source_names,
                            )
                        )
                    ),
                    excluded_source_row_numbers=exclusions,
                    destination_handling=destination_handlings[dataset.dataset_id],
                )
            )

        prepared = list(
            self._scope_reference_only_models(
                workspace.workspace_id,
                tuple(prepared),
                source_schema,
                source_origins or {},
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
                all_fields=True,
                include_unique_constraints=True,
            )
            for item in sorted(prepared, key=lambda current: current.model)
        )
        record_requests = _destination_record_requests(tuple(prepared))
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
                or not assess_odoo_operation(
                    snapshot.fingerprint.odoo_version, OdooOperation.COMPARE,
                ).allowed
            ):
                raise WorkspaceError(
                    "The destination matching read returned a different Odoo target"
                )
            if not same_odoo_major(source_schema.odoo_version, snapshot.fingerprint.odoo_version):
                raise WorkspaceError(
                    "Odoo source and destination matching requires the same major version"
                )

        model_results: list[DestinationModelMatch] = []
        create_field_decisions: list[DestinationCreateFieldDecision] = []
        create_field_values: list[DestinationCreateFieldEvidenceValue] = []
        destination_counts: dict[str, Counter[str]] = {}
        for item in sorted(prepared, key=lambda current: current.model):
            result, counts, decisions, values = self._result(
                item, metadata, records, tuple(prepared)
            )
            model_results.append(result)
            create_field_decisions.extend(decisions)
            create_field_values.extend(values)
            destination_counts[item.model] = counts
        relationships = self._relationship_results(
            workspace.workspace_id,
            tuple(prepared),
            source_schema,
            source_origins or {},
            destination_counts,
        )
        recorded_at = datetime.now(UTC)
        schema_snapshot_hash = content_hash(metadata_snapshot_payload(metadata))
        create_field_evidence = (
            DestinationCreateFieldEvidence(
                evidence_id=str(uuid4()),
                workspace_id=workspace.workspace_id,
                source_selection_hash=selection.content_hash,
                source_schema_hash=source_schema.content_hash,
                destination_target_hash=expected_target_hash,
                destination_read_principal_hash=read_identity.principal_hash,
                destination_read_context_hash=read_identity.context_hash,
                destination_schema_snapshot_hash=schema_snapshot_hash,
                values=tuple(sorted(create_field_values, key=lambda current: current.key)),
                recorded_at=recorded_at,
            )
            if create_field_values
            else None
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
            destination_schema_snapshot_hash=schema_snapshot_hash,
            destination_record_snapshot_hash=content_hash(
                record_snapshot_payload(records)
            ),
            model_matches=tuple(model_results),
            recorded_at=recorded_at,
            recorded_by=recorded_by,
            relationship_matches=relationships,
            create_field_decisions=tuple(
                sorted(create_field_decisions, key=lambda current: current.key)
            ),
            create_field_evidence_id=(
                create_field_evidence.evidence_id if create_field_evidence else None
            ),
            create_field_evidence_hash=(
                create_field_evidence.content_hash if create_field_evidence else None
            ),
            create_field_evidence=create_field_evidence,
        )

    def _scope_reference_only_models(
        self,
        workspace_id: str,
        prepared: tuple[_PreparedModel, ...],
        source_schema: OdooSchemaCatalog,
        source_origins: Mapping[str, tuple[OdooOriginBatch, ...]],
    ) -> tuple[_PreparedModel, ...]:
        """Keep only destination-setup rows reached by selected relationships.

        A reference-only dataset supplies portable keys for relationships such
        as ``mrp.bom.company_id``.  It is not an independent transfer root.
        The closure also follows reference-only owners so a referenced setup
        record can safely retain another destination-owned dependency.
        """

        reference_dataset_ids = {
            item.dataset_id
            for item in prepared
            if item.destination_handling == "reference_only"
        }
        if not reference_dataset_ids:
            return prepared

        relationships = _prepare_relationships(prepared, source_schema)
        active_rows: dict[str, set[int]] = {
            item.dataset_id: (
                set()
                if item.dataset_id in reference_dataset_ids
                else set(range(1, item.source_row_count + 1))
                - set(item.excluded_source_row_numbers)
            )
            for item in prepared
        }
        row_by_odoo_id: dict[str, dict[int, int]] = {}
        for item in prepared:
            if item.dataset_id not in reference_dataset_ids:
                continue
            batches = source_origins.get(item.dataset_id)
            if batches is None:
                continue
            identifiers = _ordered_origin_ids(
                batches,
                expected_rows=item.source_row_count,
                dataset_name=item.dataset_name,
            )
            row_by_odoo_id[item.dataset_id] = {
                identifier: row_number
                for row_number, identifier in enumerate(identifiers, start=1)
            }

        changed = True
        while changed:
            changed = False
            for relationship in relationships:
                related_id = relationship.related.dataset_id
                if related_id not in reference_dataset_ids:
                    continue
                owner_batches = source_origins.get(relationship.owner.dataset_id)
                related_rows = row_by_odoo_id.get(related_id)
                if owner_batches is None or related_rows is None:
                    continue
                columns = _ordered_relationship_values(
                    owner_batches,
                    relationship,
                )
                if columns is None:
                    continue
                owner_rows = active_rows[relationship.owner.dataset_id]
                explicit_exclusions = set(
                    relationship.related.excluded_source_row_numbers
                )
                for owner_row_number in tuple(sorted(owner_rows)):
                    if owner_row_number > len(columns):
                        raise WorkspaceError(
                            "The protected relationship rows for "
                            f"{relationship.owner.dataset_name} are inconsistent"
                        )
                    for identifier in columns[owner_row_number - 1]:
                        related_row_number = related_rows.get(identifier)
                        if (
                            related_row_number is not None
                            and related_row_number not in explicit_exclusions
                            and related_row_number not in active_rows[related_id]
                        ):
                            active_rows[related_id].add(related_row_number)
                            changed = True

        scoped: list[_PreparedModel] = []
        for item in prepared:
            if item.dataset_id not in reference_dataset_ids:
                scoped.append(item)
                continue
            rows = self._source_values.source_key_tuples(
                workspace_id,
                item.dataset_id,
                item.source_column_keys,
            )
            if len(rows) != item.source_row_count or any(
                len(row) != len(item.key_fields) for row in rows
            ):
                raise WorkspaceError(
                    f"The frozen matching rows for {item.dataset_name} are inconsistent"
                )
            referenced_rows = active_rows[item.dataset_id]
            explicit_exclusions = set(item.excluded_source_row_numbers)
            unreferenced_rows = tuple(
                row_number
                for row_number in range(1, item.source_row_count + 1)
                if row_number not in referenced_rows
                and row_number not in explicit_exclusions
            )
            source_counts: Counter[str] = Counter()
            identity_by_key: dict[
                str, tuple[bool | int | float | str, ...]
            ] = {}
            for row_number in sorted(referenced_rows):
                row = rows[row_number - 1]
                value = portable_identity(row)
                if not value:
                    continue
                source_counts[value] += 1
                identity = portable_components(row)
                if len(identity) == len(item.key_fields):
                    identity_by_key[value] = identity
            scoped.append(
                replace(
                    item,
                    source_counts=source_counts,
                    source_query_values=tuple(
                        identity_by_key[key] for key in sorted(identity_by_key)
                    ),
                    source_identity_values=tuple(
                        identity_by_key[key]
                        for key in sorted(identity_by_key)
                        if source_counts[key] == 1
                    ),
                    unreferenced_source_row_numbers=unreferenced_rows,
                )
            )
        return tuple(scoped)

    def _result(
        self,
        item: _PreparedModel,
        metadata: MetadataSnapshot,
        records: RecordSnapshot,
        prepared: tuple[_PreparedModel, ...],
    ) -> tuple[
        DestinationModelMatch,
        Counter[str],
        tuple[DestinationCreateFieldDecision, ...],
        tuple[DestinationCreateFieldEvidenceValue, ...],
    ]:
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
            value = record_identity(row.values, item.key_fields)
            if value in source_keys:
                destination_counts[value] += 1
        matched_keys = set(destination_counts)
        source_value_rows = sum(item.source_counts.values())
        create_count = len(source_keys - matched_keys)
        unresolved, decisions, values = (
            _create_field_defaults(
                item,
                metadata,
                tuple(compatible),
                records,
                prepared,
            )
            if create_count and item.destination_handling == "transfer"
            else ((), (), ())
        )
        result = DestinationModelMatch(
            dataset_id=item.dataset_id,
            dataset_name=item.dataset_name,
            model=item.model,
            model_label=item.model_label,
            source_column_key=item.source_column_key,
            key_field=item.key_field,
            key_field_label=item.key_field_label,
            source_row_count=item.included_source_row_count,
            source_distinct_key_count=len(item.source_counts),
            source_blank_row_count=max(
                0, item.included_source_row_count - source_value_rows
            ),
            source_duplicate_key_count=sum(
                1 for count in item.source_counts.values() if count > 1
            ),
            destination_existing_key_count=len(matched_keys),
            destination_duplicate_key_count=sum(
                1 for count in destination_counts.values() if count > 1
            ),
            destination_create_key_count=create_count,
            destination_key_binding_hash=_destination_key_binding_hash(
                item,
                target_rows,
            ),
            compatible_fields=tuple(sorted(compatible)),
            missing_fields=tuple(sorted(missing)),
            incompatible_fields=tuple(sorted(incompatible)),
            destination_managed_fields=item.destination_managed_fields,
            unresolved_create_fields=unresolved,
            requires_workflow_handler=bool(
                destination_model is not None
                and (state_field := destination_model.fields.get("state")) is not None
                and state_field.type == "selection"
            ),
            destination_limit_reached=_destination_chunk_limit_reached(
                item,
                target_rows,
            ),
            source_column_keys=item.source_column_keys,
            key_fields=item.key_fields,
            excluded_source_row_numbers=item.excluded_source_row_numbers,
            unreferenced_source_row_numbers=(
                item.unreferenced_source_row_numbers
            ),
            destination_handling=item.destination_handling,
        )
        return result, destination_counts, decisions, values

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
            if len(item.key_fields) == 1:
                rows = self._source_values.source_key_rows(
                    workspace_id, item.dataset_id, item.source_column_key,
                )
            else:
                rows = tuple(
                    portable_identity(row)
                    for row in self._source_values.source_key_tuples(
                        workspace_id, item.dataset_id, item.source_column_keys,
                    )
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
                excluded = set(item.omitted_source_row_numbers)
                id_to_key[item.dataset_id] = {
                    identifier: key
                    for index, (identifier, key) in enumerate(
                        zip(identifiers, rows, strict=True),
                        start=1,
                    )
                    if index not in excluded
                }

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
            excluded_owners = set(relationship.owner.omitted_source_row_numbers)
            for row_number, members in enumerate(columns, start=1):
                if row_number in excluded_owners:
                    continue
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
                    elif (
                        relationship.related.destination_handling
                        in DESTINATION_NO_WRITE_HANDLINGS
                    ):
                        missing += 1
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
                    related_key_fields=relationship.related.key_fields,
                    operation=(
                        "set" if relationship.field.type == "many2one" else "replace"
                    ),
                    inverse_field=relationship.inverse_field,
                    source_owner_count=relationship.owner.included_source_row_count,
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


def _create_field_defaults(
    item: _PreparedModel,
    metadata: MetadataSnapshot,
    compatible_fields: tuple[str, ...],
    records: RecordSnapshot,
    prepared: tuple[_PreparedModel, ...],
) -> tuple[
    tuple[str, ...],
    tuple[DestinationCreateFieldDecision, ...],
    tuple[DestinationCreateFieldEvidenceValue, ...],
]:
    """Find required create inputs absent from the reviewed source projection.

    A target default is accepted automatically only when the shared create
    policy considers it low risk. Other defaults retain a review step.
    """

    model = metadata.models.get(item.model)
    if model is None:
        return (), (), ()
    provided = set(compatible_fields)
    defaults = metadata.create_defaults.get(item.model, {})
    unresolved = set(required_create_hook_inputs(item.model, provided) - provided)
    decisions: list[DestinationCreateFieldDecision] = []
    evidence_values: list[DestinationCreateFieldEvidenceValue] = []
    for field in model.fields.values():
        if not field.required:
            continue
        default = defaults.get(field.name)
        has_default = (
            field.name in defaults
            and _usable_create_default(field.type, default)
        )
        view = SchemaField(
            name=field.name,
            label=field.label or field.name,
            type=field.type,
            required=field.required,
            readonly=field.readonly,
            relation=field.relation,
            relation_field=field.relation_field,
            selection=field.selection,
            computed=field.computed,
            related=field.related,
            company_dependent=field.company_dependent,
            create_default_present=has_default,
            create_default_value=default if has_default else None,
        )
        assessment = evaluate_create_field(
            view,
            provided=field.name in provided,
            handling=None,
            target_model=item.model,
            odoo_version=metadata.fingerprint.odoo_version,
        )
        field_contract_hash = content_hash(
            {
                "model": item.model,
                "field": field.name,
                "type": field.type,
                "required": field.required,
                "readonly": field.readonly,
                "relation": field.relation,
                "selection": [list(choice) for choice in field.selection],
                "computed": field.computed,
                "related": field.related,
                "company_dependent": field.company_dependent,
            }
        )
        source_candidates = tuple(
            sorted(
                (
                    (source.name, source.label or source.name)
                    for source in item.source_fields
                    if _compatible_create_source_field(source, field)
                ),
                key=lambda candidate: candidate[0],
            )
        )
        reference_candidates = _create_reference_candidates(
            field,
            prepared,
            records,
        )
        incoming_reference_candidates = _create_incoming_reference_candidates(
            field,
            item,
            prepared,
            records,
        )
        if assessment.coverage is CreateFieldCoverage.DEFAULT_AVAILABLE:
            default_decision = decide_verified_create_default(view)
            reference_candidate = None
            if field.type == "many2one":
                reference_candidate = next(
                    (
                        candidate
                        for candidate in reference_candidates
                        if candidate.odoo_id == default
                    ),
                    None,
                )
            value_hash = content_hash(
                {
                    "model": item.model,
                    "field": field.name,
                    "type": field.type,
                    "value": default,
                    "reference_choice": (
                        reference_candidate.choice_hash
                        if reference_candidate is not None
                        else None
                    ),
                }
            )
            automatic = (
                default_decision.action
                is VerifiedCreateDefaultAction.APPLY_AUTOMATICALLY
            )
            decisions.append(
                DestinationCreateFieldDecision(
                    dataset_id=item.dataset_id,
                    model=item.model,
                    model_label=item.model_label,
                    field_name=field.name,
                    field_label=field.label or field.name,
                    field_type=field.type,
                    provider_kind="odoo_default",
                    decision_kind="automatic" if automatic else "review",
                    reason=default_decision.reason,
                    field_contract_hash=field_contract_hash,
                    value_hash=value_hash,
                    reviewed=automatic,
                    related_model=(
                        reference_candidate.related_model
                        if reference_candidate is not None
                        else field.relation
                    ),
                    related_identity_fields=(
                        reference_candidate.identity_fields
                        if reference_candidate is not None
                        else ()
                    ),
                )
            )
            evidence_values.append(
                DestinationCreateFieldEvidenceValue(
                    dataset_id=item.dataset_id,
                    model=item.model,
                    field_name=field.name,
                    field_type=field.type,
                    value=default,
                    display_value=(
                        reference_candidate.display_value
                        if reference_candidate is not None
                        else (
                            "Current destination default"
                            if field.type == "many2one"
                            else _create_default_display_value(view)
                        )
                    ),
                    field_contract_hash=field_contract_hash,
                    value_hash=value_hash,
                    field_label=field.label or field.name,
                    provider_kind="odoo_default",
                    source_candidates=source_candidates,
                    selection=tuple(
                        sorted(
                            ((str(key), str(label)) for key, label in field.selection),
                            key=lambda choice: choice[0],
                        )
                    ),
                    reference_candidates=reference_candidates,
                    incoming_reference_candidates=(
                        incoming_reference_candidates
                    ),
                )
            )
            if not automatic:
                unresolved.add(field.name)
        elif assessment.coverage in {
            CreateFieldCoverage.REQUIRED_VALUE_MISSING,
            CreateFieldCoverage.DEFAULT_UNVERIFIED,
            CreateFieldCoverage.ODOO_MANAGED_INVALID,
        }:
            unresolved.add(field.name)
            evidence_values.append(
                DestinationCreateFieldEvidenceValue(
                    dataset_id=item.dataset_id,
                    model=item.model,
                    field_name=field.name,
                    field_type=field.type,
                    value=None,
                    display_value="",
                    field_contract_hash=field_contract_hash,
                    value_hash=content_hash(
                        {
                            "model": item.model,
                            "field": field.name,
                            "provider": "unresolved",
                        }
                    ),
                    field_label=field.label or field.name,
                    provider_kind="unresolved",
                    source_candidates=source_candidates,
                    selection=tuple(
                        sorted(
                            (
                                (str(key), str(label))
                                for key, label in field.selection
                            ),
                            key=lambda choice: choice[0],
                        )
                    ),
                    reference_candidates=reference_candidates,
                    incoming_reference_candidates=(
                        incoming_reference_candidates
                    ),
                )
            )
    return (
        tuple(sorted(unresolved)),
        tuple(sorted(decisions, key=lambda current: current.key)),
        tuple(sorted(evidence_values, key=lambda current: current.key)),
    )


def _compatible_create_source_field(
    source: SchemaField,
    destination: object,
) -> bool:
    """Return a conservative captured-source candidate for one create field."""

    if getattr(destination, "type", "") not in {
        "boolean",
        "char",
        "date",
        "datetime",
        "float",
        "html",
        "integer",
        "monetary",
        "selection",
        "text",
    }:
        return False
    if source.type != getattr(destination, "type", ""):
        return False
    if source.type == "selection":
        source_codes = {str(key) for key, _label in source.selection}
        destination_codes = {
            str(key) for key, _label in getattr(destination, "selection", ())
        }
        return bool(source_codes) and source_codes.issubset(destination_codes)
    return True


def _create_reference_candidates(
    field: object,
    prepared: tuple[_PreparedModel, ...],
    records: RecordSnapshot,
) -> tuple[DestinationCreateReferenceCandidate, ...]:
    if getattr(field, "type", "") != "many2one":
        return ()
    related_model = getattr(field, "relation", None)
    related = next(
        (item for item in prepared if item.model == related_model),
        None,
    )
    if related is None:
        return ()
    target_rows = records.records.get(related.model, ())
    identities = [record_identity(row.values, related.key_fields) for row in target_rows]
    counts = Counter(identity for identity in identities if identity)
    candidates = []
    for row, portable_identity_value in zip(target_rows, identities, strict=True):
        if not portable_identity_value or counts[portable_identity_value] != 1:
            continue
        identity = portable_components(
            tuple(row.values.get(name) for name in related.key_fields)
        )
        if len(identity) != len(related.key_fields):
            continue
        binding_hash = target_record_binding_hash(related.model, row.odoo_id)
        candidates.append(
            DestinationCreateReferenceCandidate(
                choice_hash=content_hash(
                    {
                        "model": related.model,
                        "identity_fields": related.key_fields,
                        "identity": identity,
                        "target_binding": binding_hash,
                    }
                ),
                related_model=related.model,
                identity_fields=related.key_fields,
                identity=identity,
                display_value=" + ".join(str(value) for value in identity),
                odoo_id=row.odoo_id,
                target_binding_hash=binding_hash,
            )
        )
    return tuple(sorted(candidates, key=lambda candidate: candidate.choice_hash))


def _create_incoming_reference_candidates(
    field: object,
    owner: _PreparedModel,
    prepared: tuple[_PreparedModel, ...],
    records: RecordSnapshot,
) -> tuple[DestinationCreateIncomingReferenceCandidate, ...]:
    """Offer unique source records from another selected related dataset."""

    if getattr(field, "type", "") != "many2one":
        return ()
    related_model = getattr(field, "relation", None)
    related = next(
        (
            item for item in prepared
            if item.model == related_model and item.dataset_id != owner.dataset_id
        ),
        None,
    )
    if related is None:
        return ()
    target_by_identity: dict[str, list[int]] = {
        portable_identity(identity): []
        for identity in related.source_identity_values
    }
    for row in records.records.get(related.model, ()):
        identity = record_identity(row.values, related.key_fields)
        if identity in target_by_identity:
            target_by_identity[identity].append(row.odoo_id)
    candidates = []
    for identity in related.source_identity_values:
        portable_key = portable_identity(identity)
        target_ids = target_by_identity.get(portable_key, ())
        if len(target_ids) > 1:
            continue
        binding_hash = (
            target_record_binding_hash(related.model, target_ids[0])
            if target_ids
            else ""
        )
        requires_create = not target_ids
        candidates.append(
            DestinationCreateIncomingReferenceCandidate(
                choice_hash=content_hash(
                    {
                        "source_dataset": related.dataset_id,
                        "model": related.model,
                        "identity_fields": related.key_fields,
                        "identity": identity,
                        "requires_create": requires_create,
                        "target_binding": binding_hash,
                    }
                ),
                source_dataset_id=related.dataset_id,
                source_dataset_name=related.dataset_name,
                related_model=related.model,
                identity_fields=related.key_fields,
                identity=identity,
                display_value=" + ".join(str(value) for value in identity),
                requires_create=requires_create,
                target_binding_hash=binding_hash,
            )
        )
    return tuple(sorted(candidates, key=lambda candidate: candidate.choice_hash))


def _create_default_display_value(field: SchemaField) -> str:
    value = field.create_default_value
    if field.type == "selection":
        label = next(
            (
                str(choice_label)
                for code, choice_label in field.selection
                if str(code) == str(value)
            ),
            str(value),
        )
        return f"{label} ({value})"
    if field.type == "boolean":
        return "Yes" if value else "No"
    return str(value)


def _usable_create_default(field_type: str, value: object) -> bool:
    if value is None:
        return False
    if field_type == "boolean":
        return isinstance(value, bool)
    if field_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if field_type in {"float", "monetary"}:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        try:
            return isfinite(value)
        except OverflowError:
            return False
    if field_type == "many2one":
        return isinstance(value, int) and not isinstance(value, bool) and value > 0
    if field_type in {"char", "text", "selection", "date", "datetime", "html"}:
        return isinstance(value, str) and bool(value.strip())
    return False


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
        related_key_fields=relationship.related.key_fields,
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
            if field is not None and field.type in _TEXT_KEY_TYPES | {"integer"}:
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


def destination_governed_key_choices(
    selection: SourceSelection,
    source_schema: OdooSchemaCatalog,
) -> dict[str, DestinationGovernedKeyChoice]:
    """Return exact model-aware identities that are not operator alternatives.

    Product categories use Odoo's frozen complete path as the portable
    comparison value while ``parent_id`` remains protected relationship
    evidence.  This distinguishes equal leaf names beneath different parents
    without moving a numeric Odoo identifier into portable evidence.  Other
    recommendations remain non-binding choices.
    """

    models = {item.name: item for item in source_schema.models}
    governed: dict[str, DestinationGovernedKeyChoice] = {}
    for dataset in selection.datasets:
        if not isinstance(dataset.source, OdooSourceBinding):
            continue
        model = models.get(dataset.source.model)
        if model is None:
            continue
        recommendation = recommend_business_key(model)
        if recommendation is None:
            continue
        fields = {item.name: item for item in model.fields}
        columns = {item.source_name: item.stable_key for item in dataset.columns}
        if (
            model.name != "product.category"
            or recommendation.key_fields != ("name",)
            or recommendation.scope_fields != ("parent_id",)
            or "complete_name" not in columns
            or fields.get("complete_name") is None
            or fields["complete_name"].type not in _TEXT_KEY_TYPES
        ):
            continue
        source_names = ("complete_name",)
        key_fields = ("complete_name",)
        comparison_field = "complete_name"
        if any(name not in columns or name not in fields for name in source_names):
            continue
        if (
            fields[key_fields[0]].type not in _TEXT_KEY_TYPES
            or any(
                fields[name].type not in _TEXT_KEY_TYPES | {"integer"}
                for name in key_fields
            )
        ):
            continue
        component_names = (
            *recommendation.key_fields,
            *recommendation.scope_fields,
        )
        governed[dataset.dataset_id] = DestinationGovernedKeyChoice(
            dataset_id=dataset.dataset_id,
            source_column_keys=tuple(columns[name] for name in source_names),
            key_fields=key_fields,
            title=recommendation.description,
            reason=recommendation.reason,
            warning=recommendation.warning,
            components=tuple(
                DestinationGovernedIdentityComponent(
                    field_name=name,
                    label=fields[name].label,
                    scope=name in recommendation.scope_fields,
                )
                for name in component_names
            ),
            comparison_label=fields[comparison_field].label,
            comparison_field=comparison_field,
            relationship_scoped=bool(recommendation.scope_fields),
        )
    return governed


def _key_rank(field_name: str) -> int:
    preferred = {
        "default_code": 0,
        "x_external_code": 1,
        "ref": 2,
        "name": 3,
    }
    return preferred.get(field_name, 10)


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
        value = record_identity(row.values, item.key_fields)
        if value in bindings:
            bindings[value].append(
                target_record_binding_hash(item.model, row.odoo_id)
            )
    return content_hash(
        {
            "model": item.model,
            "key_field": item.key_field,
            **({"key_fields": item.key_fields} if len(item.key_fields) > 1 else {}),
            "classifications": [
                {
                    "key": value,
                    "target_bindings": sorted(bindings[value]),
                }
                for value in sorted(bindings)
            ],
        }
    )
