"""Keep bounded field metadata reuse invisible to normalization evidence."""

from __future__ import annotations

import unittest
from dataclasses import replace
from hashlib import sha256
from unittest.mock import patch

from impodo.application.workspace.preparation import bounded_normalization
from impodo.application.workspace.preparation.bounded_normalization import (
    _BoundedNormalizationEffects,
)
from impodo.domain.mapping.contracts import DatasetMapping, ScalarFieldMapping
from impodo.domain.preparation.normalization import (
    NormalizationCandidate,
    NormalizationPolicyError,
    _hash,
    evaluate_normalization,
    normalization_change_language,
)
from impodo.domain.preparation.quality import default_quality_ruleset, evaluate_quality
from impodo.domain.recipe.value_rules import ScalarTransformPolicy
from impodo.domain.shared.models import canonical_json_bytes
from impodo.domain.staging.transformation_impact import TransformationImpactRow
from impodo.domain.workspace.workbench import DataClassification
from tests.domain.preparation.test_quality import (
    MAPPING_HASH,
    SCHEMA_HASH,
    _canonical_row,
    _staging,
    _workspace_state,
)


def _mappings() -> dict[str, DatasetMapping]:
    return {
        "contacts": DatasetMapping(
            dataset_id="dataset:contacts",
            target_model="res.partner",
            fields=(
                ScalarFieldMapping(
                    target_field="name",
                    transform=ScalarTransformPolicy(trim=True),
                ),
                ScalarFieldMapping(
                    target_field="phone",
                    transform=ScalarTransformPolicy(case_mode="uppercase"),
                ),
            ),
        ),
        "companies": DatasetMapping(
            dataset_id="dataset:companies",
            target_model="res.partner",
            fields=(
                ScalarFieldMapping(
                    target_field="name",
                    transform=ScalarTransformPolicy(case_mode="uppercase"),
                ),
            ),
        ),
    }


def _impact(
    source_row: int = 2,
    *,
    dataset: str = "contacts",
    target_field: str = "name",
    raw_value: str = " Alice ",
    proposed_value: str = "Alice",
    outcome: str = "changed",
) -> TransformationImpactRow:
    return TransformationImpactRow(
        dataset=dataset,
        source_row=source_row,
        source_column=f"Column {target_field}",
        target_field=target_field,
        raw_value=raw_value,
        proposed_value=proposed_value,
        rules="Display wording does not define the structural policy",
        outcome=outcome,
    )


def _row_id(token: str) -> str:
    return "sha256:" + token * 64


