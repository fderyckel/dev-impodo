"""Capture authorized saved settings without reading source rows or Odoo.

The bundle retains current choices and the mapping revisions explicitly used
by saved preparation/comparison evidence. It never calls the validating or
refreshing workflow services. See ``docs/developer/workflow/03-match-data.md``
and ``tests/integration/web/test_diagnostic_settings.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import UTC, date, datetime
from enum import Enum
from typing import TYPE_CHECKING

from impodo.application.workspace.access import bind_workspace_access_context
from impodo.domain.shared.access import Actor, AuthorizationError, Capability
from impodo.domain.workspace.workbench import SourceMode

from .diagnostic_settings_schema import REDACTED_SETTINGS, SETTINGS_SCHEMAS

if TYPE_CHECKING:
    from .context import WebContext

MAX_SETTINGS_ITEMS = 2_000
MAX_SETTINGS_NODES = 60_000
MAX_SETTINGS_BYTES = 4 * 1024 * 1024
STAGES = (
    "Source data",
    "Odoo data",
    "Match data",
    "Prepare data",
    "Final review",
    "Load into Odoo",
)


@dataclass(frozen=True, slots=True)
class SettingsStage:
    """Hold one stage's saved metadata or an explicit capture failure."""

    settings: Mapping[str, object]
    status: str = "captured"
    failure_type: str = ""


@dataclass(frozen=True, slots=True)
class DiagnosticSettingsSnapshot:
    """Bind six saved configuration sections to one authorized workspace."""

    workspace_id: str
    project_id: str
    data_version_id: str
    migration_run_id: str
    captured_at: str
    stages: tuple[SettingsStage, ...]
    recipe_application_id: str | None = None
    run_purpose: str | None = None

    def bundle_members(self) -> tuple[dict[str, bytes], dict[str, object]]:
        """Apply the metadata allowlist at export and report every omission."""

        projection = SettingsProjection()
        stages: list[dict[str, object]] = []
        for number, (label, stage) in enumerate(
            zip(STAGES, self.stages, strict=True), 1
        ):
            stages.append(
                {
                    "stage": number,
                    "label": label,
                    "status": stage.status,
                    "failure_type": stage.failure_type,
                    "settings": {
                        key: projection.project(value)
                        for key, value in stage.settings.items()
                    },
                }
            )
        coverage: dict[str, object] = {
            "status": "partial"
            if projection.omissions or any(s.status != "captured" for s in self.stages)
            else "captured",
            "stage_count": len(stages),
            "omissions": dict(projection.omissions),
            "unsaved_browser_changes": "unavailable",
            "historical_settings": "Only explicitly bound saved mapping revisions are included; current settings do not prove earlier attempt settings.",
        }
        payload = {
            "settings_schema_version": 1,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "data_version_id": self.data_version_id,
            "migration_run_id": self.migration_run_id,
            "recipe_application_id": self.recipe_application_id,
            "run_purpose": self.run_purpose,
            "captured_at": self.captured_at,
            "observation": "Saved settings at download time, not a new workflow check.",
            "privacy": "Schema and setting names are included. Source rows, business literals, formula contents, credentials, account names, paths and connection URLs are omitted.",
            "coverage": coverage,
            "stages": stages,
        }
        encoded = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, indent=2
        ).encode()
        if len(encoded) > MAX_SETTINGS_BYTES:
            coverage.update(status="unavailable", reason="SETTINGS_SIZE_LIMIT")
            payload["stages"] = []
            encoded = json.dumps(
                payload, ensure_ascii=True, sort_keys=True, indent=2
            ).encode()
        lines = [
            "# Saved workspace settings",
            "",
            f"Workspace: `{self.workspace_id}`.",
            "",
            "These are saved choices at download time. Unsaved browser edits and unbound historical choices are unavailable.",
            "",
            "See `settings.json` for source column names, source identifiers, Odoo matching fields and scopes, field mappings, blank policies and stage evidence bindings.",
            "",
        ]
        lines.extend(
            f"- Stage {number}: {label}. Capture: {stage.status}."
            for number, (label, stage) in enumerate(
                zip(STAGES, self.stages, strict=True), 1
            )
        )
        lines.extend(
            [
                "",
                "Business literals and formula contents are explicitly redacted. Missing, unsupported or bounded settings are reported in coverage; they are not defaults.",
                "",
            ]
        )
        return {
            "settings.json": encoded + b"\n",
            "settings-summary.md": "\n".join(lines).encode(),
        }, coverage


