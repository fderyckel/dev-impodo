from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import unittest

from impodo.domain.mapping.artifacts import MappingRevision
from impodo.domain.mapping.contracts import (
    ConstantBusinessReference,
    ConstantReferenceComponent,
    DatasetMapping,
    MappingDefinition,
    RelationshipMapping,
    RelationshipResolver,
    RelationshipValueSource,
    ResolverOrigin,
    UnsupportedMappingContractError,
)


HASH = "sha256:" + "1" * 64


class MappingContractCompatibilityTests(unittest.TestCase):
    def test_v15_constant_relationship_round_trips_and_changes_hash(self) -> None:
        relationship = RelationshipMapping(
            target_field="product_uom_id",
            kind="many2one",
            source_column_keys=(),
            resolver=RelationshipResolver(
                origin=ResolverOrigin.TARGET_CATALOG,
                model="uom.uom",
            ),
            value_source=RelationshipValueSource.CONSTANT_EXISTING,
            constant_reference=ConstantBusinessReference(
                key_values=(ConstantReferenceComponent("name", "PCE"),),
            ),
        )
        definition = MappingDefinition(
            mapping_id="mapping:boms",
            source_selection_hash=HASH,
            schema_hash=HASH,
            datasets=(
                DatasetMapping(
                    dataset_id="dataset:boms",
                    target_model="mrp.bom",
                    relationships=(relationship,),
                ),
            ),
        )

        restored = MappingDefinition.from_json(definition.to_json())

        self.assertEqual(restored, definition)
        payload = definition.to_dict()
        relation = payload["datasets"][0]["relationships"][0]
        self.assertEqual(relation["value_source"], "constant_existing")
        self.assertEqual(
            relation["constant_reference"]["key_values"],
            [{"target_field": "name", "value": "PCE"}],
        )
        changed = replace(
            relationship,
            constant_reference=ConstantBusinessReference(
                key_values=(ConstantReferenceComponent("name", "Unit"),),
            ),
        )
        self.assertNotEqual(
            definition.content_hash,
            replace(
                definition,
                datasets=(
                    replace(definition.datasets[0], relationships=(changed,)),
                ),
            ).content_hash,
        )

    def test_v14_relationship_decodes_as_source_without_accepting_v15_fields(self) -> None:
        definition = MappingDefinition(
            mapping_id="mapping:contacts",
            source_selection_hash=HASH,
            schema_hash=HASH,
            datasets=(
                DatasetMapping(
                    dataset_id="dataset:contacts",
                    target_model="res.partner",
                    relationships=(
                        RelationshipMapping(
                            target_field="company_id",
                            kind="many2one",
                            source_column_keys=("company_code",),
                            resolver=RelationshipResolver(
                                origin=ResolverOrigin.DATASET,
                                dataset_id="dataset:companies",
                            ),
                        ),
                    ),
                ),
            ),
            contract_version=14,
        )
        payload = definition.to_dict()
        relation = payload["datasets"][0]["relationships"][0]
        self.assertNotIn("value_source", relation)
        self.assertNotIn("constant_reference", relation)
        restored = MappingDefinition.from_dict(payload)
        self.assertEqual(
            restored.datasets[0].relationships[0].value_source,
            RelationshipValueSource.SOURCE,
        )

        relation["value_source"] = "source"
        with self.assertRaisesRegex(ValueError, "Relationship mapping fields"):
            MappingDefinition.from_dict(payload)

    def test_constant_relationship_shapes_fail_closed(self) -> None:
        resolver = RelationshipResolver(
            origin=ResolverOrigin.TARGET_CATALOG,
            model="uom.uom",
        )
        reference = ConstantBusinessReference(
            key_values=(ConstantReferenceComponent("name", "PCE"),),
        )
        invalid = (
            {"kind": "many2many"},
            {"source_column_keys": ("uom",)},
            {"constant_reference": None},
            {
                "resolver": RelationshipResolver(
                    origin=ResolverOrigin.DATASET,
                    dataset_id="dataset:uom",
                )
            },
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                RelationshipMapping(
                    target_field="product_uom_id",
                    kind=str(changes.get("kind", "many2one")),
                    source_column_keys=tuple(
                        changes.get("source_column_keys", ())
                    ),
                    resolver=changes.get("resolver", resolver),
                    value_source=RelationshipValueSource.CONSTANT_EXISTING,
                    constant_reference=changes.get(
                        "constant_reference", reference
                    ),
                )

    def test_v12_revision_round_trips_with_its_exact_layout_and_hash(self) -> None:
        definition = MappingDefinition(
            mapping_id="mapping:contacts",
            source_selection_hash=HASH,
            schema_hash=HASH,
            datasets=(
                DatasetMapping(
                    dataset_id="dataset:contacts",
                    target_model="res.partner",
                    relationships=(
                        RelationshipMapping(
                            target_field="company_id",
                            kind="many2one",
                            source_column_keys=("company_code",),
                            resolver=RelationshipResolver(
                                origin=ResolverOrigin.DATASET,
                                dataset_id="dataset:companies",
                            ),
                        ),
                    ),
                ),
            ),
            contract_version=12,
        )
        revision = MappingRevision(
            mapping_id=definition.mapping_id,
            version=4,
            parent_version=3,
            definition=definition,
            created_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
            created_by="Data manager",
        )
        original_json = revision.to_json()
        original_payload = json.loads(original_json)
        resolver = original_payload["definition"]["datasets"][0][
            "relationships"
        ][0]["resolver"]

        self.assertNotIn("dataset_projection_field", resolver)
        restored = MappingRevision.from_json(original_json)
        self.assertEqual(restored.definition.contract_version, 12)
        self.assertEqual(restored.definition.content_hash, definition.content_hash)
        self.assertEqual(restored.to_json(), original_json)

        successor = replace(restored.definition, contract_version=13)
        successor_resolver = successor.to_dict()["datasets"][0]["relationships"][
            0
        ]["resolver"]
        self.assertIn("dataset_projection_field", successor_resolver)
        self.assertIsNone(successor_resolver["dataset_projection_field"])
        self.assertNotEqual(successor.content_hash, definition.content_hash)
        self.assertEqual(restored.to_json(), original_json)

    def test_versions_outside_the_workspace_generation_are_blocked(self) -> None:
        payload = MappingDefinition(
            mapping_id="mapping:contacts",
            source_selection_hash=HASH,
            schema_hash=HASH,
            datasets=(),
        ).to_dict()
        payload["contract_version"] = 11

        with self.assertRaises(UnsupportedMappingContractError) as captured:
            MappingDefinition.from_dict(payload)

        self.assertEqual(captured.exception.contract_version, 11)


if __name__ == "__main__":
    unittest.main()
