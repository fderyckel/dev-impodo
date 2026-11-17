"""Prepare exact reviewed scalar and relationship values for one write."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from impodo.domain.execution.identity import identity_domain, portable_key
from impodo.domain.execution.odoo_write import OdooWriteExecutor
from impodo.domain.execution_snapshot import ExecutionDataset, ExecutionRow, FieldIntent
from impodo.domain.shared.models import BusinessReference, LogicalReference
from impodo.domain.workspace.errors import WorkspaceError

from .identity_resolution import domain_cache_key, identity_cache_key
from .load_observation import record_load_failure


def row_values(
    row: ExecutionRow,
    metadata: Mapping[str, ExecutionDataset],
    by_source: Mapping[tuple[str, str], ExecutionRow],
    source_cache: dict[tuple[str, str, str, str], int],
    identity_cache: dict[str, int],
    executor: OdooWriteExecutor,
    *,
    protected_values: Mapping[str, Any] | None = None,
    import_relations: bool = False,
    skip_fields: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    field_name = ""
    try:
        for intent in row.fields:
            field_name = intent.field
            if (
                intent.action in {"OMIT", "EXPECT_PROTECTED"}
                or intent.field in skip_fields
            ):
                continue
            if intent.action == "SET_NULL":
                values[intent.field] = "" if import_relations else None
            elif intent.action == "SET_PROTECTED" and intent.kind == "relation":
                if (
                    protected_values is None
                    or intent.protected_value_hash not in protected_values
                    or type(protected_values[intent.protected_value_hash]) is not int
                    or protected_values[intent.protected_value_hash] <= 0
                ):
                    raise WorkspaceError(
                        f"Protected relationship is unavailable for "
                        f"{row.target_model}.{intent.field}",
                        load_failure_code="LOAD_PROTECTED_RELATIONSHIP_IS_UNAVAILABLE_FOR_MODEL_FIELD",
                    )
                identifier = protected_values[intent.protected_value_hash]
                if import_relations:
                    values[f"{intent.field}/.id"] = str(identifier)
                else:
                    values[intent.field] = identifier
            elif intent.kind == "scalar":
                raw_value = intent.value
                if intent.action == "SET_PROTECTED":
                    if (
                        protected_values is None
                        or intent.protected_value_hash not in protected_values
                    ):
                        raise WorkspaceError(
                            f"Protected value is unavailable for {row.target_model}.{intent.field}",
                            load_failure_code="LOAD_PROTECTED_VALUE_IS_UNAVAILABLE_FOR_MODEL_FIELD",
                        )
                    raw_value = protected_values[intent.protected_value_hash]
                values[intent.field] = (
                    _odoo_import_scalar(raw_value)
                    if import_relations
                    else _odoo_scalar(raw_value)
                )
            elif import_relations:
                field, value = _import_relation_value(
                    intent,
                    metadata,
                    by_source,
                    source_cache,
                    identity_cache,
                    executor,
                )
                values[field] = value
            else:
                values[intent.field] = relation_value(
                    intent,
                    metadata,
                    by_source,
                    source_cache,
                    identity_cache,
                    executor,
                )
    except Exception as error:
        record_load_failure(
            error,
            stage="prepare_write_values",
            model=row.target_model,
            fields=(field_name,),
        )
        raise
    if not values:
        raise WorkspaceError(
            "A planned write has no permitted field values",
            load_failure_code="LOAD_A_PLANNED_WRITE_HAS_NO_PERMITTED_FIELD_VALUES",
        )
    return values


def _import_relation_value(
    intent: FieldIntent,
    metadata: Mapping[str, ExecutionDataset],
    by_source: Mapping[tuple[str, str], ExecutionRow],
    source_cache: dict[tuple[str, str, str, str], int],
    identity_cache: dict[str, int],
    executor: OdooWriteExecutor,
) -> tuple[str, object]:
    """Choose exact Odoo import identities for one remote relation field."""

    value = intent.value
    if isinstance(value, tuple):
        if intent.relation_operation not in {"replace", "add"}:
            raise WorkspaceError(
                f"Relationship field {intent.field} is outside the remote "
                "many2many import slice",
                load_failure_code="LOAD_RELATIONSHIP_FIELD_FIELD_IS_OUTSIDE_THE_REMOTE_MANY2MANY_IMPORT_SLICE",
            )
        for item in value:
            if isinstance(item, BusinessReference):
                if item.model != intent.related_model:
                    raise WorkspaceError(
                        f"Relationship field {intent.field} targets another Odoo model",
                        load_failure_code="LOAD_RELATIONSHIP_FIELD_FIELD_TARGETS_ANOTHER_ODOO_MODEL",
                    )
            elif not (
                isinstance(item, LogicalReference)
                and item.origin == "incoming"
                and item.dataset is not None
            ):
                raise WorkspaceError(
                    f"Relationship field {intent.field} is outside the remote "
                    "many2many import slice",
                    load_failure_code="LOAD_RELATIONSHIP_FIELD_FIELD_IS_OUTSIDE_THE_REMOTE_MANY2MANY_IMPORT_SLICE",
                )
        identifiers = tuple(
            dict.fromkeys(
                _relation_reference_id(
                    item,
                    intent,
                    metadata,
                    by_source,
                    source_cache,
                    identity_cache,
                    executor,
                )
                for item in value
            )
        )
        return (
            f"{intent.field}/.id",
            ",".join(str(identifier) for identifier in identifiers),
        )
    if intent.relation_operation != "replace":
        raise WorkspaceError(
            f"Relationship field {intent.field} is outside the remote "
            "many2one import slice",
            load_failure_code="LOAD_RELATIONSHIP_FIELD_FIELD_IS_OUTSIDE_THE_REMOTE_MANY2ONE_IMPORT_SLICE",
        )
    if isinstance(value, LogicalReference) and value.origin == "incoming":
        if not intent.incoming_projection_field:
            if value.dataset is None:
                raise WorkspaceError(
                    "An incoming relationship has no source table identity",
                    load_failure_code="LOAD_AN_INCOMING_RELATIONSHIP_HAS_NO_SOURCE_TABLE_IDENTITY",
                )
            referenced = by_source.get((value.dataset, portable_key(value.key)))
            if referenced is not None and referenced.disposition in {
                "UPDATE",
                "UNCHANGED",
            }:
                identifier = _relation_reference_id(
                    value,
                    intent,
                    metadata,
                    by_source,
                    source_cache,
                    identity_cache,
                    executor,
                )
                return f"{intent.field}/.id", str(identifier)
            return (
                f"{intent.field}/id",
                _relation_external_id(intent, by_source),
            )
        identifier = _relation_reference_id(
            value,
            intent,
            metadata,
            by_source,
            source_cache,
            identity_cache,
            executor,
        )
        return f"{intent.field}/.id", str(identifier)
    if isinstance(value, BusinessReference):
        if value.model != intent.related_model:
            raise WorkspaceError(
                f"Relationship field {intent.field} targets another Odoo model",
                load_failure_code="LOAD_RELATIONSHIP_FIELD_FIELD_TARGETS_ANOTHER_ODOO_MODEL",
            )
        identifier = _relation_reference_id(
            value,
            intent,
            metadata,
            by_source,
            source_cache,
            identity_cache,
            executor,
        )
        return f"{intent.field}/.id", str(identifier)
    raise WorkspaceError(
        f"Relationship field {intent.field} is outside the remote "
        "many2one import slice",
        load_failure_code="LOAD_RELATIONSHIP_FIELD_FIELD_IS_OUTSIDE_THE_REMOTE_MANY2ONE_IMPORT_SLICE",
    )


def _relation_external_id(
    intent: FieldIntent,
    by_source: Mapping[tuple[str, str], ExecutionRow],
) -> str:
    """Resolve an earlier remote import's many2one External ID."""

    value = intent.value
    if (
        intent.relation_operation != "replace"
        or not isinstance(value, LogicalReference)
        or value.origin != "incoming"
        or value.dataset is None
    ):
        raise WorkspaceError(
            f"Relationship field {intent.field} is outside the remote "
            "many2one import slice",
            load_failure_code="LOAD_RELATIONSHIP_FIELD_FIELD_IS_OUTSIDE_THE_REMOTE_MANY2ONE_IMPORT_SLICE",
        )
    referenced = by_source.get((value.dataset, portable_key(value.key)))
    if (
        referenced is None
        or referenced.disposition != "CREATE"
        or not referenced.proposed_external_id
        or referenced.target_model != intent.related_model
    ):
        raise WorkspaceError(
            f"Relationship field {intent.field} has no earlier imported External ID",
            load_failure_code="LOAD_RELATIONSHIP_FIELD_FIELD_HAS_NO_EARLIER_IMPORTED_EXTERNAL_ID",
        )
    return referenced.proposed_external_id


