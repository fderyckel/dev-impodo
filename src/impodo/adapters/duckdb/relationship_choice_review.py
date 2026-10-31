"""Publish and page the protected relationship-choice index in its owner's scope."""

import json
from collections import Counter

import duckdb

from impodo.domain.matching_order import MatchingOrderCheck
from impodo.domain.relationship_review import (
    MAX_RELATIONSHIP_CHOICE_PAGE_SIZE,
    RelationshipChoice,
    RelationshipChoiceCause,
    RelationshipChoiceFilter,
    RelationshipChoicePage,
)
from impodo.domain.serialization import canonical_json
from impodo.domain.workspace.errors import WorkspaceError

from .constants import DUCKDB_JSON_BATCH_MAX_BYTES
from .serialization import iter_encoded_json_batches


def published_relationship_choices(
    check: MatchingOrderCheck, payload: dict
) -> tuple[RelationshipChoice, ...]:
    """Reject partial, duplicated, or unrelated detail before publication."""
    raw = payload.get("relationship_choices", [])
    if not isinstance(raw, list):
        raise WorkspaceError("Relationship-choice evidence is invalid")
    choices = tuple(RelationshipChoice.from_dict(item) for item in raw)
    if check.relationship_review_version == 0:
        if choices:
            raise WorkspaceError("Relationship choices require a versioned check")
        return ()
    if (
        type(payload.get("relationship_review_version")) is not int
        or payload.get("relationship_review_version") != 1
    ):
        raise WorkspaceError("The relationship review receipt is missing")
    results = {
        (item.owner_dataset_id, item.target_field): item
        for item in check.relationship_health_results
        if item.checked
    }
    totals: Counter[tuple[str, str, RelationshipChoiceCause]] = Counter()
    seen = set()
    for ordinal, choice in enumerate(choices):
        result = results.get((choice.owner_dataset_id, choice.target_field))
        meaning = (
            choice.owner_dataset_id,
            choice.target_field,
            choice.key_fields,
            choice.key,
            choice.cause,
        )
        if (
            result is None
            or choice.ordinal != ordinal
            or meaning in seen
            or (
                choice.related_model,
                choice.relationship_kind,
                choice.resolver_origin,
                choice.dependency_dataset_id,
            )
            != (
                result.related_model,
                result.relationship_kind,
                result.resolver_origin,
                result.dependency_dataset_id,
            )
            or choice.affected_row_count > result.source_row_count
        ):
            raise WorkspaceError(
                "Relationship-choice detail does not agree with the checked field"
            )
        seen.add(meaning)
        totals[(choice.owner_dataset_id, choice.target_field, choice.cause)] += (
            choice.occurrence_count
        )
    for (dataset, field), result in results.items():
        expected = {
            RelationshipChoiceCause.MISSING: result.missing_count,
            RelationshipChoiceCause.AMBIGUOUS: result.ambiguous_count,
            RelationshipChoiceCause.CASE_ONLY: result.case_mismatch_count,
            RelationshipChoiceCause.INCOMPLETE: result.incomplete_row_count,
            RelationshipChoiceCause.REQUIRED_BLANK: result.blank_row_count
            if result.required
            else 0,
        }
        if any(
            totals[(dataset, field, cause)] != count
            for cause, count in expected.items()
        ):
            raise WorkspaceError(
                "Relationship-choice totals do not agree with aggregate health"
            )
    return choices


def insert_relationship_choices(
    connection: duckdb.DuckDBPyConnection,
    check_id: str,
    choices: tuple[RelationshipChoice, ...],
) -> None:
    """Insert bounded batches inside the caller's existing check transaction."""
    rows = (
        {
            "ordinal": item.ordinal,
            "owner_dataset_id": item.owner_dataset_id,
            "target_field": item.target_field,
            "cause": item.cause.value,
            "occurrence_count": item.occurrence_count,
            "affected_row_count": item.affected_row_count,
            "choice_json": canonical_json(item.protected_dict()),
        }
        for item in choices
    )
    structure = canonical_json(
        [
            {
                "ordinal": "BIGINT",
                "owner_dataset_id": "VARCHAR",
                "target_field": "VARCHAR",
                "cause": "VARCHAR",
                "occurrence_count": "BIGINT",
                "affected_row_count": "BIGINT",
                "choice_json": "VARCHAR",
            }
        ]
    )
    for batch in iter_encoded_json_batches(
        rows, max_rows=1000, max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES
    ):
        connection.execute(
            """
            INSERT INTO matching_order_protected_relationship_choice
            SELECT ?, item.ordinal, item.owner_dataset_id, item.target_field, item.cause,
                   item.occurrence_count, item.affected_row_count, item.choice_json
              FROM (SELECT unnest(from_json_strict(CAST(? AS JSON), ?)) AS item)
        """,
            [check_id, batch.payload, structure],
        )


