"""Create the approved fictional BoM dependency and correction example."""

from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    IdentityComponentMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
)
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from tests.support.hierarchy_workflow import PRODUCT_ROWS


def bom_workspace(fixture, *, two_problems=False):
    """Retain both original and reviewed component columns in accepted evidence."""

    lines = (
        "BoM,Sequence,Component,ReviewedComponent,Quantity\n"
        "BA100,10,PX101,PX101,2\n"
        f"BA100,20,PX999-MISSING,PX101,{'invalid' if two_problems else '1'}\n"
        "BA100,30,PX102,PX102,4\n"
        "BB200,10,PX103,PX103,1\n"
        "BB200,20,PX106,PX106,3\n"
    ).encode()
    workspace_id = fixture.freeze_files(
        {
            "templates": b"Reference,Name\nPX104,Pump assembly\nPX105,Machine assembly\n",
            "components": (
                "Reference,Name\n"
                + "\n".join(f"{row[0]},Component {row[0]}" for row in PRODUCT_ROWS)
                + "\n"
            ).encode(),
            "headers": b"Code,Finished\nBA100,PX104\nBB200,PX105\n",
            "lines": lines,
        }
    )

    def field(name, kind="char", relation=None, required=False):
        return SchemaField(name, name, kind, required, False, relation, None, ())

    fixture.schema(
        workspace_id,
        extra_models=(
            SchemaModel(
                "product.product",
                "Component",
                (field("default_code"), field("name", required=True)),
            ),
            SchemaModel(
                "mrp.bom",
                "Bill of Materials",
                (
                    field("code"),
                    field("product_tmpl_id", "many2one", "product.template", True),
                ),
            ),
            SchemaModel(
                "mrp.bom.line",
                "BoM line",
                (
                    field("sequence", "integer"),
                    field("bom_id", "many2one", "mrp.bom", True),
                    field("product_id", "many2one", "product.product", True),
                    field("product_qty", "float"),
                ),
            ),
        ),
        extra_keys=(
            BusinessKeyDefinition(
                "component:reference",
                "product.product",
                ("default_code",),
                status=BusinessKeyStatus.CONFIRMED,
            ),
            BusinessKeyDefinition(
                "bom:code", "mrp.bom", ("code",), status=BusinessKeyStatus.CONFIRMED
            ),
            BusinessKeyDefinition(
                "line:parent-sequence",
                "mrp.bom.line",
                ("sequence",),
                scope_fields=("bom_id",),
                status=BusinessKeyStatus.CONFIRMED,
            ),
        ),
    )
    datasets = {
        dataset.name: dataset
        for dataset in fixture.context.queries.get_source_selection(
            workspace_id
        ).datasets
    }
    columns = {
        name: {column.source_name: column.stable_key for column in dataset.columns}
        for name, dataset in datasets.items()
    }

    def resolver(name):
        return RelationshipResolver(
            origin=ResolverOrigin.DATASET, dataset_id=datasets[name].dataset_id
        )

    def relation(field_name, source, provider):
        return RelationshipMapping(
            field_name,
            "many2one",
            (source,),
            resolver(provider),
            required=True,
            required_on_create=True,
            categorical_policy=CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
        )

    mappings = [
        DatasetMapping(
            datasets[name].dataset_id,
            model,
            source_identity_column_keys=(columns[name]["Reference"],),
            target_identity=(
                IdentityComponentMapping(
                    (columns[name]["Reference"],), ("default_code",)
                ),
            ),
            fields=(
                ScalarFieldMapping("name", columns[name]["Reference"], required=True),
            ),
        )
        for name, model in (
            ("templates", "product.template"),
            ("components", "product.product"),
        )
    ]
    mappings.extend(
        (
            DatasetMapping(
                datasets["headers"].dataset_id,
                "mrp.bom",
                source_identity_column_keys=(columns["headers"]["Code"],),
                target_identity=(
                    IdentityComponentMapping((columns["headers"]["Code"],), ("code",)),
                ),
                relationships=(
                    relation(
                        "product_tmpl_id", columns["headers"]["Finished"], "templates"
                    ),
                ),
            ),
            DatasetMapping(
                datasets["lines"].dataset_id,
                "mrp.bom.line",
                source_identity_column_keys=(
                    columns["lines"]["BoM"],
                    columns["lines"]["Sequence"],
                ),
                target_identity=(
                    IdentityComponentMapping(
                        (columns["lines"]["Sequence"],), ("sequence",), "integer"
                    ),
                ),
                target_scope=(
                    IdentityComponentMapping(
                        (columns["lines"]["BoM"],),
                        ("bom_id",),
                        resolver=resolver("headers"),
                    ),
                ),
                fields=(
                    ScalarFieldMapping(
                        "product_qty",
                        columns["lines"]["Quantity"],
                        value_type="decimal",
                    ),
                ),
                relationships=(
                    relation("product_id", columns["lines"]["Component"], "components"),
                ),
            ),
        )
    )
    return workspace_id, fixture.submit(workspace_id, mappings), columns