def relation_value(
    intent: FieldIntent,
    metadata: Mapping[str, ExecutionDataset],
    by_source: Mapping[tuple[str, str], ExecutionRow],
    source_cache: dict[tuple[str, str, str, str], int],
    identity_cache: dict[str, int],
    executor: OdooWriteExecutor,
) -> object:
    value = intent.value
    if isinstance(value, tuple):
        identifiers = tuple(
            dict.fromkeys(
                _relation_reference_id(
                    item,
                    intent,
                    metadata,
                    by_source,
                    source_cache,
                    identity_cache,
                    executor,
                )
                for item in value
            )
        )
        # Update snapshots already contain the final canonical set and use
        # replace.  For creates, add starts from an empty relation and
        # remove therefore also has a deterministic final empty set.
        if intent.relation_operation in {"replace", "add"}:
            return [[6, 0, list(identifiers)]]
        if intent.relation_operation == "remove":
            return [[6, 0, []]]
        raise WorkspaceError(
            f"Relationship field {intent.field} has an unsupported operation",
            load_failure_code="LOAD_RELATIONSHIP_FIELD_FIELD_HAS_AN_UNSUPPORTED_OPERATION",
        )
    if intent.relation_operation != "replace":
        raise WorkspaceError(
            f"Relationship field {intent.field} must use replace",
            load_failure_code="LOAD_RELATIONSHIP_FIELD_FIELD_MUST_USE_REPLACE",
        )
    return _relation_reference_id(
        value,
        intent,
        metadata,
        by_source,
        source_cache,
        identity_cache,
        executor,
    )


