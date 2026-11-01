"""Build fictional captured requirements for checklist and default journeys."""

from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    ScalarFieldMapping,
    ScalarValueSource,
    TargetFieldDisposition,
    TargetFieldHandling,
)
from tests.support.identity import resolve_local_actor


def required_values_workspace(fixture):
    workspace_id, dataset, key = fixture._mapping_ready_workspace(
        scalar_field_count=4,
        required_scalar_indexes=(0, 1, 2, 3),
        verified_default_scalar_indexes=(2,),
        relationship_field_count=1,
        relationship_field_type="one2many",
        required_relationship_indexes=(0,),
    )
    context = fixture.app.state.context
    mapping = DatasetMapping(
        dataset.dataset_id,
        "res.partner",
        source_identity_column_keys=(dataset.columns[0].stable_key,),
        target_identity=(
            IdentityComponentMapping((dataset.columns[0].stable_key,), key.key_fields),
        ),
        fields=(
            ScalarFieldMapping("field_0000", dataset.columns[1].stable_key),
            ScalarFieldMapping(
                "field_0001",
                value_source=ScalarValueSource.CONSTANT,
                literal_value="Private fixed value",
            ),
        ),
        target_field_dispositions=(
            TargetFieldDisposition("relation_0000", TargetFieldHandling.ODOO_MANAGED),
        ),
    )
    revision, validation = context.mapping_workspace.check_definition(
        workspace_id,
        datasets=(mapping,),
        expected_parent_version=None,
        expected_working_draft_version=None,
        actor=resolve_local_actor(context),
    )
    return workspace_id, dataset, revision, validation
