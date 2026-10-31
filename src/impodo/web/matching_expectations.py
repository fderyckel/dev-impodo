"""Project and parse explicit business expectations without source-row reads.

See docs/developer/workflow/03-match-data.md.
"""

from __future__ import annotations

from uuid import uuid4

from starlette.datastructures import FormData

from impodo.application.business_intent_service import (
    BusinessIntentInputs,
    checked_business_intent_inputs,
)
from impodo.domain.odoo.compatibility import OdooOperation
from impodo.domain.shared.access import Capability
from impodo.domain.workspace.business_intent import (
    MAX_INTENT_MODELS,
    BusinessIntent,
    BusinessIntentError,
    BusinessModelIntent,
    BusinessRelationshipOperation,
    BusinessRelationshipRule,
    BusinessWriteMode,
    ExpectedCount,
)

from .forms import _text, _texts

_MODEL_FIELDS = (
    "model",
    "mode",
    "create_min",
    "create_max",
    "update_min",
    "update_max",
    "limit_create_fields",
    "limit_update_fields",
    "limit_create_relations",
    "limit_update_relations",
    "create_fields",
    "update_fields",
    "create_relations",
    "update_relations",
)
_BASE_FIELDS = {
    "csrf_token",
    "expected_version",
    "binding_hash",
    "operation_id",
    "model_count",
}


def expectation_form_fields(count: int = MAX_INTENT_MODELS) -> set[str]:
    """Allow only the closed bounded editor fields, including its CSRF token."""
    return _BASE_FIELDS | {
        f"{field}_{index}" for index in range(count) for field in _MODEL_FIELDS
    }


def _count(form: FormData, name: str) -> int | None:
    value = _text(form, name)
    if not value:
        return None
    if not value.isascii() or not value.isdecimal() or len(value) > 19:
        raise BusinessIntentError("Expected counts must be nonnegative whole numbers")
    return int(value)


def parse_expectations(form: FormData, packet: BusinessIntentInputs) -> BusinessIntent:
    """Keep blank limits distinct from zero and reject stale or widened model scope."""
    if set(form) - expectation_form_fields(len(packet.scope.models)) or _text(
        form, "model_count"
    ) != str(len(packet.scope.models)):
        raise BusinessIntentError(
            "The record types changed; reload the expectations editor"
        )
    models = []
    for index, scope in enumerate(packet.scope.models):
        if _text(form, f"model_{index}") != scope.model:
            raise BusinessIntentError(
                "The record types changed; reload the expectations editor"
            )
        restrictions = {}
        for phase in ("create", "update"):
            relationships: dict[str, list[str]] = {}
            for value in _texts(form, f"{phase}_relations_{index}"):
                name, separator, operation = value.partition(":")
                if not separator:
                    raise BusinessIntentError("Choose a listed relationship operation")
                relationships.setdefault(name, []).append(operation)
            restrictions[f"{phase}_fields"] = (
                _texts(form, f"{phase}_fields_{index}")
                if _text(form, f"limit_{phase}_fields_{index}") == "1"
                else None
            )
            restrictions[f"{phase}_relationships"] = (
                tuple(
                    BusinessRelationshipRule(
                        name,
                        tuple(
                            BusinessRelationshipOperation(value) for value in operations
                        ),
                    )
                    for name, operations in relationships.items()
                )
                if _text(form, f"limit_{phase}_relations_{index}") == "1"
                else None
            )
        models.append(
            BusinessModelIntent(
                scope.model,
                BusinessWriteMode(_text(form, f"mode_{index}")),
                **restrictions,
                creates=ExpectedCount(
                    _count(form, f"create_min_{index}"),
                    _count(form, f"create_max_{index}"),
                ),
                updates=ExpectedCount(
                    _count(form, f"update_min_{index}"),
                    _count(form, f"update_max_{index}"),
                ),
            )
        )
    intent = BusinessIntent(tuple(models))
    packet.scope.require_narrowing(intent)
    return intent


def matching_expectations_view(
    context,
    *,
    actor,
    workspace,
    selection,
    schema,
    governance,
    revision,
    validation,
    dirty,
):
    """Reuse the mapping page's metadata and read the last expectation revision once."""
    saved = context.business_intents.get(workspace.workspace_id, actor=actor)
    result = {
        "packet": None,
        "saved": saved,
        "rows": (),
        "message": "Check the current matches to review business expectations.",
        "operation_id": str(uuid4()),
    }
    if dirty:
        return result
    try:
        access = context.workspace_access.resolve(
            workspace.workspace_id, actor=actor, capability=Capability.PROJECT_VIEW
        )
        if access.run_purpose not in {"AUTHORING", "TEST", "PRODUCTION"}:
            raise BusinessIntentError(
                "Verify the workspace's owning run purpose before reviewing business expectations"
            )
        packet = checked_business_intent_inputs(
            workspace=workspace,
            selection=selection,
            schema=schema,
            governance=governance,
            mapping=revision,
            validation=validation,
            operation=OdooOperation.PRODUCTION
            if access.run_purpose == "PRODUCTION"
            else OdooOperation.WRITE,
        )
    except BusinessIntentError as error:
        result["message"] = str(error)
        return result
    return expectations_editor_view(packet, saved)


def expectations_editor_view(packet, saved):
    """Share the editor with Final review using already-loaded model metadata."""
    result = {"packet": packet, "saved": saved, "operation_id": str(uuid4())}
    by_model = {item.model: item for item in saved.intent.models} if saved else {}
    labels = dict(packet.model_labels)
    rows = []
    for index, scope in enumerate(packet.scope.models):
        rule = by_model.get(scope.model)
        modes = [("none", "Reuse existing records only")]
        if scope.creates:
            modes.append(("create", "Create missing records only"))
        if scope.updates:
            modes.append(("update", "Update matching records only"))
        if scope.creates and scope.updates:
            modes.append(("upsert", "Create missing and update matching records"))
        rows.append(
            {
                "index": index,
                "scope": scope,
                "rule": rule,
                "label": labels.get(scope.model, scope.model),
                "modes": modes,
                "selected_relations": {
                    phase: {
                        f"{relation.field}:{operation.value}"
                        for relation in (getattr(rule, f"{phase}_relationships") or ())
                        for operation in relation.operations
                    }
                    if rule
                    else set()
                    for phase in ("create", "update")
                },
            }
        )
    result.update(packet=packet, rows=tuple(rows), message="")
    return result
