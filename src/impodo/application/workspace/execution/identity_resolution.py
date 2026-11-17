"""Verify reviewed target identities before any write is journalled."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from impodo.domain.execution.identity import identity_domain, portable_key
from impodo.domain.execution.odoo_write import (
    MAX_IDENTITY_LOOKUP_KEYS,
    OdooIdentityLookupUnavailable,
    OdooWriteExecutor,
    OdooWriteRejected,
)
from impodo.domain.execution_snapshot import ExecutionDataset, ExecutionRow
from impodo.domain.shared.models import (
    BusinessReference,
    LogicalReference,
    canonical_json_text,
    portable_value,
    target_record_binding_hash,
)
from impodo.domain.workspace.errors import WorkspaceError

from .identity_lookups import NestedIdentityLookup


def assert_create_rows_still_absent(
    write_rows: Sequence[ExecutionRow],
    metadata: Mapping[str, ExecutionDataset],
    executor: OdooWriteExecutor,
) -> None:
    """Fail before journaling when a reviewed create key now exists."""

    nested = NestedIdentityLookup(
        write_rows, metadata, executor.find_ids_many, page_size=MAX_IDENTITY_LOOKUP_KEYS
    )
    by_model: dict[str, list[tuple[tuple[str, str, Any], ...]]] = {}
    for row in write_rows:
        if row.disposition != "CREATE":
            continue
        dataset = metadata[row.dataset]
        domain = identity_domain(
            dataset.identity_fields,
            row.business_identity,
            dataset.scope_fields,
            row.business_scope,
        )
        by_model.setdefault(row.target_model, []).append(domain)
    try:
        for model in sorted(by_model):
            domains = by_model[model]
            for start in range(0, len(domains), MAX_IDENTITY_LOOKUP_KEYS):
                page = tuple(domains[start : start + MAX_IDENTITY_LOOKUP_KEYS])
                matches = executor.find_ids_many(model, nested.translate(model, page))
                if len(matches) != len(page):
                    raise WorkspaceError(
                        "Odoo returned an incomplete create-key check",
                        load_failure_code="LOAD_ODOO_RETURNED_AN_INCOMPLETE_CREATE_KEY_CHECK",
                    )
                if any(identifiers for identifiers in matches):
                    raise WorkspaceError(
                        "An Odoo business key marked for creation now exists; "
                        "run destination preflight again",
                        load_failure_code="LOAD_AN_ODOO_BUSINESS_KEY_MARKED_FOR_CREATION_NOW_EXISTS_RUN_DESTINATION_PREFLIGHT_AGAIN",
                    )
    except OdooWriteRejected as error:
        raise WorkspaceError(
            "Odoo create-key verification failed before loading",
            load_failure_code="LOAD_ODOO_CREATE_KEY_VERIFICATION_FAILED_BEFORE_LOADING",
        ) from error


def resolve_identity_crosswalk(
    write_rows: Sequence[ExecutionRow],
    metadata: Mapping[str, ExecutionDataset],
    by_source: Mapping[tuple[str, str], ExecutionRow],
    executor: OdooWriteExecutor,
) -> dict[str, int]:
    """Bulk-verify every existing target before journaling any write."""

    nested = NestedIdentityLookup(
        by_source.values(),
        metadata,
        executor.find_ids_many,
        page_size=MAX_IDENTITY_LOOKUP_KEYS,
    )
    requirements: dict[str, dict[str, Any]] = {}

    def add_requirement(
        model: str,
        domain: tuple[tuple[str, str, Any], ...],
        expected_binding_hash: str,
        *aliases: str,
    ) -> None:
        key = domain_cache_key(model, domain)
        current = requirements.get(key)
        if current is None:
            requirements[key] = {
                "model": model,
                "domain": domain,
                "expected": expected_binding_hash,
                "aliases": {key, *aliases},
            }
            return
        if (
            current["model"] != model
            or current["domain"] != domain
            or (
                current["expected"]
                and expected_binding_hash
                and current["expected"] != expected_binding_hash
            )
        ):
            raise WorkspaceError(
                "The reviewed Odoo identity bindings conflict",
                load_failure_code="LOAD_THE_REVIEWED_ODOO_IDENTITY_BINDINGS_CONFLICT",
            )
        if not current["expected"]:
            current["expected"] = expected_binding_hash
        current["aliases"].update(aliases)

    def add_row(row: ExecutionRow) -> None:
        dataset = metadata[row.dataset]
        domain = identity_domain(
            dataset.identity_fields,
            row.business_identity,
            dataset.scope_fields,
            row.business_scope,
        )
        add_requirement(
            row.target_model,
            domain,
            row.target_binding_hash,
            identity_cache_key(row),
        )

    for row in write_rows:
        if row.disposition == "UPDATE":
            add_row(row)
        for intent in row.fields:
            if intent.action != "SET_VALUE" or intent.kind != "relation":
                continue
            values = (
                intent.value if isinstance(intent.value, tuple) else (intent.value,)
            )
            bindings = intent.target_binding_hashes or ("",) * len(values)
            for value, binding_hash in zip(values, bindings, strict=True):
                if isinstance(value, LogicalReference) and value.origin == "incoming":
                    if value.dataset is None:
                        raise WorkspaceError(
                            "An incoming relationship is incomplete",
                            load_failure_code="LOAD_INCOMING_RELATIONSHIP_INCOMPLETE",
                        )
                    referenced = by_source.get((value.dataset, portable_key(value.key)))
                    if referenced is None:
                        raise WorkspaceError(
                            "A related prepared row could not be found",
                            load_failure_code="LOAD_RELATED_PREPARED_ROW_MISSING",
                        )
                    if referenced.disposition != "CREATE":
                        add_row(referenced)
                    continue
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
                add_requirement(
                    intent.related_model,
                    domain,
                    binding_hash,
                )

    identity_cache: dict[str, int] = {}
    by_model: dict[str, list[dict[str, Any]]] = {}
    for requirement in requirements.values():
        by_model.setdefault(requirement["model"], []).append(requirement)
    try:
        for model in sorted(by_model):
            model_requirements = sorted(
                by_model[model],
                key=lambda item: domain_cache_key(model, item["domain"]),
            )
            for start in range(
                0,
                len(model_requirements),
                MAX_IDENTITY_LOOKUP_KEYS,
            ):
                page = model_requirements[start : start + MAX_IDENTITY_LOOKUP_KEYS]
                matches = executor.find_ids_many(
                    model,
                    nested.translate(model, tuple(item["domain"] for item in page)),
                )
                if len(matches) != len(page):
                    raise WorkspaceError(
                        "Odoo returned an incomplete identity crosswalk",
                        load_failure_code="LOAD_ODOO_RETURNED_AN_INCOMPLETE_IDENTITY_CROSSWALK",
                    )
                for requirement, identifiers in zip(
                    page,
                    matches,
                    strict=True,
                ):
                    if len(identifiers) != 1:
                        raise WorkspaceError(
                            f"The Odoo business key for {model} matched "
                            f"{len(identifiers)} records; compare again",
                            load_failure_code="LOAD_THE_ODOO_BUSINESS_KEY_FOR_CONFIGURED_CONTEXT_MATCHED_CONFIGURED_CONTEXT_RECORDS_COMPARE_AGAIN",
                        )
                    identifier = identifiers[0]
                    expected = requirement["expected"]
                    if (
                        expected
                        and target_record_binding_hash(model, identifier) != expected
                    ):
                        raise WorkspaceError(
                            "An Odoo business key now targets a different record; compare again",
                            load_failure_code="LOAD_AN_ODOO_BUSINESS_KEY_NOW_TARGETS_A_DIFFERENT_RECORD_COMPARE_AGAIN",
                        )
                    for alias in requirement["aliases"]:
                        identity_cache[alias] = identifier
    except OdooIdentityLookupUnavailable as error:
        raise WorkspaceError(
            f"{error} This load stopped before sending any records. "
            "Check the Odoo connection, then run Check changes again.",
            load_failure_code=error.load_failure_code,
        ) from error
    except OdooWriteRejected as error:
        raise WorkspaceError(
            "Odoo identity verification failed before loading",
            load_failure_code="LOAD_ODOO_IDENTITY_VERIFICATION_FAILED_BEFORE_LOADING",
        ) from error
    return identity_cache


def identity_cache_key(row: ExecutionRow) -> str:
    return canonical_json_text(
        {
            "model": row.target_model,
            "identity": portable_value(row.business_identity),
            "scope": portable_value(row.business_scope),
        }
    )


def domain_cache_key(
    model: str,
    domain: tuple[tuple[str, str, Any], ...],
) -> str:
    return canonical_json_text({"model": model, "domain": domain})
