"""Fictional stock-shaped records with an optional compound linked scope."""

from dataclasses import replace

from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    IdentityNullPolicy,
    MappingDefinition,
    ReferenceKeyMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
)
from impodo.domain.preparation.source import SourceRow
from impodo.domain.workspace.contracts import SourceDatasetColumn
from tests.integration.columnar import test_polars_transformation as fixtures


def optional_scope_fixture(*, preparation=None):
    """Keep source rows distinct while matching one product across three batches."""
    source = replace(
        fixtures._selection().datasets[0],
        columns=tuple(
            SourceDatasetColumn(index, name, name, "string")
            for index, name in enumerate(
                ("row", "product", "lot", "company", "location", "quantity"), 1
            )
        ),
    )
    selection = replace(fixtures._selection(), datasets=(source,))
    resolver = RelationshipResolver(
        ResolverOrigin.TARGET_CATALOG,
        model="x.batch",
        key_mappings=(ReferenceKeyMapping("lot", "name", preparation),),
        scope_mappings=(ReferenceKeyMapping("company", "company"),),
    )
    component = IdentityComponentMapping(
        ("lot", "company"),
        ("batch_id",),
        resolver=resolver,
        null_policy=IdentityNullPolicy.OPTIONAL_SCOPE_NULL,
        blank_source_column_key="lot",
    )
    mapping = DatasetMapping(
        source.dataset_id,
        "x.stock",
        source_identity_column_keys=("row",),
        target_identity=(IdentityComponentMapping(("product",), ("code",)),),
        target_scope=(
            IdentityComponentMapping(("location",), ("location",)),
            component,
        ),
        fields=(ScalarFieldMapping("quantity", "quantity", value_type="integer"),),
    )
    definition = MappingDefinition(
        "optional-scope", selection.content_hash, fixtures.HASH_B, (mapping,)
    )
    rows = tuple(
        SourceRow(
            index,
            dict(
                zip(
                    ("row", "product", "lot", "company", "location", "quantity"),
                    (str(index), "P", lot, company, "L", "10"),
                    strict=True,
                )
            ),
        )
        for index, (lot, company) in enumerate(
            ((None, "C"), ("LOT-001", "C"), ("LOT-002", "C")),
            2,
        )
    )
    return selection, definition, rows
