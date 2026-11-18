"""Build real Stage 3 ordering workspaces with fictional, read-only Odoo data."""

from datetime import UTC, datetime

from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.odoo.contracts import RecordSnapshot
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.shared.models import OdooReadIdentity
from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from tests.support.recipe_lifecycle import metadata_for_schema


def ordering_workspace(fixture, *, major=20, mode="REMOTE"):
    """Save a product-to-category match and a parts link that remains unchecked."""
    wid = fixture.freeze_files(
        {
            "products": (
                b"Reference,Category\n"
                b"P000000,Group00000\nP000001,Group00001\nP000002,Group00002\n"
                b"P000003,Group00000\nP000004,Group00001\nP000005,Group00002\n"
            ),
            "categories": b"Name,Number\nGroup00000,0\nGroup00001,1\nGroup00002,2\n",
        }
    )
    ctx = fixture.context
    catalog = fixture.schema(
        wid,
        major=major,
        mode=mode,
        extra_models=(
            SchemaModel(
                "x.item",
                "Product",
                (
                    SchemaField(
                        "ref", "Reference", "char", False, False, None, None, ()
                    ),
                    SchemaField(
                        "category_id",
                        "Category",
                        "many2one",
                        False,
                        False,
                        "x.group",
                        None,
                        (),
                    ),
                    SchemaField(
                        "parts", "Parts", "one2many", False, False, "x.item", None, ()
                    ),
                ),
            ),
            SchemaModel(
                "x.group",
                "Category",
                (SchemaField("name", "Name", "char", False, False, None, None, ()),),
            ),
        ),
        extra_keys=tuple(
            BusinessKeyDefinition(
                model + ":" + field,
                model,
                (field,),
                status=BusinessKeyStatus.CONFIRMED,
            )
            for model, field in (("x.item", "ref"), ("x.group", "name"))
        ),
    )
    selection = ctx.queries.get_source_selection(wid)
    datasets = {dataset.name: dataset for dataset in selection.datasets}
    product = datasets["products"]
    group = datasets["categories"]
    ref, category = (col.stable_key for col in product.columns)
    name = group.columns[0].stable_key
    group_mapping = DatasetMapping(
        group.dataset_id,
        "x.group",
        source_identity_column_keys=(name,),
        target_identity=(IdentityComponentMapping((name,), ("name",)),),
    )
    product_mapping = DatasetMapping(
        product.dataset_id,
        "x.item",
        source_identity_column_keys=(ref,),
        target_identity=(IdentityComponentMapping((ref,), ("ref",)),),
        relationships=(
            RelationshipMapping(
                "category_id",
                "many2one",
                (category,),
                resolver=RelationshipResolver(
                    origin=ResolverOrigin.TARGET_THEN_DATASET,
                    model="x.group",
                    dataset_id=group.dataset_id,
                    key_mappings=(ReferenceKeyMapping(category, "name"),),
                ),
            ),
            RelationshipMapping(
                "parts",
                "one2many",
                (ref,),
                resolver=RelationshipResolver(
                    origin=ResolverOrigin.DATASET,
                    dataset_id=product.dataset_id,
                ),
            ),
        ),
    )
    ctx.mapping_workspace.save_working_draft(
        wid,
        datasets=(group_mapping, product_mapping),
        expected_version=None,
        actor=fixture.actor,
    )
    requests = []

    def reader(_state, metadata, records):
        requests.append((metadata, records))
        projected = {req.model: req.fields for req in metadata}
        fields = metadata_for_schema(catalog, projected)
        return fields, RecordSnapshot(
            fields.fingerprint, {model: () for model in projected}, projected
        )

    ctx.read_identity_probe = lambda _state, _key, models: OdooReadIdentity(
        target_hash=catalog.connection_target_hash,
        principal_hash=catalog.read_principal_hash,
        permission_hash=catalog.read_permission_hash,
        context_hash=catalog.read_context_hash,
        readable_models=tuple(sorted(models)),
        observed_at=datetime.now(UTC).isoformat(),
    )
    ctx.readiness_reader = reader
    return wid, requests
