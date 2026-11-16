"""Portable prepared references retain column identity across fresh deliveries."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from impodo.application.recipe_application_compilation import RecipeApplicationCompiler
from impodo.application.recipe_compilation_service import RecipeCompiler
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.mapping.reference_preparation import (
    ReferencePart,
    ReferencePreparation,
    prepare_reference_value,
)
from impodo.domain.recipe.models import RecipeIntegrityError
from impodo.domain.recipe_envelope import (
    CURRENT_RECIPE_CONTRACT_VERSIONS,
    SEMANTIC_FIELDS,
    validate_recipe_envelope,
)
from impodo.domain.serialization import content_hash
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaModel,
    SourceSelection,
)
from tests.domain.recipe.test_representative_shapes import (
    _column,
    _dataset,
    _field,
    _key,
    _publish,
)


def _envelope(mapping, *, mapping_version=4, source_version=1):
    recipe = {name: {} for name in SEMANTIC_FIELDS}
    recipe["mapping"] = mapping
    recipe["contract_versions"] = {
        **CURRENT_RECIPE_CONTRACT_VERSIONS,
        "mapping_recipe": mapping_version,
        "source_preparation_recipe": source_version,
    }
    payload = {
        "recipe_contract_version": 2,
        "recipe": recipe,
        "semantic_hash": content_hash(recipe),
        "compatibility_hints": {},
        "provenance": {},
    }
    payload["payload_hash"] = content_hash(payload)
    return json.dumps(payload).encode()


class ReferencePreparationRecipeTests(TestCase):
    def test_publication_records_preparation_version_and_complete_source_shape(self):
        source = _dataset(
            "Customers",
            (
                ("reference", "string"),
                ("country_first", "string"),
                ("country_last", "string"),
            ),
            "a",
        )
        selection = SourceSelection(
            str(uuid4()),
            1,
            str(uuid4()),
            datetime.now(UTC),
            "Manager",
            (source,),
            "sha256:" + "a" * 64,
        )
        relation = RelationshipMapping(
            "country_id",
            "many2one",
            (_column(source, "country_first"),),
            RelationshipResolver(
                ResolverOrigin.TARGET_CATALOG,
                model="res.country",
                key_mappings=(
                    ReferenceKeyMapping(
                        _column(source, "country_first"),
                        "code",
                        ReferencePreparation(
                            formula="concat(value, column_3)",
                            bindings=(("column_3", _column(source, "country_last")),),
                        ),
                    ),
                ),
            ),
        )
        mapping = DatasetMapping(
            source.dataset_id,
            "res.partner",
            source_identity_column_keys=(_column(source, "reference"),),
            target_identity=(
                IdentityComponentMapping(
                    (_column(source, "reference"),),
                    ("ref",),
                ),
            ),
            relationships=(relation,),
        )
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):

                    def catalog(**values):
                        return replace(
                            OdooSchemaCatalog(**values),
                            odoo_version=f"{major}.0",
                            connection_mode=mode,
                        )

                    with patch(
                        "tests.domain.recipe.test_representative_shapes.OdooSchemaCatalog",
                        side_effect=catalog,
                    ):
                        recipe = _publish(
                            base_selection=selection,
                            mapping_selection=selection,
                            mappings=(mapping,),
                            models=(
                                SchemaModel(
                                    "res.partner",
                                    "Contacts",
                                    (
                                        _field("ref"),
                                        _field(
                                            "country_id",
                                            "many2one",
                                            relation="res.country",
                                        ),
                                    ),
                                ),
                                SchemaModel(
                                    "res.country",
                                    "Countries",
                                    (
                                        _field("code", required=True),
                                        _field("name", required=True),
                                    ),
                                ),
                            ),
                            business_keys=(
                                _key("res.partner", "ref"),
                                _key("res.country", "code"),
                            ),
                        )
                    self.assertEqual(recipe["contract_versions"]["mapping_recipe"], 4)
                    self.assertEqual(
                        {
                            item["source_name"]
                            for item in recipe["source_shape"]["datasets"][0]["columns"]
                        },
                        {"reference", "country_first", "country_last"},
                    )

    def fixture(self):
        source = _dataset(
            "Stock",
            tuple(
                (name, "string")
                for name in (
                    "row",
                    "product",
                    "site",
                    "warehouse",
                    "bin",
                    "owner",
                    "suffix",
                    "company",
                )
            ),
            "a",
        )

        def resolver(model, key, scope=()):
            return RelationshipResolver(
                ResolverOrigin.TARGET_CATALOG,
                model=model,
                key_mappings=(key,),
                scope_mappings=scope,
            )

        mapping = DatasetMapping(
            source.dataset_id,
            "stock.quant",
            source_identity_column_keys=(_column(source, "row"),),
            target_identity=(
                IdentityComponentMapping(
                    (_column(source, "product"),),
                    ("product_id",),
                    resolver=resolver(
                        "product.product",
                        ReferenceKeyMapping(
                            _column(source, "product"),
                            "default_code",
                            ReferencePreparation(formula='concat("P-", value)'),
                        ),
                    ),
                ),
            ),
            target_scope=(
                IdentityComponentMapping(
                    (_column(source, "site"),),
                    ("location_id",),
                    resolver=resolver(
                        "stock.location",
                        ReferenceKeyMapping(
                            _column(source, "site"),
                            "complete_name",
                            ReferencePreparation(
                                formula='concat(value, "/Stock/", column_4, "/", column_5)',
                                bindings=(
                                    ("column_4", _column(source, "warehouse")),
                                    ("column_5", _column(source, "bin")),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            relationships=(
                RelationshipMapping(
                    "owner_id",
                    "many2one",
                    (_column(source, "owner"),),
                    resolver(
                        "res.partner",
                        ReferenceKeyMapping(
                            _column(source, "owner"),
                            "ref",
                            ReferencePreparation(
                                parts=(
                                    ReferencePart(
                                        source_column_key=_column(source, "owner")
                                    ),
                                    ReferencePart(literal="-"),
                                    ReferencePart(
                                        source_column_key=_column(source, "suffix")
                                    ),
                                )
                            ),
                        ),
                        (
                            ReferenceKeyMapping(
                                _column(source, "owner"),
                                "name",
                                ReferencePreparation(
                                    formula="concat(column_8)",
                                    bindings=(
                                        ("column_8", _column(source, "company")),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
        compiler = object.__new__(RecipeCompiler)
        _sources, datasets, columns = compiler._source_identity((source,))
        payload = compiler._mapping((mapping,), datasets, columns, {})
        return source, mapping, compiler, datasets, columns, payload

    def test_formula_and_parts_rebind_after_new_keys_and_column_reordering(self):
        source, _mapping, _compiler, _datasets, _columns, payload = self.fixture()
        fresh = replace(
            source,
            dataset_id="new-stock",
            columns=tuple(
                replace(column, ordinal=index, stable_key=f"new:{column.source_name}")
                for index, column in enumerate(reversed(source.columns), start=1)
            ),
        )
        selection = SourceSelection(
            str(uuid4()),
            1,
            str(uuid4()),
            datetime.now(UTC),
            "Manager",
            (fresh,),
            "sha256:" + "b" * 64,
        )
        _sources, datasets, columns = RecipeCompiler._source_identity((fresh,))
        bindings = {logical: physical for physical, logical in datasets.items()}
        bindings.update({logical: physical[1] for physical, logical in columns.items()})
        recipe = {"mapping": payload, "control_definitions": {}}
        actual = RecipeApplicationCompiler()._mapping_datasets(
            recipe,
            bindings,
            selection,
            None,
            None,
        )[0]
        product = actual.target_identity[0].resolver.key_mappings[0]
        location = actual.target_scope[0].resolver.key_mappings[0]
        owner = actual.relationships[0].resolver.key_mappings[0]
        company = actual.relationships[0].resolver.scope_mappings[0]
        self.assertEqual(
            prepare_reference_value(product, {"new:product": "0007"}), "P-0007"
        )
        self.assertEqual(
            prepare_reference_value(
                location,
                {
                    "new:site": "0WIL",
                    "new:warehouse": "0100",
                    "new:bin": "100",
                },
            ),
            "0WIL/Stock/0100/100",
        )
        self.assertEqual(
            prepare_reference_value(
                owner,
                {
                    "new:owner": "0001",
                    "new:suffix": "002",
                },
            ),
            "0001-002",
        )
        self.assertEqual(
            prepare_reference_value(company, {"new:company": "0003"}), "0003"
        )
        self.assertEqual(
            location.preparation.bindings,
            (("column_4", "new:warehouse"), ("column_5", "new:bin")),
        )
        self.assertNotIn("physical:", str(payload))
        self.assertNotIn(source.dataset_id, str(payload))

    def test_every_formula_and_part_dependency_is_required_in_source_shape(self):
        source, mapping, compiler, datasets, columns, _payload = self.fixture()
        used = compiler._used_columns((mapping,), datasets, columns)
        self.assertEqual(
            {key for dataset, key in used if dataset == source.dataset_id},
            {column.stable_key for column in source.columns},
        )

    def test_existing_direct_reference_payload_does_not_gain_default_fields(self):
        source, _mapping, compiler, _datasets, columns, _payload = self.fixture()
        key = ReferenceKeyMapping(_column(source, "product"), "default_code")
        self.assertEqual(
            compiler._reference_key(key, source.dataset_id, columns),
            {
                "source_column_id": "column:stock.product",
                "target_field": "default_code",
            },
        )

    def test_envelope_requires_prepared_reference_contract_and_valid_rule(self):
        _source, _mapping, _compiler, _datasets, _columns, payload = self.fixture()
        for source_version in (1, 2):
            validate_recipe_envelope(_envelope(payload, source_version=source_version))
        with self.assertRaisesRegex(RecipeIntegrityError, "contract v4"):
            validate_recipe_envelope(_envelope(payload, mapping_version=3))
        for malformed in (
            None,
            {"formula": "value", "bindings": [], "parts": [], "ignored": True},
            {"formula": "unknown(value)", "bindings": [], "parts": []},
            {"formula": 12, "bindings": [], "parts": []},
            {"formula": "column_9", "bindings": [], "parts": []},
            {
                "formula": "value",
                "bindings": [
                    {"name": "column_9", "source_column_id": "column:stock.bin"},
                ],
                "parts": [],
            },
            {
                "formula": "",
                "bindings": [],
                "parts": [
                    {"source_column_id": "column:stock.owner", "literal": "bad"},
                ],
            },
        ):
            with self.subTest(malformed=malformed):
                invalid = deepcopy(payload)
                invalid["datasets"][0]["identity"][0]["resolver"][
                    "target_key_mappings"
                ][0]["preparation"] = malformed
                with self.assertRaisesRegex(RecipeIntegrityError, "rule is invalid"):
                    validate_recipe_envelope(_envelope(invalid))
