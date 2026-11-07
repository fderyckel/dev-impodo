"""Read bounded record details and explain missing source matches without writes."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import duckdb

from impodo.application.shared.artifacts import WorkspaceArtifactStore
from impodo.domain.preparation.quality import QualityCauseCount
from impodo.domain.preparation.staging_contracts import CanonicalRow
from impodo.domain.structural import ExactJoinRule, JoinKind, structural_dataset_id
from impodo.domain.workspace.derived_entities import DerivedEntityPlan

from .quality_review_projection import read_projected_review_rows


def read_prepared_review_rows(
    connection: duckdb.DuckDBPyConnection,
    staging_run_id: str,
    row_ids: Sequence[str],
    *,
    workspace_id: str,
    artifacts: WorkspaceArtifactStore,
) -> dict[str, CanonicalRow]:
    """Fetch only the canonical records on the requested review page."""

    if not row_ids:
        return {}
    placeholders = ", ".join("?" for _ in row_ids)
    stored = connection.execute(
        f"""
        SELECT row_id, row_json FROM canonical_staging_row
         WHERE run_id = ? AND row_id IN ({placeholders})
        """,
        [staging_run_id, *row_ids],
    ).fetchall()
    result = {
        str(row_id): CanonicalRow.from_dict(json.loads(str(payload)))
        for row_id, payload in stored
        if str(payload)
    }
    projected_ids = [str(row_id) for row_id, payload in stored if not str(payload)]
    if projected_ids:
        result.update(
            read_projected_review_rows(
                connection,
                staging_run_id,
                projected_ids,
                workspace_id=workspace_id,
                artifacts=artifacts,
            )
        )
    return result


def explain_source_match_causes(
    connection: duckdb.DuckDBPyConnection,
    run_id: str,
    staging_run_id: str,
    causes: tuple[QualityCauseCount, ...],
) -> tuple[QualityCauseCount, ...]:
    """Prove complete cause membership against the saved join and row lineage."""

    invalid = tuple(
        cause
        for cause in causes
        if cause.reason_code == "SOURCE_IDENTITY_INVALID" and cause.cause_key
    )
    if not invalid:
        return causes
    saved = connection.execute(
        """
        SELECT plan.plan_json, selection.selection_json
          FROM canonical_staging_run AS staging
          JOIN derived_entity_plan_revision AS plan
            ON plan.content_hash = staging.derived_plan_hash
          CROSS JOIN source_selection AS selection
         WHERE staging.run_id = ?
         ORDER BY plan.version DESC LIMIT 1
        """,
        [staging_run_id],
    ).fetchone()
    if saved is None:
        return causes
    plan = DerivedEntityPlan.from_json(str(saved[0]))
    selection = json.loads(str(saved[1]))
    if plan.source_selection_hash != selection["content_hash"]:
        return causes
    origins = _optional_join_fields(
        plan,
        {item["dataset_id"]: item["name"] for item in selection["datasets"]},
    )
    candidates = [
        (cause.cause_key, field, *origins[(cause.dataset, field)])
        for cause in invalid
        for field in cause.affected_fields
        if (cause.dataset, field) in origins
    ]
    if not candidates:
        return causes
    # Aggregate all direct members, independent of the visible page/filter.
    values = ", ".join("(?, ?, ?, ?)" for _ in candidates)
    findings = connection.execute(
        f"""
        WITH candidates(cause_key, field, source_id, source_name) AS
             (VALUES {values})
        SELECT candidate.cause_key, candidate.source_name,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE
                   NOT list_contains(
                       json_keys(NULLIF(row.row_json, ''), '$.lineage.physical_sources'),
                       candidate.source_id)
                   AND list_contains(
                       CAST(json_extract(NULLIF(row.row_json, ''),
                           '$.lineage.field_sources."$source_identity"')
                           AS VARCHAR[]), candidate.field)
               ) AS missing_count
          FROM candidates AS candidate
          JOIN quality_cause_member AS member
            ON member.run_id = ? AND member.cause_key = candidate.cause_key
           AND member.impact_kind = 'DIRECT'
          JOIN canonical_staging_row AS row
            ON row.run_id = ? AND row.row_id = member.row_id
         GROUP BY candidate.cause_key, candidate.source_name
        """,
        [
            *(value for candidate in candidates for value in candidate),
            run_id,
            staging_run_id,
        ],
    ).fetchall()
    counts = {cause.cause_key: cause.affected_record_count for cause in invalid}
    tables = {
        str(key): str(table)
        for key, table, total, missing in findings
        if int(total) == int(missing) == counts[str(key)]
    }
    field_names = {
        (rule.output_dataset_name, column.column_key): column.source_name
        for rule in plan.rules
        if isinstance(rule, ExactJoinRule)
        for column in rule.output_columns
    }
    explained: dict[str, str] = {}
    duplicates = [
        cause
        for cause in causes
        if cause.reason_code == "SOURCE_IDENTITY_DUPLICATE" and cause.cause_key
    ]
    pairs = [
        (duplicate.cause_key, cause.cause_key)
        for duplicate in duplicates
        for cause in invalid
        if cause.cause_key in tables
        and cause.dataset == duplicate.dataset
        and cause.policy == duplicate.policy
        and cause.affected_record_count == duplicate.affected_record_count
    ]
    if pairs:
        pair_values = ", ".join("(?, ?)" for _ in pairs)
        matches = connection.execute(
            f"""
            WITH pairs(duplicate_key, invalid_key) AS (VALUES {pair_values}),
            members AS (SELECT * FROM quality_cause_member WHERE run_id = ?),
            sizes AS (SELECT cause_key, COUNT(*) AS total FROM members GROUP BY cause_key)
            SELECT pairs.duplicate_key, pairs.invalid_key
              FROM pairs
                 JOIN members AS duplicate
                   ON duplicate.cause_key = pairs.duplicate_key
                 LEFT JOIN quality_cause_member AS invalid
                   ON invalid.run_id = duplicate.run_id
                  AND invalid.cause_key = pairs.invalid_key
                  AND invalid.row_id = duplicate.row_id
                  AND invalid.impact_kind = duplicate.impact_kind
                 JOIN canonical_staging_row AS row
                   ON row.run_id = ? AND row.row_id = duplicate.row_id
                 JOIN sizes ON sizes.cause_key = pairs.invalid_key
             GROUP BY pairs.duplicate_key, pairs.invalid_key
             HAVING COUNT(*) = MAX(sizes.total)
                AND COUNT(*) FILTER (WHERE invalid.row_id IS NULL OR
                       CAST(json_extract(row.row_json, '$.source_identity')
                            AS VARCHAR) != '[null]') = 0
            """,
            [*(value for pair in pairs for value in pair), run_id, staging_run_id],
        ).fetchall()
        explained = {str(duplicate): str(invalid) for duplicate, invalid in matches}
    return tuple(
        replace(
            cause,
            missing_match_table=tables.get(cause.cause_key, ""),
            missing_match_field_label=", ".join(
                field_names.get((cause.dataset, field), field)
                for field in cause.affected_fields
            )
            if cause.cause_key in tables
            else "",
            explained_by_cause_key=explained.get(cause.cause_key, ""),
        )
        for cause in causes
    )


def _optional_join_fields(
    plan: DerivedEntityPlan,
    physical: dict[str, str],
) -> dict[tuple[str, str], tuple[str, str]]:
    """Trace projected columns back to an optional physical right-hand table."""

    joins = {
        structural_dataset_id(rule): rule
        for rule in plan.rules
        if isinstance(rule, ExactJoinRule)
    }

    def origin(dataset_id: str, field: str) -> tuple[str, str] | None:
        seen: set[str] = set()
        while dataset_id in joins and dataset_id not in seen:
            seen.add(dataset_id)
            rule = joins[dataset_id]
            projection = next(
                (item for item in rule.projections if item.output_column_key == field),
                None,
            )
            if projection is None:
                return None
            if (
                projection.source_dataset_id == rule.right_dataset_id
                and rule.kind is JoinKind.LEFT
                and rule.right_dataset_id in physical
            ):
                return rule.right_dataset_id, physical[rule.right_dataset_id]
            dataset_id, field = (
                projection.source_dataset_id,
                projection.source_column_key,
            )
        return None

    result = {}
    for dataset_id, rule in joins.items():
        for column in rule.output_columns:
            source = origin(dataset_id, column.column_key)
            if source is not None:
                result[(rule.output_dataset_name, column.column_key)] = source
    return result


def read_prepared_field_labels(
    connection: duckdb.DuckDBPyConnection,
    rows: dict[str, CanonicalRow],
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Select captured labels for just the models represented on this page."""

    models = sorted({row.target_model for row in rows.values()})
    if not models:
        return {}
    placeholders = ", ".join("?" for _ in models)
    labels: dict[str, list[tuple[str, str]]] = {}
    for model, field, label in connection.execute(
        f"""
        SELECT json_extract_string(model.value, '$.name'),
               json_extract_string(field.value, '$.name'),
               json_extract_string(field.value, '$.label')
          FROM odoo_schema_catalog AS catalog,
               json_each(catalog.catalog_json, '$.models') AS model,
               json_each(model.value, '$.fields') AS field
         WHERE json_extract_string(model.value, '$.name') IN ({placeholders})
        """,
        models,
    ).fetchall():
        labels.setdefault(str(model), []).append((str(field), str(label)))
    return {model: tuple(fields) for model, fields in labels.items()}


