"""Compile validated YAML profile documents into runtime semantics."""

from __future__ import annotations

from ...profile import ProfileDocument
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