class SettingsProjection:
    """Walk only reviewed dataclass fields, with global size and depth bounds."""

    def __init__(self) -> None:
        self.nodes = 0
        self.omissions: dict[str, int] = {}

    def _omit(self, reason: str) -> dict[str, str]:
        self.omissions[reason] = self.omissions.get(reason, 0) + 1
        return {"status": "unavailable", "reason": reason}

    def project(self, value: object, depth: int = 0) -> object:
        """Serialize metadata without traversing unapproved fields or mappings."""

        self.nodes += 1
        if self.nodes > MAX_SETTINGS_NODES or depth > 16:
            return self._omit("SETTINGS_STRUCTURE_LIMIT")
        if value is None:
            return {"status": "not_saved"} if depth == 0 else None
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            if len(value) > 500:
                return self._omit("SETTING_TEXT_LIMIT")
            return value
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, (tuple, list)):
            if len(value) > MAX_SETTINGS_ITEMS:
                return self._omit("SETTING_ITEM_LIMIT")
            return [self.project(item, depth + 1) for item in value]
        schema = SETTINGS_SCHEMAS.get(
            f"{type(value).__module__}.{type(value).__qualname__}"
        )
        if schema is None or not is_dataclass(value) or isinstance(value, type):
            return self._omit("SETTING_TYPE_NOT_ALLOWLISTED")
        result: dict[str, object] = {}
        for field in fields(value):
            if field.name not in schema:
                continue
            item = getattr(value, field.name)
            if field.name in REDACTED_SETTINGS:
                redacted: dict[str, object] = {
                    "status": "redacted",
                    "present": item is not None and item != "",
                }
                if isinstance(item, (tuple, list, dict)):
                    redacted["entry_count"] = len(item)
                if field.name == "comparison_value" and item in (None, "", "0"):
                    redacted["structural_value"] = (
                        "empty" if item in (None, "") else "zero"
                    )
                result[field.name] = redacted
            else:
                result[field.name] = self.project(item, depth + 1)
        return result


def capture_diagnostic_settings(
    context: WebContext,
    workspace_id: str,
    actor: Actor,
) -> DiagnosticSettingsSnapshot:
    """Read saved stage choices after Project authorization in one local scope."""

    access = context.workspace_access.resolve(
        workspace_id, actor=actor, capability=Capability.PROJECT_VIEW
    )
    with bind_workspace_access_context(access):
        return replace(_capture(
            context,
            access.project_id,
            access.data_version_id,
            access.migration_run_id,
            workspace_id,
        ), recipe_application_id=access.recipe_application_id, run_purpose=access.run_purpose)


def _capture(
    context: WebContext,
    project_id: str,
    data_version_id: str,
    migration_run_id: str,
    workspace_id: str,
) -> DiagnosticSettingsSnapshot:
    """Keep optional stage failures independent without hiding access refusals."""

    queries = context.queries

    def stage(read: Callable[[], Mapping[str, object]]) -> SettingsStage:
        try:
            return SettingsStage(read())
        except AuthorizationError:
            raise
        except Exception as error:  # noqa: BLE001 - optional diagnostics must survive unavailable stage evidence
            return SettingsStage(
                {}, status="unavailable", failure_type=type(error).__name__
            )

    def source_settings() -> Mapping[str, object]:
        state = queries.get(workspace_id)
        return {
            "workspace": state,
            "files": state.source_files,
            "source_choices": queries.get_source_configurations(workspace_id),
            "source_selection": queries.get_source_selection(workspace_id),
            "preparation_rules": queries.get_derived_entity_plan(workspace_id),
            "odoo_capture_choices": queries.get_current_odoo_capture_selections(
                workspace_id
            )
            if state.source_mode is SourceMode.ODOO
            else (),
            "odoo_relationship_scope": queries.get_current_odoo_relationship_scope(
                workspace_id
            )
            if state.source_mode is SourceMode.ODOO
            else None,
        }

    source = stage(source_settings)
    odoo = stage(
        lambda: {
            "schema": queries.get_odoo_schema_catalog(workspace_id),
            "matching_rules": queries.get_schema_governance(workspace_id),
            "supporting_data_choices": context.schema_workspace.schemas.get_supporting_review(
                workspace_id
            ),
        }
    )
    def matching_settings() -> Mapping[str, object]:
        draft = queries.get_mapping_working_draft(workspace_id)
        revision = queries.get_mapping_revision(workspace_id)
        return {
            "source_columns": queries.get_mapping_source_selection(workspace_id),
            "working_draft": draft,
            "working_draft_hash": draft.definition.content_hash if draft else None,
            "confirmed_mapping": revision,
            "confirmed_mapping_hash": revision.definition.content_hash if revision else None,
            "submission": queries.get_mapping_submission(workspace_id),
        }

    matching = stage(matching_settings)

    def preparation() -> Mapping[str, object]:
        summary = context.preflight.staging.get_current_staging_summary(workspace_id)
        return {
            "prepared_summary": summary,
            "mapping_used": queries.get_mapping_revision(
                workspace_id, summary.mapping_version
            )
            if summary
            else None,
            "quality_rules": queries.get_current_quality_ruleset(workspace_id),
            "normalization_review": context.normalization.current_summary(workspace_id),
        }

    def review() -> Mapping[str, object]:
        report = context.preflight.preflight.get_current_readiness_report(workspace_id)
        return {
            "comparison": report,
            "mapping_used": queries.get_mapping_revision(
                workspace_id, report.mapping_version
            )
            if report
            else None,
            "configured_read_workers_at_capture": context.preflight_read_workers,
        }

    def load() -> Mapping[str, object]:
        facts = context.navigation.repository.get(workspace_id)
        execution = facts.execution_state
        run_id = (
            execution.execution_run_id if execution else facts.transfer_execution_run_id
        )
        return {
            "saved_stage_status": facts,
            "load_control": context.execution.load_controls.get_load_control(
                workspace_id, run_id
            )
            if run_id and context.execution.load_controls
            else None,
            "attempt_configuration_location": "load-attempts.json; use attempt/build bindings before associating settings with an earlier load.",
        }

    return DiagnosticSettingsSnapshot(
        workspace_id,
        project_id,
        data_version_id,
        migration_run_id,
        datetime.now(UTC).isoformat(),
        (source, odoo, matching, stage(preparation), stage(review), stage(load)),
    )