def _relation_reference_id(
    value: object,
    intent: FieldIntent,
    metadata: Mapping[str, ExecutionDataset],
    by_source: Mapping[tuple[str, str], ExecutionRow],
    source_cache: dict[tuple[str, str, str, str], int],
    identity_cache: dict[str, int],
    executor: OdooWriteExecutor,
) -> int:
    if isinstance(value, LogicalReference) and value.origin == "incoming":
        if value.dataset is None:
            raise WorkspaceError(
                "An incoming relationship is incomplete",
                load_failure_code="LOAD_INCOMING_RELATIONSHIP_INCOMPLETE",
            )
        source_key = (
            value.dataset,
            portable_key(value.key),
            intent.related_model,
            intent.incoming_projection_field,
        )
        if source_key in source_cache:
            return source_cache[source_key]
        referenced = by_source.get((value.dataset, portable_key(value.key)))
        if referenced is None:
            raise WorkspaceError(
                "A related prepared row could not be found",
                load_failure_code="LOAD_RELATED_PREPARED_ROW_MISSING",
            )
        if intent.incoming_projection_field:
            raise WorkspaceError(
                "A generated Odoo relationship receipt was not journalled",
                load_failure_code="LOAD_A_GENERATED_ODOO_RELATIONSHIP_RECEIPT_WAS_NOT_JOURNALLED",
            )
        if referenced.disposition == "CREATE":
            raise WorkspaceError(
                "A related Odoo create did not complete",
                load_failure_code="LOAD_A_RELATED_ODOO_CREATE_DID_NOT_COMPLETE",
            )
        related_id = find_row_id(
            referenced,
            metadata[referenced.dataset],
            identity_cache,
            executor,
        )
        source_cache[
            (
                value.dataset,
                portable_key(value.key),
                referenced.target_model,
                "",
            )
        ] = related_id
        return related_id
    if isinstance(value, BusinessReference | LogicalReference):
        key, scope = value.key, value.scope
    else:
        raise WorkspaceError(
            "A relationship is not expressed by a business key",
            load_failure_code="LOAD_A_RELATIONSHIP_IS_NOT_EXPRESSED_BY_A_BUSINESS_KEY",
        )
    domain = identity_domain(
        intent.related_identity_fields,
        key,
        intent.related_scope_fields,
        scope,
    )
    cache_key = domain_cache_key(intent.related_model, domain)
    return _find_unique(
        intent.related_model,
        domain,
        cache_key,
        identity_cache,
        executor,
    )