class NormalizationMetadataTests(unittest.TestCase):
    def test_reused_metadata_preserves_materialized_evidence_and_restricted_values(
        self,
    ) -> None:
        mappings = _mappings()
        rows = (
            _canonical_row("a", 2),
            _canonical_row(
                "b", 3, source_identity=("C002",), target_identity=("C002",)
            ),
            _canonical_row(
                "c", 4, dataset="companies", physical_dataset_id="dataset:companies"
            ),
        )
        impacts = (
            _impact(),
            _impact(3, raw_value=" Bob ", proposed_value="Bob"),
            _impact(target_field="phone", raw_value="ab", proposed_value="AB"),
            _impact(4, dataset="companies", raw_value="acme", proposed_value="ACME"),
        )
        row_ids = {(row.dataset, row.source_row): row.row_id for row in rows}
        for classification in (
            DataClassification.INTERNAL,
            DataClassification.RESTRICTED,
        ):
            with self.subTest(classification=classification):
                workspace = replace(
                    _workspace_state(), data_classification=classification
                )
                staging = _staging(workspace.workspace_id, rows)
                ruleset = default_quality_ruleset(
                    workspace_id=workspace.workspace_id,
                    mapping_hash=MAPPING_HASH,
                    schema_hash=SCHEMA_HASH,
                    datasets=("contacts", "companies"),
                )
                quality = evaluate_quality(
                    workspace_state=workspace,
                    staging=staging,
                    physical_rows={
                        "dataset:contacts": (2, 3),
                        "dataset:companies": (4,),
                    },
                    ruleset=ruleset,
                )
                expected = evaluate_normalization(
                    workspace_state=workspace,
                    staging=staging,
                    quality=quality,
                    mappings=mappings,
                    candidates=(
                        NormalizationCandidate(
                            dataset=impact.dataset,
                            source_row=impact.source_row,
                            source_label=impact.source_column,
                            target_field=impact.target_field,
                            raw_display=impact.raw_value,
                            proposed_display=impact.proposed_value,
                            rules=impact.rules,
                            outcome=impact.outcome,
                        )
                        for impact in impacts
                    ),
                )
                factory = _BoundedNormalizationEffects(
                    workspace_state=workspace,
                    mapping_hash=MAPPING_HASH,
                    mappings=mappings,
                    eligible_row_ids=quality.eligible_row_ids,
                )
                groups = {group.group_id: group for group in expected.groups}
                effects = []
                for impact in impacts:
                    built = factory._effect(
                        impact, row_ids[(impact.dataset, impact.source_row)]
                    )
                    assert built is not None
                    effect, metadata = built
                    effects.append(effect)
                    group = groups[effect.group_id]
                    self.assertEqual(
                        metadata,
                        {
                            key: getattr(group, key)
                            for key in (
                                "rule_id",
                                "kind",
                                "outcome",
                                "dataset",
                                "target_field",
                                "name",
                                "explanation",
                                "owner_label",
                            )
                        },
                    )
                ordered = tuple(sorted(effects, key=lambda effect: effect.effect_id))
                self.assertEqual(ordered, expected.effects)
                self.assertEqual(
                    replace(expected, effects=ordered).content_hash,
                    expected.content_hash,
                )
                self.assertEqual(
                    sha256(
                        canonical_json_bytes(
                            [effect.to_portable_dict() for effect in ordered]
                        )
                    ).hexdigest(),
                    sha256(
                        canonical_json_bytes(
                            [effect.to_portable_dict() for effect in expected.effects]
                        )
                    ).hexdigest(),
                )
                if classification is DataClassification.RESTRICTED:
                    self.assertTrue(
                        all(
                            effect.before
                            == effect.after
                            == "Hidden for restricted data"
                            for effect in ordered
                        )
                    )

    def test_returned_metadata_mutation_does_not_change_later_effects(self) -> None:
        factory = _BoundedNormalizationEffects(
            workspace_state=_workspace_state(),
            mapping_hash=MAPPING_HASH,
            mappings=_mappings(),
            eligible_row_ids=frozenset({_row_id("a")}),
        )
        first = factory._effect(_impact(), _row_id("a"))
        assert first is not None
        first_effect, metadata = first
        original = metadata.copy()
        metadata.clear()
        metadata["name"] = "Caller replacement"
        second = factory._effect(
            _impact(3, raw_value=" Bob ", proposed_value="Bob"), _row_id("b")
        )
        assert second is not None
        second_effect, second_metadata = second
        self.assertIsNot(metadata, second_metadata)
        self.assertEqual(second_metadata, original)
        self.assertEqual(first_effect.group_id, second_effect.group_id)
        self.assertNotEqual(first_effect.effect_id, second_effect.effect_id)
        self.assertEqual(
            (first_effect.before, second_effect.before), (" Alice ", " Bob ")
        )
        self.assertEqual((first_effect.eligible, second_effect.eligible), (True, False))
        explicitly_eligible = factory._effect(_impact(3), _row_id("b"), eligible=True)
        explicitly_excluded = factory._effect(_impact(), _row_id("a"), eligible=False)
        assert explicitly_eligible is not None and explicitly_excluded is not None
        self.assertTrue(explicitly_eligible[0].eligible)
        self.assertFalse(explicitly_excluded[0].eligible)

    def test_invalid_unknown_field_skips_and_valid_unknown_field_fails_closed(
        self,
    ) -> None:
        factory = _BoundedNormalizationEffects(
            workspace_state=_workspace_state(),
            mapping_hash=MAPPING_HASH,
            mappings=_mappings(),
            eligible_row_ids=frozenset(),
        )
        with patch.object(bounded_normalization, "_hash", wraps=_hash) as hashes:
            self.assertIsNone(
                factory._effect(
                    _impact(0, target_field="unknown", outcome="invalid"),
                    "invalid row ID",
                )
            )
            for _attempt in range(2):
                with self.assertRaises(NormalizationPolicyError):
                    factory._effect(_impact(target_field="unknown"), _row_id("a"))
            hashes.assert_not_called()

    def test_each_factory_binds_its_own_mapping_hash(self) -> None:
        effects = []
        metadata = []
        for mapping_hash in (MAPPING_HASH, "sha256:" + "f" * 64):
            factory = _BoundedNormalizationEffects(
                workspace_state=_workspace_state(),
                mapping_hash=mapping_hash,
                mappings=_mappings(),
                eligible_row_ids=frozenset({_row_id("a")}),
            )
            built = factory._effect(_impact(), _row_id("a"))
            assert built is not None
            effects.append(built[0])
            metadata.append(built[1])
        self.assertNotEqual(metadata[0]["rule_id"], metadata[1]["rule_id"])
        self.assertNotEqual(effects[0].group_id, effects[1].group_id)
        self.assertNotEqual(effects[0].effect_id, effects[1].effect_id)

    def test_policy_hashing_and_language_scale_with_fields_while_effects_scale_with_rows(
        self,
    ) -> None:
        factory = _BoundedNormalizationEffects(
            workspace_state=_workspace_state(),
            mapping_hash=MAPPING_HASH,
            mappings=_mappings(),
            eligible_row_ids=frozenset(),
        )
        impacts = (
            _impact(),
            _impact(3),
            _impact(target_field="phone"),
            _impact(3, target_field="phone"),
            _impact(dataset="companies"),
            _impact(3, dataset="companies"),
        )
        with (
            patch.object(bounded_normalization, "_hash", wraps=_hash) as hashes,
            patch.object(
                bounded_normalization,
                "normalization_change_language",
                wraps=normalization_change_language,
            ) as language,
        ):
            for impact in impacts:
                self.assertIsNotNone(factory._effect(impact, _row_id("a")))
            self.assertEqual(hashes.call_count, len(impacts) + 2 * 3)
            self.assertEqual(language.call_count, 3)


if __name__ == "__main__":
    unittest.main()