def relationship_choice_page(
    connection: duckdb.DuckDBPyConnection,
    check_id: str,
    filters: RelationshipChoiceFilter,
    *,
    page_size: int,
    after: int | None = None,
    before: int | None = None,
) -> RelationshipChoicePage:
    """Read only indexed headers and at most one page of protected JSON rows."""
    if not 1 <= page_size <= MAX_RELATIONSHIP_CHOICE_PAGE_SIZE or (
        after is not None and before is not None
    ):
        raise WorkspaceError("Choose one bounded relationship-choice page")
    if any(value is not None and value < 0 for value in (after, before)):
        raise WorkspaceError("Relationship choice cursor is invalid")
    current = connection.execute(
        "SELECT check_id FROM matching_order_check_current WHERE singleton_id = 1"
    ).fetchone()
    if current is None or str(current[0]) != check_id:
        raise WorkspaceError("The relationship check changed; review it again")
    clauses = ["check_id = ?"]
    parameters: list[object] = [check_id]
    for column, value in (
        ("owner_dataset_id", filters.owner_dataset_id),
        ("target_field", filters.target_field),
        ("cause", filters.cause),
    ):
        if value:
            clauses.append(f"{column} = ?")
            parameters.append(value)
    where = " AND ".join(clauses)
    totals = connection.execute(
        f"SELECT COUNT(*), COALESCE(SUM(occurrence_count), 0) FROM matching_order_protected_relationship_choice WHERE {where}",
        parameters,
    ).fetchone()
    count, occurrences = (int(totals[0]), int(totals[1])) if totals else (0, 0)
    cursor = (
        " AND ordinal > ?"
        if after is not None
        else " AND ordinal < ?"
        if before is not None
        else ""
    )
    direction = "DESC" if before is not None else "ASC"
    cursor_values = (
        [after] if after is not None else [before] if before is not None else []
    )
    records = connection.execute(
        f"""
        SELECT ordinal, choice_json, owner_dataset_id, target_field, cause, occurrence_count, affected_row_count
          FROM matching_order_protected_relationship_choice WHERE {where}{cursor}
         ORDER BY ordinal {direction} LIMIT ?
    """,
        [*parameters, *cursor_values, page_size],
    ).fetchall()
    if direction == "DESC":
        records.reverse()
    rows = []
    for record in records:
        choice = RelationshipChoice.from_dict(json.loads(str(record[1])))
        if (
            choice.ordinal,
            choice.owner_dataset_id,
            choice.target_field,
            choice.cause.value,
            choice.occurrence_count,
            choice.affected_row_count,
        ) != (
            int(record[0]),
            str(record[2]),
            str(record[3]),
            str(record[4]),
            int(record[5]),
            int(record[6]),
        ):
            raise WorkspaceError("Stored relationship-choice headers are inconsistent")
        rows.append(choice)
    preceding = (
        connection.execute(
            f"SELECT COUNT(*) FROM matching_order_protected_relationship_choice WHERE {where} AND ordinal < ?",
            [*parameters, rows[0].ordinal],
        ).fetchone()
        if rows
        else None
    )
    start = int(preceding[0]) + 1 if preceding else 0
    end = start + len(rows) - 1 if rows else 0
    return RelationshipChoicePage(
        tuple(rows),
        count,
        occurrences,
        start,
        end,
        rows[0].ordinal if rows and start > 1 else None,
        rows[-1].ordinal if rows and end < count else None,
        check_id,
    )
