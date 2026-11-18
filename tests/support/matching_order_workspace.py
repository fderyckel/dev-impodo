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
from impodo.domain.workspace.derived_entities import derived_dataset_links
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.recipe_lifecycle import metadata_for_schema


def ordering_workspace(
    fixture,
    *,
    shape="related",
    rows=6,
    categories=3,
    major=20,
    mode="REMOTE",
    partial=False,
):
    """Freeze CSVs, save a draft, and expose the reader's actual request plans."""
    items = "Reference,Category\n" + "".join(
        f"P{i:06},Group{i % categories:05}\n" for i in range(rows)
    )
    files = {"products": items.encode()}
    if shape == "related":
        files["categories"] = (
            "Name,Number\n" + "".join(f"Group{i:05},{i}\n" for i in range(categories))
        ).encode()
    wid = fixture.freeze_files(files)
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
    physical = ctx.queries.get_source_selection(wid)
    product = next(ds for ds in physical.datasets if ds.name == "products")
    ref, category = (col.stable_key for col in product.columns)
    mappings = []
    links = []
    if shape != "direct":
        if shape == "derived":
            plan, _ = run_local_operation_sync(
                ctx.derived_entities.save_rule,
                wid,
                output_dataset_name="categories",
                source_dataset_id=product.dataset_id,
                source_column_key=category,
                target_model="x.group",
                target_name_field="name",
                external_id_namespace="groups",
                parent_separator=None,
                blank_policy="block",
                expected_parent_version=None,
                actor=fixture.actor,
            )
            (link,) = derived_dataset_links(plan)
            group_id, name = link.derived_dataset_id, link.name_column_key
        else:
            group = next(ds for ds in physical.datasets if ds.name == "categories")
            group_id, name = group.dataset_id, group.columns[0].stable_key
        links.append(
            RelationshipMapping(
                "category_id",
                "many2one",
                (category,),
                resolver=RelationshipResolver(
                    origin=ResolverOrigin.TARGET_THEN_DATASET,
                    model="x.group",
                    dataset_id=group_id,
                    key_mappings=(ReferenceKeyMapping(category, "name"),),
                ),
            )
        )
        mappings.append(
            DatasetMapping(
                group_id,
                "x.group",
                source_identity_column_keys=(name,),
                target_identity=(IdentityComponentMapping((name,), ("name",)),),
            )
        )
    if partial:
        links.append(
            RelationshipMapping(
                "parts",
                "one2many",
                (ref,),
                resolver=RelationshipResolver(
                    origin=ResolverOrigin.DATASET,
                    dataset_id=product.dataset_id,
                ),
            )
        )
    mappings.append(
        DatasetMapping(
            product.dataset_id,
            "x.item",
            source_identity_column_keys=(ref,),
            target_identity=(IdentityComponentMapping((ref,), ("ref",)),),
            relationships=tuple(links),
        )
    )
    ctx.mapping_workspace.save_working_draft(
        wid, datasets=tuple(mappings), expected_version=None, actor=fixture.actor
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
