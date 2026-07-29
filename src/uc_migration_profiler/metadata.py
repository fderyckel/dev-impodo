"""Validate a profile against captured Odoo model metadata.

`planner.py` requests only the models and fields needed by the profile, and a
connector returns a :class:`MetadataSnapshot`. This module checks that the
snapshot can support the proposed mapping before target records are compared.
Problems are returned as structured :class:`Issue` objects; they are not
raised as control-flow exceptions because the review report must explain every
missing or incompatible field.
"""

from __future__ import annotations

from .connectors import MetadataSnapshot
from .models import Issue
from .profile import DatasetSpec, FieldSpec, ProfileDocument, RelationSpec


TYPE_COMPATIBILITY = {
    "string": {"char", "text", "html", "selection"},
    "integer": {"integer"},
    "decimal": {"float", "monetary", "integer"},
    "boolean": {"boolean"},
    "date": {"date"},
    "datetime": {"datetime"},
}


def validate_profile_metadata(
    profile: ProfileDocument,
    snapshot: MetadataSnapshot,
) -> tuple[tuple[Issue, ...], tuple[dict[str, object], ...]]:
    """Validate all dataset and reference requirements against a snapshot.

    Returns:
        A pair containing blocking issues and deterministic coverage rows.
        Coverage rows drive the manifest/workbook explanation of which model
        fields were requested and available.
    """

    issues: list[Issue] = []
    coverage: list[dict[str, object]] = []
    if not snapshot.complete:
        issues.append(
            Issue(
                code="TARGET_SNAPSHOT_INCOMPLETE",
                message="metadata snapshot is incomplete",
            )
        )

    for dataset in profile.datasets:
        model = snapshot.models.get(dataset.target.model)
        if model is None:
            issues.append(
                Issue(
                    code="TARGET_MODEL_UNKNOWN",
                    message=f"target model {dataset.target.model!r} is unavailable",
                    dataset=dataset.name,
                )
            )
            coverage.append(
                {
                    "dataset": dataset.name,
                    "model": dataset.target.model,
                    "status": "MISSING_MODEL",
                    "requested_fields": 0,
                    "available_fields": 0,
                }
            )
            continue

        requested = _requested_fields(dataset)
        available_count = 0
        for field_name in sorted(requested):
            metadata = model.fields.get(field_name)
            if metadata is None:
                issues.append(
                    Issue(
                        code="TARGET_FIELD_UNKNOWN",
                        message=(
                            f"field {dataset.target.model}.{field_name} is unavailable"
                        ),
                        dataset=dataset.name,
                        field=field_name,
                    )
                )
                continue
            available_count += 1
            scalar = dataset.fields.get(field_name)
            relation = dataset.relations.get(field_name)
            if scalar is not None:
                issues.extend(
                    _validate_scalar(dataset, field_name, scalar, metadata.type, metadata.readonly)
                )
            if relation is not None:
                expected_model = (
                    relation.resolve.target_model
                    or profile.dataset(str(relation.resolve.dataset)).target.model
                )
                issues.extend(
                    _validate_relation(
                        dataset,
                        field_name,
                        relation,
                        metadata.type,
                        metadata.relation,
                        metadata.relation_field,
                        metadata.readonly,
                        expected_model,
                    )
                )

        for component in (
            *dataset.target_identity.components,
            *dataset.target_identity.scope,
        ):
            if component.resolve is None:
                continue
            field_name = component.target_fields[0]
            field_metadata = model.fields.get(field_name)
            if field_metadata is None:
                continue
            expected_model = (
                component.resolve.target_model
                or profile.dataset(str(component.resolve.dataset)).target.model
            )
            if field_metadata.type != "many2one":
                issues.append(
                    Issue(
                        code="TARGET_RELATION_KIND_INCORRECT",
                        message=(
                            f"identity field {field_name} is "
                            f"{field_metadata.type}, expected many2one"
                        ),
                        dataset=dataset.name,
                        field=field_name,
                    )
                )
            if field_metadata.relation != expected_model:
                issues.append(
                    Issue(
                        code="TARGET_RELATED_MODEL_INCORRECT",
                        message=(
                            f"identity field {field_name} relates to "
                            f"{field_metadata.relation!r}, expected "
                            f"{expected_model!r}"
                        ),
                        dataset=dataset.name,
                        field=field_name,
                    )
                )

        coverage.append(
            {
                "dataset": dataset.name,
                "model": dataset.target.model,
                "status": "COMPLETE" if available_count == len(requested) else "PARTIAL",
                "requested_fields": len(requested),
                "available_fields": available_count,
            }
        )

        seen_reference_requirements: set[
            tuple[str, tuple[str, ...]]
        ] = set()
        resolvers = [
            component.resolve
            for component in (
                *dataset.target_identity.components,
                *dataset.target_identity.scope,
            )
            if component.resolve is not None
        ]
        resolvers.extend(
            relation.resolve for relation in dataset.relations.values()
        )
        for resolve in resolvers:
            if resolve.target_model is None:
                continue
            requirement = (resolve.target_model, resolve.target_fields)
            if requirement in seen_reference_requirements:
                continue
            seen_reference_requirements.add(requirement)
            reference_model = snapshot.models.get(resolve.target_model)
            available_reference_fields = 0
            if reference_model is None:
                issues.append(
                    Issue(
                        code="TARGET_MODEL_UNKNOWN",
                        message=(
                            f"reference model {resolve.target_model!r} is unavailable"
                        ),
                        dataset=dataset.name,
                    )
                )
            else:
                for reference_field in resolve.target_fields:
                    if reference_field not in reference_model.fields:
                        issues.append(
                            Issue(
                                code="TARGET_FIELD_UNKNOWN",
                                message=(
                                    f"reference field {resolve.target_model}."
                                    f"{reference_field} is unavailable"
                                ),
                                dataset=dataset.name,
                                field=reference_field,
                            )
                        )
                    else:
                        available_reference_fields += 1
            coverage.append(
                {
                    "dataset": dataset.name,
                    "model": resolve.target_model,
                    "status": (
                        "COMPLETE"
                        if available_reference_fields == len(resolve.target_fields)
                        else "PARTIAL"
                    ),
                    "requested_fields": len(resolve.target_fields),
                    "available_fields": available_reference_fields,
                }
            )
    return tuple(issues), tuple(coverage)


