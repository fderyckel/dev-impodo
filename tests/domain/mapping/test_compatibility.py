from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import unittest

from impodo.domain.mapping.artifacts import MappingRevision
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    MappingDefinition,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    UnsupportedMappingContractError,
)


HASH = "sha256:" + "1" * 64


class MappingContractCompatibilityTests(unittest.TestCase):
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
