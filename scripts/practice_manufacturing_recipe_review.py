"""Explicit review of pinned manufacturing type advice through normal services.

This does not alter generic Recipe applicability or discard issues. It checks
the exact current validation and field ownership, then submits its real warning
fingerprints through the ordinary guarded application services. File hashes
must have passed the manufacturing runner's validate_files before activation.
"""

from impodo.domain.mapping.validation.evidence import mapping_issue_fingerprint
from impodo.web.composition.page_reads import run_local_operation_sync
from scripts.qualify_practice_customers import require

ADVISORY = "MAPPING_SOURCE_TYPE_ADVISORY_MISMATCH"
ROW_REVIEW = "MAPPING_ROW_INCLUSION_CONFIRMATION_REQUIRED"
REVIEWED_ADVISORIES = {
    "categories": frozenset(),
    "products": frozenset(),
    "product-status": frozenset({(ADVISORY, "product.template", "active")}),
    "boms": frozenset({(ADVISORY, "mrp.bom", "product_qty")}),
}


def reviewed_warning_fingerprints(validation, revision, phase):
    """Fail closed unless every real warning is an explicitly reviewed field."""
    require(phase in REVIEWED_ADVISORIES, "RECIPE_MANUFACTURING_PHASE_INVALID")
    require(
        validation is not None
        and validation.status.value in {"VALID", "VALID_WITH_WARNINGS"},
        "RECIPE_MAPPING_INVALID",
    )
    require(
        validation.mapping_content_hash == revision.definition.content_hash,
        "RECIPE_MAPPING_VALIDATION_STALE",
    )
    datasets = {dataset.dataset_id: dataset for dataset in revision.definition.datasets}
    fingerprints = []
    for issue in validation.issues:
        require(issue.severity in {"warning", "advisory"}, "RECIPE_MAPPING_INVALID")
        if issue.severity == "advisory":
            continue
        require(
            (issue.code, issue.target_model, issue.target_field)
            in REVIEWED_ADVISORIES[phase],
            "RECIPE_MANUFACTURING_REVIEW_UNEXPECTED",
        )
        dataset = datasets.get(issue.dataset_id)
        require(
            dataset is not None
            and dataset.target_model == issue.target_model
            and any(
                field.target_field == issue.target_field for field in dataset.fields
            ),
            "RECIPE_MANUFACTURING_REVIEW_FIELD_DIFFERED",
        )
        fingerprints.append(mapping_issue_fingerprint(issue))
    return tuple(fingerprints)


def confirm_manufacturing_recipe(fixture, application, phase, *, expected_population):
    """Review exactly the known fixture advice and the fresh row population."""
    require(
        application.status.value in {"READY", "BLOCKED"},
        "RECIPE_APPLICABILITY_BLOCKED",
    )
    context, actor = fixture.context, fixture.actor
    mappings = context.mapping_workspace.mappings
    revision = mappings.get_mapping_revision(application.workspace_id)
    require(revision is not None, "RECIPE_MAPPING_UNAVAILABLE")
    validation = mappings.get_mapping_validation(
        application.workspace_id, revision.version
    )
    fingerprints = reviewed_warning_fingerprints(validation, revision, phase)
    if application.status.value == "READY":
        return application
    working = mappings.get_mapping_working_draft(application.workspace_id)
    require(working is not None, "RECIPE_MAPPING_UNAVAILABLE")

    issues = context.run_planning.repository.list_issues(application.application_id)
    blocking = {issue.code for issue in issues if issue.blocks}
    require(bool(blocking), "RECIPE_APPLICABILITY_BLOCKED")
    require(blocking <= {ROW_REVIEW, ADVISORY}, "RECIPE_APPLICABILITY_BLOCKED")
    # Application issues omit model/field. The current mapping validation is
    # therefore authoritative for reviewing the complete exact warning set.
    require(
        blocking - {ROW_REVIEW}
        == {issue.code for issue in validation.issues if issue.severity == "warning"},
        "RECIPE_MANUFACTURING_REVIEW_UNEXPECTED",
    )
    review = run_local_operation_sync(
        context.row_inclusion_reviews.check_current,
        application.workspace_id,
        actor=actor,
    )
    require(
        (review.source_row_count, review.included_count, review.excluded_count)
        == expected_population,
        "RECIPE_POPULATION_DIFFERED",
    )
    require(review.cannot_evaluate_count == 0, "RECIPE_POPULATION_UNCHECKED")
    run_local_operation_sync(
        context.row_inclusion_reviews.confirm_current,
        application.workspace_id,
        datasets=revision.definition.datasets,
        expected_revision_version=revision.version,
        expected_working_draft_version=working.version,
        actor=actor,
    )
    run_local_operation_sync(
        context.mapping_workspace.submit_current,
        application.workspace_id,
        datasets=revision.definition.datasets,
        expected_version=revision.version,
        expected_working_draft_version=working.version,
        actor=actor,
        warning_acknowledgements=fingerprints,
    )
    confirmed = run_local_operation_sync(
        context.run_planning.confirm_application_mapping,
        application.application_id,
        actor=actor,
    )
    require(confirmed.status.value == "READY", "RECIPE_APPLICABILITY_BLOCKED")
    return confirmed
