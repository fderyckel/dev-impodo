"""Publish fictional checked field effects for browser and route qualification."""

from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    ScalarFieldMapping,
)
from impodo.domain.staging.transformation_impact import _TransformationImpactCollector
from impodo.domain.workspace.reference_keys import reference_policy_hash
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.support.identity import resolve_local_actor


def field_effects_workspace(fixture, *, major=20, remote=False, field_count=6):
    """Publish fictional complete counts through the existing snapshot owner."""
    workspace, dataset, key = fixture._mapping_ready_workspace(
        scalar_field_count=field_count,
        odoo_major=major,
        connection_mode=OdooConnectionMode.REMOTE
        if remote
        else OdooConnectionMode.LOCAL,
    )
    context = fixture.app.state.context
    actor = resolve_local_actor(context)
    mapping = DatasetMapping(
        dataset.dataset_id,
        "res.partner",
        source_identity_column_keys=(dataset.columns[0].stable_key,),
        target_identity=(
            IdentityComponentMapping((dataset.columns[0].stable_key,), key.key_fields),
        ),
        fields=tuple(
            ScalarFieldMapping(f"field_{index:04d}", dataset.columns[1].stable_key)
            for index in range(field_count)
        ),
    )
    revision, validation = context.mapping_workspace.check_definition(
        workspace,
        datasets=(mapping,),
        expected_parent_version=None,
        expected_working_draft_version=None,
        actor=actor,
    )
    fixture.assertEqual(validation.reference_policy_hash, reference_policy_hash(major))
    identity = context.transformation_impacts.context(workspace).identity
    outcomes = ("changed", "fallback", "invalid", "null", "provided", "unchanged")

    def build(sink):
        collector = _TransformationImpactCollector(
            revision.definition.content_hash, detail_limit=0, sink=sink
        )
        for index in range(field_count):
            collector.record(
                dataset=dataset.name,
                source_row=2,
                source_column="Value",
                target_field=f"field_{index:04d}",
                raw_value="Private original",
                proposed_value="Private prepared",
                rules="Source",
                outcome=outcomes[index % len(outcomes)],
            )
        return collector.report()

    snapshot = context.transformation_impacts.replace_snapshot(
        workspace, identity, build, actor=actor
    )
    return workspace, dataset, revision, snapshot
