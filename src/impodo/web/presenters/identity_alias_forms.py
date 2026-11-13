"""Project bounded reference-mapping drafts without persisting business values."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from impodo.domain.mapping.contracts import MAX_VALUE_MAPPINGS, ValueMapping
from impodo.domain.workspace.transfer_identity_aliases import (
    normalize_transfer_identity_aliases,
)


class IdentityAliasForm(Protocol):
    """Read exact repeated form values without retaining the request."""

    def getlist(self, key: str) -> Sequence[object]: ...


def identity_alias_slot_count(aliases: Sequence[ValueMapping]) -> int:
    """Offer existing mappings and one additional pair within the shared limit."""

    return min(len(aliases) + 1, MAX_VALUE_MAPPINGS)


def identity_alias_form_fields(prefix: str, slots: int) -> set[str]:
    """Admit only the bounded source and destination inputs shown by the page."""

    if not 1 <= slots <= MAX_VALUE_MAPPINGS:
        raise ValueError("The reference mapping form exceeds its supported size")
    return {
        f"{prefix}_{kind}_{index}"
        for index in range(slots)
        for kind in ("source", "target")
    }


def identity_alias_form_drafts(
    form: IdentityAliasForm, *, prefix: str, slots: int
) -> tuple[tuple[str, str], ...]:
    """Retain entered values for a failed check without accepting them as rules."""

    identity_alias_form_fields(prefix, slots)
    rows = []
    for index in range(slots):
        values = []
        for kind in ("source", "target"):
            submitted = form.getlist(f"{prefix}_{kind}_{index}")
            if len(submitted) > 1 or any(
                not isinstance(value, str) for value in submitted
            ):
                raise ValueError(
                    "Enter one source and destination value for each mapping"
                )
            values.append(str(submitted[0]) if submitted else "")
        rows.append((values[0], values[1]))
    return tuple(rows)


def parse_identity_alias_drafts(
    drafts: Sequence[tuple[str, str]],
) -> tuple[ValueMapping, ...]:
    """Require complete exact choices while keeping blank pairs unused."""

    aliases = []
    for source, target in drafts:
        if not source.strip() and not target.strip():
            continue
        if not source.strip() or not target.strip():
            raise ValueError("Enter both the source value and its destination value")
        aliases.append(ValueMapping(source, target))
    return normalize_transfer_identity_aliases(aliases)


def identity_alias_form_rows(
    aliases: Sequence[ValueMapping] = (),
    *,
    drafts: Sequence[tuple[str, str]] | None = None,
) -> tuple[dict[str, object], ...]:
    """Render approved choices or an unsaved draft, followed by one empty pair."""

    pairs = (
        tuple(drafts)
        if drafts is not None
        else tuple((item.source_value, item.target_value) for item in aliases)
    )
    if not pairs or drafts is None and any(pairs[-1]):
        pairs = (*pairs, ("", ""))
    return tuple(
        {"index": index, "source_value": source, "target_value": target}
        for index, (source, target) in enumerate(pairs[:MAX_VALUE_MAPPINGS])
    )
