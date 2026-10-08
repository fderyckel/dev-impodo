"""Derive the source-stage gate from current workspace evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from impodo.domain.workspace.workbench import SourceMode, WorkspaceState


class SourceStageIssue(StrEnum):
    """Name the earliest source decision that still needs attention."""

    SCHEMA_MISSING = "SCHEMA_MISSING"
    SCHEMA_ATTENTION = "SCHEMA_ATTENTION"
    CAPTURE_PLANS_INCOMPLETE = "CAPTURE_PLANS_INCOMPLETE"
    FILE_SOURCE_NOT_FROZEN = "FILE_SOURCE_NOT_FROZEN"
    SOURCE_NOT_FROZEN = "SOURCE_NOT_FROZEN"


class SourceReadinessFacts(Protocol):
    """Expose only the bounded facts used by the source-stage gate."""

    source_selection_hash: str
    schema_present: bool
    schema_attention: bool
    schema_models: tuple[str, ...]
    capture_models: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceStageReadiness:
    """Describe whether downstream work may use the current source version."""

    ready: bool
    issue: SourceStageIssue | None = None
    missing_capture_models: tuple[str, ...] = ()


def assess_source_stage_readiness(
    workspace_state: WorkspaceState,
    facts: SourceReadinessFacts,
) -> SourceStageReadiness:
    """Assess the current source boundary without trusting a mutable flag.

    File sources become current when their immutable selection exists. Odoo
    sources additionally require a current schema and one capture plan for
    every selected model before the complete frozen selection can unlock later
    stages.
    """

    if workspace_state.source_mode is SourceMode.ODOO:
        if not facts.schema_present:
            return SourceStageReadiness(
                ready=False,
                issue=SourceStageIssue.SCHEMA_MISSING,
            )
        if facts.schema_attention:
            return SourceStageReadiness(
                ready=False,
                issue=SourceStageIssue.SCHEMA_ATTENTION,
            )
        missing_capture_models = tuple(
            sorted(set(facts.schema_models) - set(facts.capture_models))
        )
        unexpected_capture_models = set(facts.capture_models) - set(
            facts.schema_models
        )
        if missing_capture_models or unexpected_capture_models:
            return SourceStageReadiness(
                ready=False,
                issue=SourceStageIssue.CAPTURE_PLANS_INCOMPLETE,
                missing_capture_models=missing_capture_models,
            )
    if not facts.source_selection_hash:
        return SourceStageReadiness(
            ready=False,
            issue=(
                SourceStageIssue.SOURCE_NOT_FROZEN
                if workspace_state.source_mode is SourceMode.ODOO
                else SourceStageIssue.FILE_SOURCE_NOT_FROZEN
            ),
        )
    return SourceStageReadiness(ready=True)
