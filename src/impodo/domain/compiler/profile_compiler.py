"""Adapt validated YAML profiles to the shared migration-plan contract.

The compiler preserves validated dataset semantics and binds their authoring
origin hash. It performs no source I/O or transformation execution, allowing
profile and browser authoring to share downstream preparation and preflight.
"""

from __future__ import annotations

from impodo.domain.recipe.profile import ProfileDocument

from .contracts import CompiledMigrationPlan, compiled_profile_origin_hash


def compile_profile_document(profile: ProfileDocument) -> CompiledMigrationPlan:
    """Adapt one authoring profile at the runtime boundary."""

    payload = profile.model_dump(mode="json", exclude_none=True)
    for dataset in payload["datasets"]:
        for scalar in dataset["fields"].values():
            if not scalar["must_be_unique"]:
                scalar.pop("must_be_unique")
    optional_scopes = any(item.null_policy == "optional_scope_null" for dataset in profile.datasets for item in dataset.target_identity.scope)
    unique_fields = any(field.must_be_unique for dataset in profile.datasets for field in dataset.fields.values())
    return CompiledMigrationPlan(
        contract_version=6 if unique_fields else 5 if optional_scopes else 4 if any(item.hierarchy_parent is not None for item in profile.datasets) else 3,
        compiler_version=7 if unique_fields else 6 if optional_scopes else 5 if any(item.hierarchy_parent is not None for item in profile.datasets) else 4,
        plan_id=profile.profile.id,
        origin="profile_document",
        origin_hash=compiled_profile_origin_hash(payload),
        datasets=profile.datasets,
    )
