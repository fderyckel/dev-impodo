"""Select an Odoo-data panel from saved evidence without changing the workflow."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict


class SchemaJourneyStep(TypedDict):
    """One Stage 2 location and its evidence-derived display status."""

    key: str
    label: str
    href: str
    status: str
    status_label: str


class SchemaJourney(TypedDict):
    """Display state for the existing Odoo-data forms."""

    enabled: bool
    step: str
    steps: tuple[SchemaJourneyStep, ...]


SCHEMA_JOURNEY_STEPS = (
    ("records", "Choose records"),
    ("related", "Related values"),
    ("matching", "Matching rules"),
)


def resolve_schema_journey(
    query_params: Mapping[str, str],
    *,
    enabled: bool,
    records_complete: bool = False,
    records_attention: bool = False,
    related_complete: bool = False,
    related_attention: bool = False,
    related_optional: bool = False,
    matching_complete: bool = False,
    matching_attention: bool = False,
    preferred_step: str = "",
    schema_href: str = "",
) -> SchemaJourney:
    """Resolve a view while keeping saved completion separate from navigation.

    Recovery selects its owning panel. Otherwise a valid explicit location
    wins, and a normal return opens the first responsibility needing work.
    These display choices never establish permission or confirm a decision.
    """

    if not enabled:
        return {"enabled": False, "step": "records", "steps": ()}
    keys = {key for key, _label in SCHEMA_JOURNEY_STEPS}
    requested_step = query_params.get("step", "")
    if preferred_step in keys:
        step = preferred_step
    elif requested_step in keys:
        step = requested_step
    elif not records_complete or records_attention:
        step = "records"
    elif related_attention or not (related_complete or related_optional):
        step = "related"
    else:
        step = "matching"
    evidence = {
        "records": (records_complete, records_attention, False, True),
        "related": (
            related_complete,
            related_attention,
            related_optional,
            records_complete and not records_attention,
        ),
        "matching": (
            matching_complete,
            matching_attention,
            False,
            records_complete and not records_attention,
        ),
    }
    steps: list[SchemaJourneyStep] = []
    for key, label in SCHEMA_JOURNEY_STEPS:
        complete, attention, optional, available = evidence[key]
        if attention:
            status, status_label = "attention", "Needs attention"
        elif complete:
            status, status_label = "complete", "Complete"
        elif not available:
            status, status_label = "locked", "Not yet available"
        elif optional:
            status, status_label = "optional", "Optional"
        else:
            status, status_label = "available", "Available"
        steps.append(
            {
                "key": key,
                "label": label,
                "href": f"{schema_href}?step={key}",
                "status": status,
                "status_label": status_label,
            }
        )
    return {"enabled": True, "step": step, "steps": tuple(steps)}
