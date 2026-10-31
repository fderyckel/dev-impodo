"""Publish protected identity-group members from the explicit check's indexes.

Included source positions identify rows within this check, not physical file
rows. Target positions number the observed matches and never carry Odoo IDs.
Pages consume the persisted index through the existing authorized review port.
"""

from collections import defaultdict

from impodo.domain.matching_order import (
    MatchingIdentityChoice,
    MatchingIdentityChoiceCause,
    MatchingIdentityKeyIndex,
)
from impodo.domain.serialization import content_hash
from impodo.domain.workspace.errors import WorkspaceError


def identity_duplicate_choices(
    index: MatchingIdentityKeyIndex,
    *,
    dataset_id: str,
    target_model: str,
    display_fields: tuple[str, ...],
    display_keys: tuple[tuple[str | None, ...], ...],
) -> tuple[MatchingIdentityChoice, ...]:
    """Reuse normalized counters and row-aligned recursive business-key leaves."""
    if len(display_keys) != len(index.source_rows):
        raise WorkspaceError("Identity review rows do not agree with the check")
    duplicate_keys = {
        key
        for key, count in index.source_counts.items()
        if count > 1 or index.target_counts[key] > 1
    }
    if not duplicate_keys:
        return ()
    positions: dict[tuple[str | None, ...], list[int]] = defaultdict(list)
    display_by_key: dict[tuple[str | None, ...], tuple[str, ...]] = {}
    for position, (key, display) in enumerate(
        zip(index.source_rows, display_keys, strict=True), 1
    ):
        if key not in duplicate_keys:
            continue
        if len(display) != len(display_fields) or any(
            value is None for value in display
        ):
            raise WorkspaceError(
                "The complete identity is unavailable for group review"
            )
        positions[key].append(position)
        display_by_key.setdefault(key, tuple(str(value).strip() for value in display))

    rows = []
    for key in sorted(duplicate_keys):
        source_count, target_count = index.source_counts[key], index.target_counts[key]
        causes = []
        if source_count > 1:
            causes.append(MatchingIdentityChoiceCause.REPEATED_SOURCE)
        if target_count > 1:
            causes.append(MatchingIdentityChoiceCause.AMBIGUOUS_IDENTITY_TARGET)
        for cause in causes:
            group_hash = content_hash(
                {
                    "dataset": dataset_id,
                    "model": target_model,
                    "fields": display_fields,
                    "key": display_by_key[key],
                    "cause": cause.value,
                }
            )
            members = [("source", position) for position in positions[key]]
            if cause is MatchingIdentityChoiceCause.AMBIGUOUS_IDENTITY_TARGET:
                members.extend(
                    ("target", position) for position in range(1, target_count + 1)
                )
            for origin, position in members:
                rows.append(
                    MatchingIdentityChoice(
                        ordinal=len(rows),
                        dataset_id=dataset_id,
                        target_field=index.target_fields[0],
                        related_model=target_model,
                        related_fields=display_fields,
                        key=display_by_key[key],
                        cause=cause,
                        target_match_count=target_count,
                        incoming_match_count=0,
                        group_hash=group_hash,
                        member_origin=origin,
                        member_position=position,
                        source_match_count=source_count,
                    )
                )
    return tuple(rows)