def read_prepared_identity_values(
    connection: duckdb.DuckDBPyConnection,
    staging_run_id: str,
    rows: dict[str, CanonicalRow],
) -> dict[str, tuple[tuple[str, Any], ...]]:
    """Label saved identity components using their exact authored mapping."""

    if not rows:
        return {}
    saved = connection.execute(
        """
        SELECT revision.revision_json
          FROM canonical_staging_run AS staging
          JOIN mapping_revision AS revision
            ON revision.content_hash = staging.mapping_hash
         WHERE staging.run_id = ? ORDER BY revision.version DESC LIMIT 1
        """,
        [staging_run_id],
    ).fetchone()
    if saved is None:
        return {}
    definition = json.loads(str(saved[0])).get("definition")
    if not isinstance(definition, dict):
        return {}
    mappings = definition["datasets"]
    counts = Counter(mapping["target_model"] for mapping in mappings)
    by_model = {
        mapping["target_model"]: mapping
        for mapping in mappings
        if counts[mapping["target_model"]] == 1
    }
    result = {}
    for row in rows.values():
        mapping = by_model.get(row.target_model)
        if mapping is None:
            continue
        values = []
        for role, components in (
            ("target_identity", row.target_identity),
            ("target_scope", row.target_scope),
        ):
            fields = [
                field
                for component in mapping[role]
                for field in component["target_fields"]
            ]
            if len(fields) == len(components):
                values.extend(zip(fields, components, strict=True))
        result[row.row_id] = tuple(values)
    return result
