"""Build protected review rows while relationship classification is in memory.

No repository or Odoo call belongs here. The ordinary health result remains
aggregate-only; the explicit review retains only the selected business keys.
"""

from collections.abc import Mapping

from impodo.domain.relationship_health import RelationshipHealthResult
from impodo.domain.relationship_review import (
    RelationshipChoice,
    RelationshipChoiceCause,
    RelationshipChoiceResolution,
)
from impodo.domain.workspace.errors import WorkspaceError


def append_relationship_choices(
    result: RelationshipHealthResult,
    *,
    key_fields: tuple[str, ...],
    resolutions: list[RelationshipChoiceResolution],
    display_keys: Mapping[tuple[str, ...], tuple[str, ...]],
    affected_rows: Mapping[tuple[str, ...], int],
    incomplete_keys: tuple[tuple[tuple[str | None, ...], int], ...],
    choices: list[RelationshipChoice],
) -> None:
    """Retain exact causes and impact without reclassifying or scanning sources."""

    def add(key, cause, occurrences, affected, target=0, incoming=0, origin="source"):
        width = max(len(key_fields), len(key))
        fields = (
            *key_fields,
            *(
                f"source_component_{index + 1}"
                for index in range(len(key_fields), width)
            ),
        )
        values = (*key, *(None for _ in range(len(key), width)))
        choices.append(
            RelationshipChoice(
                ordinal=len(choices),
                owner_dataset_id=result.owner_dataset_id,
                target_field=result.target_field,
                relationship_kind=result.relationship_kind,
                resolver_origin=result.resolver_origin,
                related_model=result.related_model,
                dependency_dataset_id=result.dependency_dataset_id,
                key_fields=fields,
                key=values,
                cause=cause,
                occurrence_count=occurrences,
                affected_row_count=affected,
                target_match_count=target,
                incoming_match_count=incoming,
                match_origin=origin,
            )
        )

    for resolution in resolutions:
        key = display_keys.get(resolution.key, resolution.key)
        add(
            key,
            resolution.cause,
            resolution.occurrence_count,
            affected_rows.get(resolution.key, resolution.occurrence_count),
            resolution.target_match_count,
            resolution.incoming_match_count,
            resolution.match_origin,
        )
    if sum(count for _key, count in incomplete_keys) != result.incomplete_row_count:
        raise WorkspaceError(
            "Incomplete relationship evidence does not agree with health counts"
        )
    for key, count in incomplete_keys:
        add(key, RelationshipChoiceCause.INCOMPLETE, count, count)
    if result.required and result.blank_row_count:
        add(
            (None,) * len(key_fields),
            RelationshipChoiceCause.REQUIRED_BLANK,
            result.blank_row_count,
            result.blank_row_count,
        )
