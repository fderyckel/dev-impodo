"""Select a Match data page without changing saved workflow evidence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict


class MappingJourneyStep(TypedDict):
    """One link in the Stage 3 authoring journey."""

    key: str
    label: str
    href: str


class MappingJourney(TypedDict):
    """Display state for one existing Match data editor and its controls."""

    step: str
    task: str
    company_required: bool
    steps: tuple[MappingJourneyStep, ...]


MAPPING_JOURNEY_STEPS = (
    ("company", "Choose company"),
    ("tables", "Match tables"),
    ("check", "Check and resolve"),
    ("confirm", "Confirm matches"),
)
MAPPING_TABLE_TASKS = frozenset({"rows", "identity", "fields", "links"})


def resolve_mapping_journey(
    query_params: Mapping[str, str],
    company_required: bool,
    *,
    mapping_href: str = "",
) -> MappingJourney:
    """Whitelist display choices while leaving all authorization gates intact.

    Missing or invalid locations start at the required company choice, or the
    table editor after that choice is available. Explicit locations remain
    usable for correction links; they never establish saved completion.
    """

    step = query_params.get("step", "")
    if step not in {key for key, _label in MAPPING_JOURNEY_STEPS}:
        step = "company" if company_required else "tables"
    task = query_params.get("task", "rows")
    if task not in MAPPING_TABLE_TASKS:
        task = "rows"
    return {
        "step": step,
        "task": task,
        "company_required": company_required,
        "steps": tuple(
            {
                "key": key,
                "label": label,
                "href": f"{mapping_href}?step={key}",
            }
            for key, label in MAPPING_JOURNEY_STEPS
        ),
    }
