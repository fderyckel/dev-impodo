"""Restore retained Odoo data through existing checked and journalled loading.

This boundary selects fresh destination access and company context. Source
data remains offline; portable field choices enter the normal transfer engine.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

from impodo.application.destination_matching_service import (
    DestinationMatchingService,
    DestinationMatchKeyChoice,
    destination_match_scope_candidates,
)
from impodo.application.odoo_saved_data_jobs import SavedOdooActionError
from impodo.application.schema_workspace_service import _schema_field_from_metadata
from impodo.application.transfer_order_service import TransferOrderService
from impodo.application.transfer_preflight_service import TransferPreflightService
from impodo.application.transfer_review_service import TransferReviewService
from impodo.application.workspace.execution.service import execution_api_scope
from impodo.domain.execution.models import ExecutionRunStatus
from impodo.domain.odoo_database_binding import (
    OdooDatabaseBinding,
    OdooDatabaseBindingRole,
    database_scope_hash,
)
from impodo.domain.odoo_relationship_scope import OdooRelationshipCaptureAction
from impodo.domain.odoo_restore import (
    OdooRestorePlan,
    RestoreCreateDefault,
    RestoreFieldRule,
    compile_odoo_restore,
)
from impodo.domain.odoo_saved_data import (
    SavedOdooDataDefinition,
    SavedOdooRestoreProfile,
)
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import Actor, Capability
from impodo.domain.shared.tenancy import AuthorizationReceipt, RequestOrganizationScope
from impodo.domain.source_binding import OdooSourceBinding
from impodo.domain.workspace.business_keys import recommend_business_key
from impodo.domain.workspace.contracts import SchemaModel
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.transfer_identity_aliases import (
    ProtectedIdentityAliasRulesReference,
)
from impodo.domain.workspace.transfer_review import TransferReviewApproval
from impodo.domain.workspace.workbench import (
    WorkspaceState,
    transfer_destination_workspace,
)
from impodo.web.context import WebContext
from impodo.web.target_credentials import (
    TargetCredential,
    TargetCredentialRole,
    get_target_credential,
    store_target_credential,
)

from .database_bindings import frozen_database_guard, review_database_binding
from .page_reads import run_local_operation_sync
from .target_readers import _company_choice_connector


@dataclass(frozen=True, slots=True)
class SavedRestoreResult:
    """Return safe review or verified-result destinations to the browser."""

    workspace_id: str
    profile_id: str
    redirect_url: str
    verified: bool
    issues: tuple[str, ...] = ()
    field_rows: tuple[dict[str, object], ...] = ()
    default_rows: tuple[dict[str, object], ...] = ()


def saved_identity_alias_rule_owner(
    profile: SavedOdooRestoreProfile, definition: SavedOdooDataDefinition
) -> str:
    """Bind reusable rules to source meaning and reviewed destination choices."""

    if profile.project_id != definition.project_id or profile.saved_data_id != definition.saved_data_id:
        raise WorkspaceError("The reference mappings belong to another saved data set")
    settings = json.loads(profile.settings_json)
    return content_hash({
        "project_id": profile.project_id,
        "saved_data_id": profile.saved_data_id,
        "profile_id": profile.profile_id,
        "source_target_hash": definition.source_target_hash,
        "source_company_hash": definition.source_company_hash,
        "source_major": definition.odoo_major,
        "destination": (profile.connection_mode, profile.base_url, profile.database, profile.company_name),
        "keys": {model: tuple(fields) for model, fields in sorted(settings.get("keys", {}).items())},
        "scopes": {model: tuple(fields) for model, fields in sorted(settings.get("scopes", {}).items())},
    })


def saved_identity_alias_rules(context: WebContext, profile, definition, *, actor: Actor, manage: bool = False):
    """Authorize and decrypt reusable rules without carrying old approvals."""

    if profile.project_id != definition.project_id or profile.saved_data_id != definition.saved_data_id:
        raise WorkspaceError("The reference mappings belong to another saved data set")
    settings = json.loads(profile.settings_json)
    raw_reference = settings.get("identity_alias_rules")
    if raw_reference is None:
        return ()
    access = context.workspace_access.resolve(
        definition.source_workspace_id, actor=actor, capability=Capability.PROTECTED_EVIDENCE_READ,
    )
    if access.project_id != profile.project_id:
        raise WorkspaceError("The saved reference mappings belong to another project")
    if manage:
        context.workspace_access.resolve(
            definition.source_workspace_id, actor=actor, capability=Capability.PROTECTED_EVIDENCE_MANAGE,
        )
    reference = ProtectedIdentityAliasRulesReference.from_dict(raw_reference)
    return context.destination_identity_aliases.read_rules(
        access.project_id, reference, owner_key=saved_identity_alias_rule_owner(profile, definition),
    )


def _protected_values(
    context: WebContext, state: WorkspaceState, *, actor: Actor
) -> dict[str, object]:
    """Open approved create values only for writing and independent read-back."""

    plan = state.destination_match_plan
    if plan is None or plan.create_field_evidence_id is None:
        return {}
    owner = context.workspace_access.require(
        actor, Capability.PROTECTED_EVIDENCE_READ, workspace_id=state.workspace_id
    )
    evidence = context.destination_create_fields.read(owner.project_id, plan)
    if evidence is None:
        raise WorkspaceError(
            "The protected creation values are unavailable; check the restore again"
        )
    values: dict[str, object] = {}
    for item in evidence.values:
        if item.provider_kind not in {
            "odoo_default",
            "fixed_value",
            "existing_reference",
        }:
            continue
        if item.value is None or values.get(item.value_hash, item.value) != item.value:
            raise WorkspaceError(
                "The protected creation values are incomplete or inconsistent"
            )
        values[item.value_hash] = item.value
    return values


def _configure_destination(
    context: WebContext,
    state: WorkspaceState,
    profile: SavedOdooRestoreProfile,
    api_key: str,
    *,
    actor: Actor,
    scope: RequestOrganizationScope,
    authorization_receipt: AuthorizationReceipt,
) -> tuple[WorkspaceState, TargetCredential]:
    """Finish local target admission before any remote request."""

    current = context.queries.get(state.workspace_id)
    review_database_binding(
        context, current, OdooDatabaseBindingRole.DESTINATION,
        base_url=profile.base_url, database=profile.database,
        actor=actor, scope=scope, authorization_receipt=authorization_receipt,
    )
    current = context.workspace_states.configure_transfer_destination(
        current.workspace_id,
        actor=actor,
        expected_revision=current.revision,
        odoo_connection_mode=profile.connection_mode,
        odoo_base_url=profile.base_url,
        odoo_database=profile.database,
    )
    key = api_key
    if not key and profile.credential_workspace_id is not None:
        owner = context.workspace_access.require(
            actor,
            Capability.PROTECTED_EVIDENCE_READ,
            workspace_id=profile.credential_workspace_id,
        )
        if owner.project_id != profile.project_id:
            raise WorkspaceError("Saved destination access belongs to another project")
        previous = context.queries.get(profile.credential_workspace_id)
        if (
            previous.destination_odoo_connection_mode
            != current.destination_odoo_connection_mode
            or previous.destination_odoo_base_url != profile.base_url
            or previous.destination_odoo_database != profile.database
        ):
            raise WorkspaceError(
                "Enter a checking and loading key for this destination"
            )
        stored = get_target_credential(
            context.secret_store, previous, TargetCredentialRole.DESTINATION_TRANSFER
        )
        if stored is not None:
            key = stored.secret
    if not key:
        raise WorkspaceError("Enter the destination checking and loading key")
    credential = store_target_credential(
        context.secret_store,
        current,
        TargetCredentialRole.DESTINATION_TRANSFER,
        key,
        persistent=False,
    )
    return context.queries.get(current.workspace_id), credential


def _pending_restore(
    context: WebContext, profile: SavedOdooRestoreProfile, *, actor: Actor,
    current_binding: OdooDatabaseBinding,
) -> str | None:
    """Keep a changed operation or profile from bypassing an unresolved journal."""

    service = context.saved_odoo_data
    if service is None:
        return None
    service.get(profile.project_id, profile.saved_data_id, actor=actor)
    context.authorization.require(
        actor, Capability.EXPORT_PLAN_EXECUTE, project_id=profile.project_id
    )
    offset = 0
    while True:
        bindings = service.repository.list_project_restore_bindings(
            profile.project_id, limit=100, offset=offset
        )
        for binding in bindings:
            previous = context.queries.get(binding.workspace_id)
            if (
                not previous.destination_configured
                or database_scope_hash(previous.destination_odoo_base_url, previous.destination_odoo_database)
                != current_binding.scope_hash
            ):
                continue
            run = context.execution.current_transfer_run(binding.workspace_id)
            if run is None:
                continue
            previous_binding = previous.destination_database_binding
            generation = context.odoo_database_bindings.repository.current(
                current_binding.organization_id, current_binding.scope_hash,
            )
            if generation.generation > 0 and (
                (previous_binding is not None and previous_binding.generation_hash != current_binding.generation_hash)
                or (previous_binding is None and generation.declared_at is not None and run.started_at < generation.declared_at)
            ):
                # These receipts belong to a declared predecessor. Keep them as
                # history and never verify them against replacement records.
                continue
            verification = context.reconciliation.current(binding.workspace_id)
            if (
                run.status is not ExecutionRunStatus.COMPLETED
                or verification is None
                or verification.unknown_count
                or verification.fallout_count
            ):
                return f"/workspaces/{binding.workspace_id}/transfer-load/outcome"
        if len(bindings) < 100:
            return None
        offset += len(bindings)


def restore_saved_odoo_data(
    context: WebContext,
    project_id: str,
    saved_data_id: str,
    version_number: int,
    *,
    actor: Actor,
    scope: RequestOrganizationScope,
    authorization_receipt: AuthorizationReceipt,
    operation_id: str,
    profile: SavedOdooRestoreProfile,
    api_key: str,
    progress: Callable[[int, str], None],
    check_only: bool = False,
) -> SavedRestoreResult:
    """Check the complete scope and execute only an explicitly confirmed restore."""

    service = context.saved_odoo_data
    if service is None:
        raise WorkspaceError("Saved Odoo data is unavailable in this deployment")
    if profile.project_id != project_id or profile.saved_data_id != saved_data_id:
        raise WorkspaceError("The restore profile belongs to another saved data set")
    alias_rules = ()
    if json.loads(profile.settings_json).get("identity_alias_rules") is not None:
        def historical_definition():
            service.get(project_id, saved_data_id, actor=actor)
            version = service.repository.get_version(project_id, saved_data_id, version_number)
            return service.repository.get_configuration(
                project_id, saved_data_id, version.configuration_revision
            )

        definition = run_local_operation_sync(historical_definition)
        alias_rules = run_local_operation_sync(
            lambda: saved_identity_alias_rules(context, profile, definition, actor=actor, manage=True)
        )
    alias_rules_by_model = {item.model: item for item in alias_rules}
    state = run_local_operation_sync(
        lambda: service.prepare_restore(
            project_id,
            saved_data_id,
            version_number,
            actor=actor,
            operation_id=operation_id,
        )
    )
    existing_run = run_local_operation_sync(
        lambda: context.execution.current_transfer_run(state.workspace_id)
    )
    if existing_run is not None:
        verification = run_local_operation_sync(
            lambda: context.reconciliation.current(state.workspace_id)
        )
        if (
            verification is None
            or verification.unknown_count
            or verification.fallout_count
        ):
            raise SavedOdooActionError(
                "Review this saved restore before continuing it.",
                redirect_url=f"/workspaces/{state.workspace_id}/transfer-load/outcome",
            )
        return SavedRestoreResult(
            state.workspace_id,
            profile.profile_id,
            f"/workspaces/{state.workspace_id}/transfer-load/outcome",
            verification is not None
            and not (verification.unknown_count or verification.fallout_count),
        )
    state, credential = run_local_operation_sync(
        lambda: _configure_destination(context, state, profile, api_key, actor=actor,
            scope=scope, authorization_receipt=authorization_receipt)
    )
    if not check_only:
        def unresolved():
            current_binding = context.odoo_database_bindings.require_current(
                state.workspace_id, OdooDatabaseBindingRole.DESTINATION,
                base_url=profile.base_url, database=profile.database,
                actor=actor, capability=Capability.EXPORT_PLAN_EXECUTE,
            )
            return _pending_restore(context, profile, actor=actor, current_binding=current_binding)
        pending = run_local_operation_sync(unresolved)
        if pending:
            raise SavedOdooActionError(
                "An earlier restore to this database needs verification. Review that run before starting another restore.",
                redirect_url=pending,
            )
    progress(5, "Checking the destination database and company")
    destination = transfer_destination_workspace(state)
    destination_guard = frozen_database_guard(
        context, destination, actor=actor, role=OdooDatabaseBindingRole.DESTINATION,
        capability=Capability.EXPORT_PLAN_EXECUTE,
    )
    destination_guard()
    companies = (
        _company_choice_connector(destination, credential.secret)
        .get_company_access()
        .choices
    )
    destination_guard()
    matches = tuple(
        company for company in companies if company.name == profile.company_name
    )
    if len(matches) != 1:
        raise WorkspaceError(
            "The destination company name must identify exactly one accessible company"
        )
    state = run_local_operation_sync(
        lambda: context.workspace_states.select_odoo_company(
            state.workspace_id,
            selection=matches[0],
            actor=actor,
            expected_revision=state.revision,
        )
    )
    destination = transfer_destination_workspace(state)
    models = tuple(sorted(state.intended_models))
    identity = context.read_identity_probe(destination, credential.secret, models)
    metadata = context.schema_reader(destination, credential.secret)
    state = run_local_operation_sync(
        lambda: context.workspace_states.verify_transfer_destination(
            state.workspace_id,
            actor=actor,
            expected_revision=state.revision,
            target_hash=metadata.fingerprint.target_hash,
            credential_binding_hash=credential.binding_hash,
            read_principal_hash=identity.principal_hash,
            odoo_version=metadata.fingerprint.odoo_version,
        )
    )
    selection = run_local_operation_sync(
        lambda: context.queries.get_source_selection(state.workspace_id)
    )
    schema = run_local_operation_sync(
        lambda: context.queries.get_odoo_schema_catalog(state.workspace_id)
    )
    relationship_scope = run_local_operation_sync(
        lambda: context.queries.get_current_odoo_relationship_scope(state.workspace_id)
    )
    if selection is None or schema is None:
        raise WorkspaceError("The complete saved data version is unavailable")
    settings = json.loads(profile.settings_json)
    plans: dict[str, OdooRestorePlan] = {}
    choices: list[DestinationMatchKeyChoice] = []
    source_by_model = {model.name: model for model in schema.models}
    scope_candidates = destination_match_scope_candidates(selection, schema, relationship_scope)
    errors: list[str] = []
    field_rows: list[dict[str, object]] = []
    default_rows: list[dict[str, object]] = []
    if set(alias_rules_by_model) - set(source_by_model):
        errors.append("Review reference mappings for record types outside this saved version")
    for dataset in selection.datasets:
        if not isinstance(dataset.source, OdooSourceBinding):
            raise WorkspaceError("Choose an Odoo saved data version")
        source = source_by_model[dataset.source.model]
        columns = {column.source_name: column.stable_key for column in dataset.columns}
        recommendation = recommend_business_key(source)
        supplied_keys = settings.get("keys", {}).get(source.name)
        keys = (
            tuple(supplied_keys)
            if supplied_keys
            else recommendation.key_fields
            if recommendation
            else ()
        )
        matching_scopes = tuple(settings.get("scopes", {}).get(source.name, ()))
        aliases = alias_rules_by_model.get(source.name)
        if aliases is not None and (aliases.key_fields != keys or matching_scopes):
            errors.append(f"Review the reference mappings and matching fields for {source.label}")
            continue
        if matching_scopes and (
            len(matching_scopes) != 1
            or matching_scopes[0] not in {
                name for name, _label in scope_candidates.get(dataset.dataset_id, ())
            }
        ):
            errors.append(f"Review the parent or company used to match {source.label}")
            continue
        if not keys or any(key not in columns for key in keys):
            errors.append(f"Choose portable matching fields for {source.label}")
            continue
        target_metadata = metadata.models.get(source.name)
        target = (
            None
            if target_metadata is None
            else SchemaModel(
                name=source.name,
                label=source.label,
                fields=tuple(
                    _schema_field_from_metadata(
                        name, field, metadata.create_defaults.get(source.name)
                    )
                    for name, field in sorted(target_metadata.fields.items())
                ),
                unique_constraints=target_metadata.unique_constraints,
            )
        )
        rules = tuple(
            RestoreFieldRule.from_dict(rule)
            for rule in settings.get("rules", {}).get(source.name, [])
        )
        explicit_defaults = settings.get("defaults", {}).get(source.name, {})
        mapped_names = {
            rule.target_field or rule.source_field
            for rule in rules
            if not rule.excluded
        }
        mapped_names.update(
            name
            for name in columns
            if not any(rule.source_field == name for rule in rules)
        )
        defaults = (
            tuple(
                RestoreCreateDefault(
                    field.name, value=explicit_defaults[field.name]["value"]
                )
                if field.name in explicit_defaults
                and explicit_defaults[field.name].get("provider") == "fixed_value"
                else RestoreCreateDefault(field.name, use_odoo_default=True)
                for field in target.fields
                if (
                    field.required
                    and field.create_default_present
                    and field.name not in mapped_names
                    and field.type not in {"many2one", "many2many", "one2many"}
                )
                or field.name in explicit_defaults
            )
            if target is not None
            else ()
        )
        relation_names = tuple(
            field.name
            for field in source.fields
            if field.type in {"many2one", "many2many"}
            and relationship_scope is not None
            and any(
                decision.source_model == source.name
                and decision.field_name == field.name
                and decision.relation_model in source_by_model
                and decision.action
                in {
                    OdooRelationshipCaptureAction.CAPTURE_LINKED,
                    OdooRelationshipCaptureAction.PRESERVE_LINKED,
                    OdooRelationshipCaptureAction.MATCH_EXISTING,
                }
                for decision in relationship_scope.decisions
            )
        )
        relation_names = tuple(dict.fromkeys((*relation_names, *matching_scopes)))
        handling = (
            "reference_only"
            if relationship_scope is not None and source.name in relationship_scope.reference_models
            else "transfer"
        )
        if handling == "reference_only":
            choices.append(
                DestinationMatchKeyChoice(
                    dataset.dataset_id,
                    columns[keys[0]],
                    tuple(columns[key] for key in keys[1:]),
                    destination_handling=handling,
                    scope_fields=matching_scopes,
                    identity_aliases=aliases.aliases if aliases is not None else (),
                )
            )
            continue
        plan = compile_odoo_restore(
            source,
            target,
            source_version=schema.odoo_version,
            destination_version=metadata.fingerprint.odoo_version,
            captured_fields=tuple(dict.fromkeys((*columns, *relation_names))),
            identity_fields=(*keys, *matching_scopes),
            rules=rules,
            defaults=defaults,
            creates_expected=False,
        )
        errors.extend(issue.message for issue in plan.issues if issue.blocking)
        plans[source.name] = plan
        by_rule = {rule.source_field: rule for rule in rules}
        for name in (*columns, *relation_names):
            field = next((field for field in source.fields if field.name == name), None)
            if field is None:
                continue
            rule = by_rule.get(name, RestoreFieldRule(name))
            coverage = next(
                (item for item in plan.coverage if item.source_field == name), None
            )
            field_rows.append(
                {
                    "model": source.name,
                    "name": name,
                    "label": field.label,
                    "type": field.type,
                    "policy": rule.policy.value,
                    "link_policy": rule.link_policy.value,
                    "target_name": rule.target_field or name,
                    "excluded": rule.excluded,
                    "is_identity": name in (*keys, *matching_scopes),
                    "message": coverage.message if coverage else "",
                }
            )
        if target is not None:
            restored_names = {field.destination.name for field in plan.fields}
            for field in target.fields:
                if not field.required or field.name in restored_names or field.readonly:
                    continue
                saved_default = explicit_defaults.get(field.name, {})
                default_rows.append(
                    {
                        "model": source.name,
                        "name": field.name,
                        "label": field.label,
                        "type": field.type,
                        "choices": field.selection,
                        "value": saved_default.get("value", ""),
                        "provider": saved_default.get(
                            "provider",
                            "odoo_default"
                            if field.create_default_present
                            else "fixed_value",
                        ),
                        "has_odoo_default": field.create_default_present,
                        "message": "Choose a value for new records; existing records keep this field.",
                    }
                )
        choices.append(
            DestinationMatchKeyChoice(
                dataset.dataset_id,
                columns[keys[0]],
                tuple(columns[key] for key in keys[1:]),
                destination_handling=handling,
                scope_fields=matching_scopes,
                identity_aliases=aliases.aliases if aliases is not None else (),
            )
        )
    profiles = run_local_operation_sync(
        lambda: service.repository.list_restore_profiles(project_id, saved_data_id)
    )
    old_profile = next(
        (item for item in profiles if item.profile_id == profile.profile_id), None
    )
    profile = run_local_operation_sync(
        lambda: service.repository.save_restore_profile(
            replace(
                profile,
                credential_workspace_id=state.workspace_id,
                updated_at=datetime.now(UTC),
            ),
            expected_revision=old_profile.revision if old_profile else None,
        )
    )
    review_url = f"/projects/{project_id}/saved-odoo-data?selected={saved_data_id}&profile={profile.profile_id}&checked={state.workspace_id}"
    if errors:
        return SavedRestoreResult(
            state.workspace_id,
            profile.profile_id,
            review_url,
            False,
            tuple(errors),
            tuple(field_rows),
            tuple(default_rows),
        )
    origins = {}
    for dataset in selection.datasets:
        protected = run_local_operation_sync(
            lambda dataset=dataset: context.odoo_provenance.read_current_origins(
                state.workspace_id, actor=actor, dataset_id=dataset.dataset_id
            )
        )
        if protected is None:
            raise WorkspaceError("A saved relationship artifact is missing")
        origins[dataset.dataset_id] = protected[1]
    matching = DestinationMatchingService(context.categorical_coverage)
    records = []

    def compare(current):
        return matching.check(
            current,
            selection,
            schema,
            choices,
            api_key=credential.secret,
            credential_binding_hash=credential.binding_hash,
            read_identity=context.read_identity_probe(
                destination, credential.secret, models
            ),
            reader=context.destination_match_reader,
            record_snapshot_sink=records.append,
            recorded_by=actor.identity.display_name,
            source_origins=origins,
            relationship_scope=relationship_scope,
            restore_plans=plans,
        )

    progress(20, "Comparing all records and checking field policies")
    match = compare(state)

    def protect_match():
        context.workspace_access.require(
            actor, Capability.PROTECTED_EVIDENCE_MANAGE, workspace_id=state.workspace_id
        )
        checked = context.destination_create_fields.put(project_id, match)
        if checked.identity_alias_bindings:
            context.workspace_access.resolve(
                state.workspace_id, actor=actor, capability=Capability.PROTECTED_EVIDENCE_READ,
            )
            checked = context.destination_identity_aliases.put(project_id, checked)
        return checked

    match = run_local_operation_sync(protect_match)
    if not match.ready or not match.create_field_defaults_complete:
        run_local_operation_sync(
            lambda: context.workspace_states.save_destination_match_plan(
                state.workspace_id,
                actor=actor,
                expected_revision=state.revision,
                plan=match,
            )
        )
        messages = []
        for item in match.model_matches:
            if item.unresolved_create_fields and item.destination_create_key_count:
                messages.append(
                    f"{item.model_label}: choose values for new records in {', '.join(item.unresolved_create_fields)}"
                )
            if item.source_blank_row_count:
                messages.append(
                    f"{item.model_label}: {item.source_blank_row_count} saved rows have empty matching values"
                )
            if item.source_duplicate_key_count or item.destination_duplicate_key_count:
                messages.append(
                    f"{item.model_label}: matching values identify more than one record; choose a unique combination of fields"
                )
            if item.missing_fields or item.incompatible_fields:
                messages.append(f"{item.model_label}: review removed or changed fields")
        if not messages:
            messages.append(
                "Resolve the destination or linked-record comparison before restoring all data"
            )
        return SavedRestoreResult(
            state.workspace_id,
            profile.profile_id,
            review_url,
            False,
            tuple(messages),
            tuple(field_rows),
            tuple(default_rows),
        )

    def review():
        current = context.workspace_states.save_destination_match_plan(
            state.workspace_id,
            actor=actor,
            expected_revision=state.revision,
            plan=match,
        )
        order = TransferOrderService().build(
            current,
            match,
            recorded_by=actor.identity.display_name,
            relationship_scope=relationship_scope,
        )
        current = context.workspace_states.save_transfer_order_plan(
            current.workspace_id,
            actor=actor,
            expected_revision=current.revision,
            plan=order,
        )
        owner = context.workspace_access.require(
            actor, Capability.EXPORT_PLAN_APPROVE, workspace_id=current.workspace_id
        )
        package = TransferReviewService().build(
            current,
            match,
            order,
            run_id=owner.migration_run_id,
            data_version_id=owner.data_version_id,
            built_by=actor.identity,
            restore_plans=plans,
        )
        current = context.workspace_states.save_transfer_review_package(
            current.workspace_id,
            actor=actor,
            expected_revision=current.revision,
            package=package,
        )
        return current, package

    state, package = run_local_operation_sync(review)
    if check_only:
        return SavedRestoreResult(
            state.workspace_id,
            profile.profile_id,
            review_url,
            False,
            (),
            tuple(field_rows),
            tuple(default_rows),
        )
    approval = TransferReviewApproval.approve(
        package,
        approval_id=str(uuid4()),
        actor=actor,
        approved_at=datetime.now(UTC),
        reason="Confirmed Restore all with submitted destination and field policies",
    )
    state = run_local_operation_sync(
        lambda: context.workspace_states.approve_transfer_review(
            state.workspace_id,
            actor=actor,
            expected_revision=state.revision,
            approval=approval,
        )
    )
    records.clear()
    fresh_match = compare(state)
    if len(records) != 1:
        raise WorkspaceError("The final destination read is incomplete")
    report = TransferPreflightService().build(
        state,
        package,
        approval,
        match,
        fresh_match,
        recorded_by=actor.identity,
        restore_plans=plans,
    )
    state = run_local_operation_sync(
        lambda: context.workspace_states.save_transfer_preflight_report(
            state.workspace_id,
            actor=actor,
            expected_revision=state.revision,
            report=report,
        )
    )
    if not report.ready:
        raise WorkspaceError(
            "The destination changed; check the restore again before loading"
        )
    snapshot = context.transfer_execution.compile(
        state,
        selection,
        schema,
        package,
        report,
        fresh_match,
        records[0],
        actor=actor,
        restore_plans=plans,
    )
    if snapshot.write_count == 0:
        progress(100, "The destination already matches this saved data version")
        return SavedRestoreResult(
            state.workspace_id,
            profile.profile_id,
            review_url + "&unchanged=yes",
            True,
            (),
            tuple(field_rows),
            tuple(default_rows),
        )
    context.transfer_execution.stage(state, selection, schema, snapshot)
    progress(50, "Restoring records in dependency order")
    api_scope = execution_api_scope(snapshot)
    write_identity = context.write_identity_probe(
        destination, credential.secret, api_scope
    )
    outcome_url = f"/workspaces/{state.workspace_id}/transfer-load/outcome"
    try:
        run = context.transfer_execution.execute(
            state,
            snapshot,
            expected_preflight_hash=report.content_hash,
            executor=context.write_executor_factory(
                destination, credential.secret, api_scope
            ),
            actor=actor,
            batch_rows=100,
            read_identity=identity,
            credential_binding_hash=credential.binding_hash,
            write_identity=write_identity,
            protected_values=run_local_operation_sync(
                lambda: _protected_values(context, state, actor=actor)
            ),
        )
        progress(90, "Reading the destination back to verify the result")
        verification = context.reconciliation.reconcile(
            state.workspace_id,
            expected_execution_run_id=run.run_id,
            reader=context.readback_reader_factory(
                destination, credential.secret, api_scope
            ),
            actor=actor,
            write_identity=context.write_identity_probe(
                destination, credential.secret, api_scope
            ),
            write_credential_binding_hash=credential.binding_hash,
            protected_values=run_local_operation_sync(
                lambda: _protected_values(context, state, actor=actor)
            ),
        )
    except Exception as error:
        journal = run_local_operation_sync(
            lambda: context.execution.current_transfer_run(state.workspace_id)
        )
        if journal is None:
            raise
        raise SavedOdooActionError(
            "The restore stopped. Open its saved result to verify committed records before continuing this run.",
            redirect_url=outcome_url,
        ) from error
    if verification.unknown_count or verification.fallout_count:
        raise SavedOdooActionError(
            "Some restored records need verification. Review this run before starting another restore.",
            redirect_url=outcome_url,
        )
    return SavedRestoreResult(
        state.workspace_id,
        profile.profile_id,
        outcome_url,
        True,
    )