def find_row_id(
    row: ExecutionRow,
    dataset: ExecutionDataset,
    identity_cache: dict[str, int],
    executor: OdooWriteExecutor,
) -> int:
    cache_key = identity_cache_key(row)
    if cache_key in identity_cache:
        return identity_cache[cache_key]
    domain = identity_domain(
        dataset.identity_fields,
        row.business_identity,
        dataset.scope_fields,
        row.business_scope,
    )
    return _find_unique(
        row.target_model,
        domain,
        cache_key,
        identity_cache,
        executor,
    )


def _find_unique(
    model: str,
    domain: tuple[tuple[str, str, Any], ...],
    cache_key: str,
    identity_cache: dict[str, int],
    executor: OdooWriteExecutor,
) -> int:
    if cache_key in identity_cache:
        return identity_cache[cache_key]
    raise WorkspaceError(
        "The reviewed Odoo identity crosswalk is incomplete",
        load_failure_code="LOAD_THE_REVIEWED_ODOO_IDENTITY_CROSSWALK_IS_INCOMPLETE",
    )


def _odoo_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is not None:
            normalized = normalized.astimezone(UTC).replace(tzinfo=None)
        return normalized.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, BusinessReference | LogicalReference):
        raise WorkspaceError(
            "A relational business key cannot be used as a scalar",
            load_failure_code="LOAD_A_RELATIONAL_BUSINESS_KEY_CANNOT_BE_USED_AS_A_SCALAR",
        )
    if value is None or type(value) in {str, int, bool, float}:
        return value
    raise WorkspaceError(
        "A prepared value is not supported by the Odoo API",
        load_failure_code="LOAD_A_PREPARED_VALUE_IS_NOT_SUPPORTED_BY_THE_ODOO_API",
    )


def _odoo_import_scalar(value: Any) -> str:
    """Render one scalar for Odoo's text-only ``Model.load`` matrix."""

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is not None:
            normalized = normalized.astimezone(UTC).replace(tzinfo=None)
        return normalized.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, BusinessReference | LogicalReference):
        raise WorkspaceError(
            "A relational business key cannot be used as a scalar",
            load_failure_code="LOAD_A_RELATIONAL_BUSINESS_KEY_CANNOT_BE_USED_AS_A_SCALAR",
        )
    if value is None:
        return ""
    if type(value) is bool:
        return "1" if value else "0"
    if type(value) in {str, int, float}:
        return str(value)
    raise WorkspaceError(
        "A prepared value is not supported by the Odoo API",
        load_failure_code="LOAD_A_PREPARED_VALUE_IS_NOT_SUPPORTED_BY_THE_ODOO_API",
    )
