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
    return CompiledMigrationPlan(
        plan_id=profile.profile.id,
        origin="profile_document",
        origin_hash=compiled_profile_origin_hash(payload),
        datasets=profile.datasets,
    )
