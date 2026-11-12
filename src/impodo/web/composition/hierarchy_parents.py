"""Separate Stage 1 local capture/publication from existing-parent Odoo reads.

See docs/developer/workflow/01-source-data.md. No retained DuckDB owner crosses
the read worker. Search results are ephemeral and never become a catalogue.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from starlette.concurrency import run_in_threadpool

from impodo.adapters.odoo.local_stack import LocalStackProfile
from impodo.application.hierarchy_parent_lookup import (
    HierarchyParentChoice,
    browse_parent_choices,
    hierarchy_lookup_shape,
    parent_policy,
    read_parent_choices,
)
from impodo.application.shared.secrets import SecretStoreError
from impodo.domain.hierarchy import HierarchyParentPolicy
from impodo.domain.odoo.company import OdooCompanyAccess
from impodo.domain.odoo.contracts import MetadataRequest
from impodo.domain.shared.access import Actor, Capability
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import SourceMode, WorkspaceState
from impodo.web.context import WebContext
from impodo.web.target_credentials import (
    TargetCredential,
    TargetCredentialRole,
    get_target_credential,
)

from .odoo_company import read_company_access
from .page_reads import run_local_operation
from .target_readers import (
    _existing_catalog_model,
    _selected_local_profile,
    hierarchy_parent_connector,
)


@dataclass(frozen=True, slots=True)
class PreparedHierarchyAccess:
    """Capture immutable inputs and the role-qualified reader for one command."""

    state: WorkspaceState
    source_hash: str
    plan_hash: str | None
    model: str
    credential: TargetCredential | None = field(repr=False)
    profile: LocalStackProfile | None = None


@dataclass(frozen=True, slots=True)
class HierarchyParentOutcome:
    """Present safe parent choices or the company decision needed to read them."""

    policy: HierarchyParentPolicy | None = None
    choices: tuple[HierarchyParentChoice, ...] = ()
    companies: OdooCompanyAccess | None = None
    error: str | None = None
    parents_loaded: bool = False
    has_more: bool = False


def prepare_hierarchy_access(
    context: WebContext, workspace_id: str, model: str, actor: Actor
) -> PreparedHierarchyAccess:
    """Authorize before reading child stores or credentials in a local scope."""
    context.workspace_states.authorization.require(
        actor, Capability.NORMALIZATION_DECIDE, workspace_id=workspace_id
    )
    context.workspace_states.authorization.require(
        actor, Capability.PROJECT_EDIT, workspace_id=workspace_id
    )
    state = context.queries.get(workspace_id)
    if state.source_mode is not SourceMode.FILE:
        raise WorkspaceError("Build this hierarchy from an accepted source file")
    selection = context.queries.get_source_selection(workspace_id)
    if selection is None:
        raise WorkspaceError("Accept the source tables before selecting Odoo parents")
    target_model = _existing_catalog_model(context, state, model)
    plan = context.queries.get_derived_entity_plan(workspace_id)
    profile = _selected_local_profile(context, state)
    owner = context.target_credential_workspace(
        workspace_id, workspace_state=state, actor=actor
    )
    credential = get_target_credential(
        context.secret_store, owner, TargetCredentialRole.READ
    )
    if profile is None and credential is None:
        raise SecretStoreError(
            "Enter the checking key before finding existing Odoo parents"
        )
    return PreparedHierarchyAccess(
        state,
        selection.content_hash,
        plan.content_hash if plan else None,
        target_model,
        credential,
        profile,
    )


async def resolve_hierarchy_parent(
    context: WebContext,
    workspace_id: str,
    *,
    actor: Actor,
    model: str,
    name_field: str,
    parent_field: str,
    existing_levels: int,
    selected: str,
    search: str,
    company_id: str,
    find: bool = False,
    companies_only: bool = False,
) -> HierarchyParentOutcome:
    """Read Odoo between bounded local operations and recheck before publication."""
    prepared = await run_local_operation(
        prepare_hierarchy_access, context, workspace_id, model, actor
    )

    def read():
        state = prepared.state
        profile, credential = prepared.profile, prepared.credential

        def company_access():
            if profile is not None:
                return context.local_odoo_reader.get_company_access(state, profile)
            if credential is not None:
                return read_company_access(state, credential.secret)
            raise SecretStoreError("Enter the checking key before loading companies")

        if companies_only:
            return HierarchyParentOutcome(companies=company_access()), state

        def read_metadata(read_state):
            if profile is not None:
                return context.local_odoo_reader.get_model_metadata(
                    read_state, profile, (prepared.model,)
                ), None
            if credential is None:
                raise SecretStoreError(
                    "Enter the checking key before finding Odoo parents"
                )
            if context.readiness_reader is not None:
                return context.schema_reader(
                    replace(read_state, intended_models=(prepared.model,)),
                    credential.secret,
                ), None
            reader = hierarchy_parent_connector(read_state, credential.secret)
            return reader.get_model_metadata(
                (MetadataRequest(prepared.model, fields=(), all_fields=True),),
                include_create_defaults=False,
            ), reader

        metadata, connector = read_metadata(state)
        shape = hierarchy_lookup_shape(
            metadata, prepared.model, name_field, parent_field
        )
        access, company = None, None
        if shape.company_field is not None:
            access = company_access()
            desired = company_id or (
                str(state.odoo_company.company_id) if state.odoo_company else ""
            )
            company = next(
                (item for item in access.choices if str(item.company_id) == desired),
                None,
            )
            if desired and company is None:
                raise WorkspaceError(
                    "This company is unavailable to the checking user. Check Odoo access and choose again"
                )
            if company is not None:
                state = replace(state, odoo_company=company)
                if state != prepared.state:
                    metadata, connector = read_metadata(state)
                    if (
                        hierarchy_lookup_shape(
                            metadata, prepared.model, name_field, parent_field
                        )
                        != shape
                    ):
                        raise WorkspaceError(
                            "The Odoo hierarchy fields changed while checking its company. Check them again"
                        )
        if shape.company_field is not None and company is None:
            return HierarchyParentOutcome(
                companies=access,
                error="Choose Company for this load, then load the existing Odoo parents",
            ), state
        scoped = replace(
            state,
            intended_models=tuple(
                sorted(
                    {
                        prepared.model,
                        *({"res.company"} if shape.company_field else set()),
                    }
                )
            ),
        )
        identity_before = (
            context.read_identity_probe(
                scoped, credential.secret, scoped.intended_models
            )
            if connector is not None and credential is not None
            else None
        )

        def records(requests, apply_parent_domain):
            if context.readiness_reader is not None:
                _metadata, result = context.readiness_reader(
                    scoped,
                    tuple(
                        MetadataRequest(item.model, fields=item.fields)
                        for item in requests
                    ),
                    requests,
                )
            elif profile is not None:
                _metadata, result = context.local_odoo_reader.get_preflight_snapshots(
                    scoped,
                    profile,
                    tuple(
                        MetadataRequest(item.model, fields=item.fields)
                        for item in requests
                    ),
                    requests,
                    related_models=scoped.intended_models,
                    validate_lookup_fields=True,
                    lookup_relationship=(shape.model, shape.parent_field)
                    if apply_parent_domain and company is not None
                    else None,
                )
            elif connector is not None:
                if apply_parent_domain and company is not None:
                    domain = connector.linked_lookup_domain(
                        shape.model, shape.parent_field, shape.model
                    )
                    requests = tuple(
                        replace(item, domain=(*item.domain, *domain))
                        for item in requests
                    )
                result = connector.get_records(requests)
            else:
                raise SecretStoreError(
                    "Enter the checking key before finding Odoo parents"
                )
            if (
                result.fingerprint.target_hash != metadata.fingerprint.target_hash
                or result.fingerprint.odoo_version != metadata.fingerprint.odoo_version
            ):
                raise WorkspaceError(
                    "The Odoo target changed during the parent lookup. Check it again"
                )
            return result

        page = browse_parent_choices(shape, records, search=search) if find else None
        choices = (
            page.choices
            if page is not None
            else (
                read_parent_choices(shape, records, selected=selected)
                if selected
                else ()
            )
        )
        if connector is not None and credential is not None:
            identity_after = context.read_identity_probe(
                scoped, credential.secret, scoped.intended_models
            )
            if identity_before is None or (
                identity_before.target_hash,
                identity_before.principal_hash,
                identity_before.permission_hash,
                identity_before.context_hash,
                identity_before.readable_models,
            ) != (
                identity_after.target_hash,
                identity_after.principal_hash,
                identity_after.permission_hash,
                identity_after.context_hash,
                identity_after.readable_models,
            ):
                raise WorkspaceError(
                    "The checking user's Odoo access changed during the parent lookup. Check it again"
                )
        policy = (
            None
            if find
            else parent_policy(
                shape,
                choices[0] if choices else None,
                existing_levels,
                company.name if company is not None else None,
            )
        )
        return HierarchyParentOutcome(
            policy=policy,
            choices=choices,
            companies=access,
            parents_loaded=find,
            has_more=page.has_more if page is not None else False,
        ), state

    outcome, checked_state = await run_in_threadpool(read)

    def publish():
        current = prepare_hierarchy_access(context, workspace_id, model, actor)
        if (
            current.state != prepared.state
            or current.source_hash != prepared.source_hash
            or current.plan_hash != prepared.plan_hash
            or current.profile != prepared.profile
            or (current.credential.binding_hash if current.credential else None)
            != (prepared.credential.binding_hash if prepared.credential else None)
        ):
            raise WorkspaceError(
                "The hierarchy, company, or checking key changed during the lookup. Check the parents again"
            )
        if (
            checked_state.odoo_company != current.state.odoo_company
            and checked_state.odoo_company is not None
        ):
            context.workspace_states.select_odoo_company(
                workspace_id,
                actor=actor,
                expected_revision=current.state.revision,
                selection=checked_state.odoo_company,
            )
        return outcome

    return await run_local_operation(publish)
