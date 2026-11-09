"""Serve protected duplicate comparisons and versioned exclusion commands.

Each request owns one bounded synchronous database operation. Applying a
selection uses the existing mapping receipt and optimistic draft version;
remote identity checking starts separately after the save has completed.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from impodo.application.workspace.mapping.duplicate_rows import (
    PREPARED_DUPLICATE_REASONS,
    DuplicateRowReview,
    DuplicateRows,
    PreparedDuplicateEvidence,
)
from impodo.domain.mapping.mutations import (
    MappingMutationAction,
    MappingMutationReceipt,
    MappingMutationState,
)
from impodo.domain.mapping.validation.evidence import mapping_issue_fingerprint
from impodo.domain.preparation.quality import QualityCauseImpact
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import Actor, Capability
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import SourceMode
from impodo.web.composition.page_reads import run_local_operation, run_page_read
from impodo.web.context import WebContext
from impodo.web.forms import (
    _mapping_request_form,
    _optional_nonnegative_query_int,
    _secure_form,
    _text,
    _texts,
)
from impodo.web.request_identity import request_identity
from impodo.web.security import require_session
from impodo.web.source_stage_gate import source_stage_message


def duplicate_row_payload(review: DuplicateRows) -> dict[str, object]:
    """Expose complete compared values only through this protected response."""
    groups: dict[str, dict[str, object]] = {}
    rows_by_group: dict[str, list[dict[str, object]]] = {}
    for index, (member, values) in enumerate(zip(review.page.rows, review.rows, strict=True)):
        if member.group_hash not in groups:
            rows_by_group[member.group_hash] = []
            groups[member.group_hash] = {
                "key": " · ".join(
                    f"{field}: {value}"
                    for field, value in zip(
                        member.related_fields, member.key, strict=True
                    )
                ),
                "total": member.source_match_count,
                "rows": rows_by_group[member.group_hash],
            }
        rows_by_group[member.group_hash].append(
            {
                "position": member.member_position,
                "source_row": review.source_numbers[index] if review.source_numbers else None,
                "values": [
                    ""
                    if values.get(column.stable_key) is None
                    else str(values[column.stable_key])
                    for column in review.dataset.columns
                ],
            }
        )
    return {
        "dataset": review.dataset.name,
        "dataset_id": review.dataset.dataset_id,
        "dataset_index": next(
            index
            for index, item in enumerate(review.selection.datasets)
            if item.dataset_id == review.dataset.dataset_id
        ),
        "check_id": review.page.check_id,
        "quality_cause": review.quality_cause,
        "expected_version": review.draft.version,
        "expected_parent_version": review.draft.base_mapping_version,
        "columns": [item.source_name for item in review.dataset.columns],
        "exclusion_source_key": review.exclusion_source_key,
        "identifier_message": review.identifier_message,
        "groups": list(groups.values()),
        "unavailable": review.unavailable,
        "matching_count": review.page.matching_count,
        "start": review.page.start_position,
        "end": review.page.end_position,
        "previous": review.page.previous_before,
        "next": review.page.next_after,
        "included_count": len(review.included),
    }


def build_duplicate_rows_router(context: WebContext) -> APIRouter:
    """Keep the row-comparison interaction outside the mapping form parser."""
    router = APIRouter()
    service = DuplicateRowReview(
        context.mapping_workspace, context.matching_order, context.categorical_coverage
    )

    def access(request: Request, workspace_id: str):
        actor = request_identity(request, context.identity_resolver).actor
        owner = context.workspace_access.resolve(
            workspace_id, actor=actor, capability=Capability.PROTECTED_EVIDENCE_READ
        )
        context.workspace_access.resolve(
            workspace_id, actor=actor, capability=Capability.MAPPING_EDIT
        )
        if owner.recipe_application_id is not None:
            raise WorkspaceError(
                "This Recipe run has fixed row rules. Revise the Recipe and start a new reviewed run to change exclusions."
            )
        view = context.workspace_views.get(workspace_id, actor=actor)
        if view.migration_project.data_classification.value == "RESTRICTED":
            raise WorkspaceError(
                "Automatic row comparisons and exclusions are unavailable for restricted data. Review the source-key rule manually."
            )
        if (
            context.preparation_jobs is not None
            and context.preparation_jobs.active(workspace_id) is not None
        ):
            raise WorkspaceError(
                "Preparation is using the saved data. Wait for it to finish before changing rows to use."
            )
        readiness = context.navigation.get_source_readiness(workspace_id)
        if not readiness.ready:
            raise WorkspaceError(source_stage_message(readiness))
        return actor

    def prepared_evidence(workspace_id: str, actor: Actor, run_id: str, cause_key: str, page: int):
        if not cause_key:
            return None
        navigation = context.navigation.get_for_workspace(workspace_id, actor=actor)
        facts = navigation.facts
        if (
            navigation.workspace_state.source_mode is not SourceMode.FILE
            or not run_id or run_id != facts.quality_run_id
            or facts.quality_staging_run_id != facts.staging_run_id
        ):
            raise WorkspaceError("Prepare the current data again before reviewing duplicates.")
        evidence = context.queries.get_quality_cause_review_page(
            workspace_id, run_id, cause_key, page=page, page_size=100, direct_only=True,
        )
        cause = evidence.selected_cause
        if cause is None or cause.reason_code not in PREPARED_DUPLICATE_REASONS:
            raise WorkspaceError("Choose a current duplicate finding from Prepare data.")
        collisions = (
            context.queries.get_quality_collision_group_sizes(
                workspace_id, run_id,
                tuple(item.row.row_id for item in evidence.items if item.cause_impact is QualityCauseImpact.DIRECT),
            ) if cause.reason_code == "POST_TRANSFORM_IDENTITY_COLLISION" else {}
        )
        return PreparedDuplicateEvidence(run_id, evidence, collisions)

    @router.get("/workspaces/{workspace_id}/mapping/duplicate-rows")
    async def compare(request: Request, workspace_id: str):
        """Read at most 100 current duplicate members without contacting Odoo."""
        require_session(request)

        def read():
            actor = access(request, workspace_id)
            check = request.query_params.get("check", "")
            after = _optional_nonnegative_query_int(request.query_params.get("after"))
            before = _optional_nonnegative_query_int(request.query_params.get("before"))
            review = service.review(
                workspace_id,
                request.query_params.get("dataset", ""),
                check,
                after=after,
                before=before,
                prepared=prepared_evidence(
                    workspace_id, actor, check, request.query_params.get("quality_cause", ""),
                    after or before or 1,
                ),
                actor=actor,
            )
            return JSONResponse(
                duplicate_row_payload(review), headers={"Cache-Control": "no-store"}
            )

        try:
            return await run_page_read(read)
        except (ValueError, WorkspaceError) as error:
            return JSONResponse(
                {"detail": str(error)},
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )

    @router.post("/workspaces/{workspace_id}/mapping/duplicate-rows")
    async def exclusions(request: Request, workspace_id: str):
        """Preview exact effects or save the previously reviewed selection."""
        require_session(request)
        form = await _mapping_request_form(request)
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "dataset_id",
                "check_id",
                "after",
                "before",
                "position",
                "expected_version",
                "expected_parent_version",
                "preview_hash",
                "operation_id",
                "action",
                "exclusion_source_key",
                "quality_cause",
            },
        )

        def command():
            actor = access(request, workspace_id)
            action = _text(form, "action")
            if action not in {"preview", "apply"}:
                raise WorkspaceError("Choose preview or apply exclusions.")
            version = int(_text(form, "expected_version"))
            operation_id = _text(form, "operation_id")
            quality_cause = _text(form, "quality_cause")
            parent_version = (
                int(_text(form, "expected_parent_version"))
                if _text(form, "expected_parent_version") else None
            )
            if quality_cause:
                context.workspace_access.resolve(workspace_id, actor=actor, capability=Capability.MAPPING_SUBMIT)
            started = False
            try:
                if action == "apply":
                    receipt = context.mapping_workspace.begin_mutation(
                        workspace_id,
                        operation_id=operation_id,
                        action=MappingMutationAction.SAVE_PROGRESS,
                        request_hash=content_hash(
                            {
                                "duplicate_exclusions": sorted(
                                    (key, value)
                                    for key, value in form.multi_items()
                                    if key != "csrf_token"
                                )
                            }
                        )[7:],
                        submitted_working_draft_version=version,
                        submitted_mapping_revision_version=parent_version,
                        actor=actor,
                    )
                    started = not receipt.replayed
                    if receipt.replayed:
                        if receipt.state is MappingMutationState.COMMITTED:
                            return _saved_exclusions_result(context, workspace_id, receipt, quality_cause, actor=actor)
                        raise WorkspaceError(
                            receipt.failure_detail
                            or "The previous save is still pending. Check the save outcome before retrying."
                        )
                check = _text(form, "check_id")
                after = _optional_nonnegative_query_int(_text(form, "after"))
                before = _optional_nonnegative_query_int(_text(form, "before"))
                review = service.review(
                    workspace_id,
                    _text(form, "dataset_id"),
                    check,
                    after=after,
                    before=before,
                    prepared=prepared_evidence(workspace_id, actor, check, quality_cause, after or before or 1),
                    exclusion_source_key=_text(form, "exclusion_source_key"),
                    actor=actor,
                )
                if review.draft.version != version:
                    raise WorkspaceError(
                        "The saved matching choices changed. Reopen duplicate review before applying exclusions."
                    )
                if quality_cause:
                    submission = context.queries.get_mapping_submission(workspace_id, parent_version)
                    if (
                        parent_version is None or review.draft.base_mapping_version != parent_version
                        or submission is None or submission.mapping_content_hash != review.draft.content_hash
                    ):
                        raise WorkspaceError("The prepared finding no longer matches the confirmed rules.")
                positions = tuple(int(value) for value in _texts(form, "position"))
                policy, preview = service.preview(workspace_id, review, positions)
                if action == "preview":
                    return preview
                if preview["preview_hash"] != _text(form, "preview_hash"):
                    raise WorkspaceError(
                        "The exclusion preview changed. Review the selected rows again before applying."
                    )
                draft = service.save(
                    workspace_id, review, policy, operation_id=operation_id, actor=actor
                )
                if quality_cause:
                    saved_receipt = context.mapping_workspace.get_mutation_receipt(workspace_id, operation_id, actor=actor)
                    if saved_receipt is None or saved_receipt.state is not MappingMutationState.COMMITTED:
                        raise WorkspaceError("The exclusion save could not be verified.")
                    return _saved_exclusions_result(context, workspace_id, saved_receipt, quality_cause, actor=actor)
                return {"saved": True, "version": draft.version}
            except (ValueError, WorkspaceError) as error:
                if started:
                    receipt = context.mapping_workspace.get_mutation_receipt(
                        workspace_id, operation_id, actor=actor
                    )
                    if (
                        receipt is not None
                        and receipt.state is MappingMutationState.PENDING
                    ):
                        context.mapping_workspace.reject_mutation(
                            workspace_id,
                            operation_id,
                            failure_code="DUPLICATE_EXCLUSION_REJECTED",
                            failure_detail=str(error),
                            actor=actor,
                        )
                raise

        try:
            result = await run_local_operation(command)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except (ValueError, WorkspaceError) as error:
            return JSONResponse(
                {"detail": str(error)},
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )

    return router


def _saved_exclusions_result(
    context: WebContext, workspace_id: str, receipt: MappingMutationReceipt,
    quality_cause: str, *, actor: Actor,
) -> dict[str, object]:
    """Check and confirm a reviewed subset, leaving genuine new issues for review."""

    result: dict[str, object] = {"saved": True, "version": receipt.working_draft_version}
    if not quality_cause:
        return result
    mapping = context.mapping_workspace
    parent_version = receipt.submitted_mapping_revision_version
    saved_version = receipt.working_draft_version
    draft = context.queries.get_mapping_working_draft(workspace_id)
    revision = context.queries.get_mapping_revision(workspace_id)
    if (
        parent_version is None or saved_version is None or draft is None or revision is None
        or draft.content_hash != receipt.content_identity
        or not (
            (revision.version == parent_version and draft.version == saved_version)
            or (
                revision.parent_version == parent_version and draft.version == saved_version + 1
                and revision.definition.content_hash == draft.content_hash
            )
        )
    ):
        raise WorkspaceError("The exclusions were saved. Newer matching choices now need review.")
    old_submission = context.queries.get_mapping_submission(workspace_id, parent_version)
    try:
        if old_submission is None:
            raise WorkspaceError("Confirm the earlier field matches before preparing again.")
        if revision.version == parent_version:
            revision, validation = mapping.check_definition(
                workspace_id, datasets=draft.definition.datasets,
                expected_parent_version=parent_version,
                expected_working_draft_version=draft.version, actor=actor,
            )
        else:
            validation = context.queries.get_mapping_validation(workspace_id, revision.version)
        if validation is None or validation.status.value == "INVALID":
            raise WorkspaceError("The updated selection has matching checks that need review.")
        warnings = {mapping_issue_fingerprint(issue) for issue in validation.issues if issue.severity == "warning"}
        if not warnings.issubset(old_submission.warning_acknowledgements):
            raise WorkspaceError("The updated selection has new warnings to review before preparing.")
        draft = context.queries.get_mapping_working_draft(workspace_id)
        if draft is None or draft.content_hash != receipt.content_identity:
            raise WorkspaceError("The saved matching choices changed during the row check.")
        snapshot, confirmation = context.row_inclusion_reviews.current(workspace_id, actor=actor)
        if snapshot is None:
            snapshot = context.row_inclusion_reviews.check_current(workspace_id, actor=actor)
        if not snapshot.confirmable:
            raise WorkspaceError("The updated rows-to-use check needs review.")
        if confirmation is None and any(
            item.row_inclusion.included_parent_field for item in draft.definition.datasets
        ):
            raise WorkspaceError("Review the checked exclusions for tables that follow a parent before preparing.")
        if confirmation is None:
            context.row_inclusion_reviews.confirm_current(
                workspace_id, datasets=draft.definition.datasets,
                expected_revision_version=revision.version,
                expected_working_draft_version=draft.version, actor=actor,
            )
        mapping.submit_current(
            workspace_id, datasets=draft.definition.datasets,
            expected_version=revision.version,
            expected_working_draft_version=draft.version,
            warning_acknowledgements=old_submission.warning_acknowledgements,
            actor=actor,
        )
    except WorkspaceError as error:
        return {
            **result, "follow_up_message": str(error),
            "follow_up_url": f"/workspaces/{workspace_id}/mapping?step=check",
        }
    return {**result, "prepare_url": f"/workspaces/{workspace_id}/summary/check"}