def _requested_fields(dataset: DatasetSpec) -> set[str]:
    """Return all target fields whose metadata the dataset depends upon."""

    result = set(dataset.fields)
    result.update(dataset.relations)
    for component in (
        *dataset.target_identity.components,
        *dataset.target_identity.scope,
    ):
        result.update(component.target_fields)
    return result


def _validate_scalar(
    dataset: DatasetSpec,
    field_name: str,
    spec: FieldSpec,
    target_type: str,
    readonly: bool,
) -> list[Issue]:
    """Validate scalar type compatibility and future-write eligibility."""

    issues = []
    if target_type not in TYPE_COMPATIBILITY[spec.type]:
        issues.append(
            Issue(
                code="TARGET_TYPE_INCOMPATIBLE",
                message=f"{field_name} is {target_type}, profile expects {spec.type}",
                dataset=dataset.name,
                field=field_name,
            )
        )
    if readonly and not spec.validate_only:
        issues.append(
            Issue(
                code="TARGET_FIELD_READONLY",
                message=f"{field_name} is readonly but proposed for writing",
                dataset=dataset.name,
                field=field_name,
            )
        )
    return issues


def _validate_relation(
    dataset: DatasetSpec,
    field_name: str,
    spec: RelationSpec,
    target_type: str,
    related_model: str | None,
    inverse_field: str | None,
    readonly: bool,
    expected_model: str,
) -> list[Issue]:
    """Validate relation kind, related model, ownership, and readonly state."""

    issues = []
    if target_type != spec.kind:
        issues.append(
            Issue(
                code="TARGET_RELATION_KIND_INCORRECT",
                message=f"{field_name} is {target_type}, profile expects {spec.kind}",
                dataset=dataset.name,
                field=field_name,
            )
        )
    if related_model != expected_model:
        issues.append(
            Issue(
                code="TARGET_RELATED_MODEL_INCORRECT",
                message=(
                    f"{field_name} relates to {related_model!r}, "
                    f"profile expects {expected_model!r}"
                ),
                dataset=dataset.name,
                field=field_name,
            )
        )
    if target_type == "one2many":
        issues.append(
            Issue(
                code="TARGET_ONE2MANY_WRITE_OWNER_INVALID",
                message=f"{field_name} is one2many and cannot own an imported relation",
                dataset=dataset.name,
                field=field_name,
            )
        )
        if not inverse_field:
            issues.append(
                Issue(
                    code="TARGET_INVERSE_RELATION_MISSING",
                    message=f"{field_name} has no inverse relation field",
                    dataset=dataset.name,
                    field=field_name,
                )
            )
    if readonly and not spec.validate_only:
        issues.append(
            Issue(
                code="TARGET_FIELD_READONLY",
                message=f"{field_name} is readonly but proposed for writing",
                dataset=dataset.name,
                field=field_name,
            )
        )
    return issues
