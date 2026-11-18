"""Build the fully mapped, 1,000-field qualification fixture on either host."""

import sys

import psutil

from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingTargetMode,
    ScalarFieldMapping,
    ScalarValueSource,
)
from tests.support.identity import resolve_local_actor


def peak_memory_bytes():
    """Return the kernel process high-water mark in bytes."""
    if sys.platform == "win32":
        return psutil.Process().memory_info().peak_wset
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def mapped_catalog(fixture):
    """Save all catalogue fields before measuring either request workload."""
    workspace_id, dataset, business_key = fixture._mapping_ready_workspace(
        scalar_field_count=1000
    )
    source_identity, source_value = dataset.columns
    context = fixture.app.state.context
    initial = context.mapping_workspace.save_working_draft(
        workspace_id,
        datasets=(
            DatasetMapping(
                dataset_id=dataset.dataset_id,
                target_model="res.partner",
                mode=MappingTargetMode.UPSERT,
                source_identity_column_keys=(source_identity.stable_key,),
                target_identity=(
                    IdentityComponentMapping(
                        source_column_keys=(source_identity.stable_key,),
                        target_fields=("ref",),
                    ),
                ),
                fields=tuple(
                    ScalarFieldMapping(
                        target_field=f"field_{index:04d}",
                        source_column_key=source_value.stable_key,
                        value_source=ScalarValueSource.SOURCE,
                    )
                    for index in range(1000)
                ),
            ),
        ),
        expected_version=None,
        actor=resolve_local_actor(context),
    )
    assert initial.version == 1
    return workspace_id, dataset, business_key
