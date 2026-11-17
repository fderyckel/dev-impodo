"""Independent field rules preserve record matching and block duplicate values."""

from dataclasses import replace
from decimal import Decimal, localcontext
import unittest

from impodo.domain.compiler import compile_profile_document
from impodo.domain.compiler.contracts import CompiledMigrationPlan
from impodo.domain.execution.planner import plan_preflight_requirements
from impodo.domain.mapping.contracts import (
    MappingDefinition,
    ScalarFieldMapping,
    ScalarValueSource,
)
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.preparation.field_uniqueness import (
    duplicate_field_values,
    unique_value_key,
)
from impodo.domain.preparation.preflight import PreflightEngine
from impodo.domain.preparation.quality import (
    QualityOutcomePolicy,
    default_quality_ruleset,
    evaluate_quality,
)
from impodo.domain.preparation.source import (
    PreparedBundle,
    SourceRow,
    SourceTable,
    prepare_source_tables,
)
from impodo.domain.preparation.staging_contracts import StagingDisposition
from impodo.domain.recipe.profile import (
    DatasetSpec,
    FieldSpec,
    IdentityComponent,
    ProfileDocument,
    SourceIdentitySpec,
    SourceSpec,
    TargetIdentitySpec,
    TargetSpec,
)
from impodo.domain.shared.models import (
    Classification,
    FieldMetadata,
    ModelMetadata,
    PreparedRecord,
    TargetFingerprint,
    TargetRecord,
)
from tests.domain.preparation.test_quality import (
    QUALITY_DEFINITION,
    _canonical_row,
    _staging,
    _workspace_state,
)

HASH = "sha256:" + "1" * 64
MODEL = "res.partner"


def plan():
    return CompiledMigrationPlan(
        plan_id="unique_vat",
        origin="profile_document",
        origin_hash=HASH,
        contract_version=6,
        compiler_version=7,
        datasets=(
            DatasetSpec(
                name="customers",
                source=SourceSpec(file="customers.csv"),
                source_identity=SourceIdentitySpec(fields=("ref",)),
                target=TargetSpec(model=MODEL),
                target_identity=TargetIdentitySpec(
                    components=(
                        IdentityComponent(
                            source_fields=("ref",),
                            target_fields=("ref",),
                            type="string",
                        ),
                    )
                ),
                fields={
                    "vat": FieldSpec(source="vat", type="string", must_be_unique=True)
                },
            ),
        ),
    )


def record(ref, vat, row=2, dataset="customers"):
    return PreparedRecord(
        dataset=dataset,
        source_row=row,
        target_model=MODEL,
        source_identity=(ref,),
        target_identity=(ref,),
        target_scope=(),
        scalar_values={"vat": vat},
        references={},
    )


def snapshots(major, mode, targets):
    fingerprint = TargetFingerprint(
        HASH, mode, "fictional", f"{major}.0", "2026-11-17T00:00:00Z"
    )
    return (
        MetadataSnapshot(
            fingerprint,
            {
                MODEL: ModelMetadata(
                    MODEL,
                    "Customers",
                    {
                        "ref": FieldMetadata("ref", "char"),
                        "vat": FieldMetadata("vat", "char"),
                    },
                )
            },
        ),
        RecordSnapshot(
            fingerprint,
            {
                MODEL: tuple(
                    TargetRecord(MODEL, index, {"ref": ref, "vat": vat})
                    for index, (ref, vat) in enumerate(targets, 1)
                )
            },
            {MODEL: ("ref", "vat")},
        ),
    )


class FieldUniquenessTests(unittest.TestCase):
    def test_unselected_rule_does_not_hash_or_scan_unrelated_values(self):
        from unittest.mock import patch

        dataset = plan().datasets[0]
        dataset = dataset.model_copy(
            update={
                "fields": {
                    "vat": dataset.fields["vat"].model_copy(
                        update={"must_be_unique": False}
                    )
                }
            }
        )
        compiled = plan().model_copy(update={"datasets": (dataset,)})
        with patch(
            "impodo.domain.preparation.unique_targets.unique_value_key",
            side_effect=AssertionError("No uniqueness scan was selected"),
        ):
            result = PreflightEngine().run(
                compiled,
                PreparedBundle((record("C1", "VAT-1"),), {}, ()),
                *snapshots(20, "REMOTE", ()),
            )
        self.assertEqual(result.decisions[0].classification, Classification.CREATE)

    def test_nonwriting_mappings_reject_a_rule_outside_their_read_scope(self):
        from impodo.domain.mapping.contracts import DatasetMapping, MappingTargetMode

        for mode, message in (
            (MappingTargetMode.ODOO_PINNED_UPDATE, "protected Odoo corrections"),
            (MappingTargetMode.REFERENCE, "creates or updates"),
        ):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(ValueError, message):
                    DatasetMapping(
                        "captured",
                        MODEL,
                        mode=mode,
                        fields=(
                            ScalarFieldMapping(
                                "vat", "column:vat", must_be_unique=True
                            ),
                        ),
                    )
                payload = plan().datasets[0].model_dump(mode="json")
                payload["target"]["mode"] = mode.value
                with self.assertRaisesRegex(ValueError, message):
                    DatasetSpec.model_validate(payload)

    def test_transformations_are_checked_after_preparation_and_incomplete_reads_fail_closed(
        self,
    ):
        from pathlib import PurePath
        from impodo.domain.recipe.profile import NormalizationSpec

        dataset = plan().datasets[0]
        compiled = plan().model_copy(
            update={
                "datasets": (
                    dataset.model_copy(
                        update={
                            "fields": {
                                "vat": dataset.fields["vat"].model_copy(
                                    update={"normalize": NormalizationSpec(trim=True)}
                                ),
                            }
                        }
                    ),
                )
            }
        )
        table = SourceTable(
            "customers",
            PurePath("customers.csv"),
            ("ref", "vat"),
            (
                SourceRow(2, {"ref": "C1", "vat": " VAT-1 "}),
                SourceRow(3, {"ref": "C2", "vat": "VAT-1"}),
            ),
            HASH,
        )
        prepared = prepare_source_tables(
            compiled, (table,), source_hashes={"customers": HASH}
        )
        self.assertEqual(
            [row.scalar_values["vat"] for row in prepared.records], ["VAT-1", "VAT-1"]
        )
        result = PreflightEngine().run(compiled, prepared, *snapshots(20, "REMOTE", ()))
        self.assertTrue(
            all(
                row.classification is Classification.BLOCKED for row in result.decisions
            )
        )
        self.assertEqual(table.rows[0].values["vat"], " VAT-1 ")
        metadata, captured = snapshots(20, "REMOTE", (("OTHER", "VAT-1"),))
        with self.assertRaisesRegex(ValueError, "incomplete"):
            PreflightEngine().run(
                plan(),
                PreparedBundle((record("C1", "VAT-1"),), {}, ()),
                metadata,
                replace(captured, complete=False),
            )
        missing = replace(
            captured, records={MODEL: (TargetRecord(MODEL, 1, {"ref": "OTHER"}),)}
        )
        with self.assertRaisesRegex(ValueError, "uniqueness evidence.*incomplete"):
            PreflightEngine().run(
                plan(),
                PreparedBundle((record("C1", "VAT-1"),), {}, ()),
                metadata,
                missing,
            )

    def test_both_odoo_majors_and_access_modes_allow_only_the_same_record(self):
        cases = (
            ("NEW", "VAT-1", (), Classification.CREATE),
            ("C001", "VAT-1", (("C001", "VAT-1"),), Classification.UNCHANGED),
            ("C001", "VAT-2", (("C001", "VAT-1"),), Classification.UPDATE),
            ("NEW", "VAT-1", (("OTHER", "VAT-1"),), Classification.BLOCKED),
            (
                "C001",
                "VAT-1",
                (("C001", "VAT-1"), ("OTHER", "VAT-1")),
                Classification.BLOCKED,
            ),
            (
                "C001",
                "VAT-2",
                (("C001", "VAT-1"), ("OTHER", "VAT-2")),
                Classification.BLOCKED,
            ),
        )
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                for ref, vat, targets, expected in cases:
                    with self.subTest(major=major, mode=mode, ref=ref, targets=targets):
                        result = PreflightEngine().run(
                            plan(),
                            PreparedBundle((record(ref, vat),), {}, ()),
                            *snapshots(major, mode, targets),
                        )
                        decision = result.decisions[0]
                        self.assertEqual(decision.classification, expected)
                        if expected is Classification.BLOCKED:
                            issue = next(
                                item
                                for item in decision.issues
                                if item.code == "TARGET_FIELD_NOT_UNIQUE"
                            )
                            self.assertEqual(issue.field, "vat")
                            self.assertIn("OTHER", issue.message)
                            self.assertIn(vat, issue.message)
                            self.assertIn(issue, result.issues)

    def test_preflight_blocks_incoming_duplicates_across_tables_without_changing_identity(
        self,
    ):
        first = plan().datasets[0]
        compiled = plan().model_copy(
            update={
                "datasets": (
                    first,
                    first.model_copy(update={"name": "suppliers"}),
                )
            }
        )
        result = PreflightEngine().run(
            compiled,
            PreparedBundle(
                (record("C1", "VAT-1"), record("S1", "VAT-1", dataset="suppliers")),
                {},
                (),
            ),
            *snapshots(20, "REMOTE", ()),
        )
        self.assertEqual(
            [row.classification for row in result.decisions],
            [Classification.BLOCKED] * 2,
        )
        self.assertEqual(
            {row.business_identity for row in result.decisions}, {("C1",), ("S1",)}
        )
        self.assertTrue(
            all(
                issue.field == "vat" for row in result.decisions for issue in row.issues
            )
        )

    def test_blank_values_do_not_generate_conflicts_or_value_lookup_requests(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                prepared = (record("C1", value), record("C2", value, 3))
                requirements = plan_preflight_requirements(plan(), prepared)
                self.assertFalse(
                    any(
                        domain[0] == "vat"
                        for request in requirements.record_requests
                        for domain in request.domain
                        if isinstance(domain, list)
                    )
                )
                result = PreflightEngine().run(
                    plan(),
                    PreparedBundle(prepared, {}, ()),
                    *snapshots(19, "LOCAL", ()),
                )
                self.assertTrue(
                    all(
                        row.classification is Classification.CREATE
                        for row in result.decisions
                    )
                )

    def test_read_requests_are_selective_complete_and_chunked_separately_from_matching(
        self,
    ):
        compiled = plan()
        rows = tuple(record(f"C{i}", f"VAT-{i}", i + 2) for i in range(5))
        requirements = plan_preflight_requirements(
            compiled, rows, maximum_keys_per_request=2
        )
        domains = [request.domain for request in requirements.record_requests]
        vat_domains = [domain for domain in domains if domain[0][0] == "vat"]
        self.assertEqual(len(vat_domains), 3)
        self.assertEqual(
            {value for domain in vat_domains for value in domain[0][2]},
            {f"VAT-{i}" for i in range(5)},
        )
        self.assertTrue(all(len(domain[0][2]) <= 2 for domain in vat_domains))
        self.assertTrue(
            all(
                set(request.fields) == {"ref", "vat"}
                for request in requirements.record_requests
            )
        )

    def test_preparation_finds_all_repeated_prepared_values_and_ignores_exclusions(
        self,
    ):
        state = _workspace_state()
        rows = tuple(
            replace(
                _canonical_row(
                    token, row, source_identity=(token,), target_identity=(token,)
                ),
                proposed_values={"vat": value},
            )
            for token, row, value in (
                ("a", 2, "VAT-1"),
                ("b", 3, "VAT-1"),
                ("c", 4, ""),
                ("d", 5, None),
            )
        )
        staging = _staging(state.workspace_id, rows)
        ruleset = default_quality_ruleset(
            workspace_id=state.workspace_id,
            mapping_hash=staging.mapping_hash,
            schema_hash=staging.schema_hash,
            datasets=("contacts",),
        )
        run = evaluate_quality(
            workspace_state=state,
            staging=staging,
            physical_rows={"dataset:contacts": (2, 3, 4, 5)},
            ruleset=ruleset,
            unique_fields=((MODEL, "vat"),),
        )
        self.assertFalse(run.ready_for_package)
        findings = [
            issue
            for issue in run.issues
            if issue.reason_code == "SOURCE_FIELD_NOT_UNIQUE"
        ]
        self.assertEqual({issue.source_row for issue in findings}, {2, 3})
        self.assertTrue(
            all(issue.policy is QualityOutcomePolicy.BLOCK for issue in findings)
        )
        self.assertEqual(
            list(
                duplicate_field_values(
                    (
                        rows[0],
                        replace(rows[1], disposition=StagingDisposition.EXCLUDED),
                    ),
                    ((MODEL, "vat"),),
                )
            ),
            [],
        )

    def test_typed_values_preserve_false_zero_case_and_full_decimal_precision(self):
        self.assertIsNotNone(unique_value_key(False))
        self.assertIsNotNone(unique_value_key(0))
        self.assertNotEqual(unique_value_key(False), unique_value_key(0))
        self.assertNotEqual(unique_value_key("vat"), unique_value_key("VAT"))
        self.assertEqual(unique_value_key(Decimal("1.00")), unique_value_key(1))
        with localcontext() as context:
            context.prec = 3
            self.assertNotEqual(
                unique_value_key(Decimal("12345.10")),
                unique_value_key(Decimal("12345.11")),
            )

    def test_saved_mapping_and_compiled_recipe_retain_rule_and_reject_incompatible_intent(
        self,
    ):
        definition = replace(
            QUALITY_DEFINITION,
            datasets=(
                replace(
                    QUALITY_DEFINITION.datasets[0],
                    fields=(
                        ScalarFieldMapping(
                            "vat", "column:reference", must_be_unique=True
                        ),
                    ),
                ),
            ),
        )
        self.assertEqual(MappingDefinition.from_json(definition.to_json()), definition)
        self.assertNotEqual(
            definition.content_hash,
            replace(
                definition,
                datasets=(
                    replace(
                        definition.datasets[0],
                        fields=(
                            replace(
                                definition.datasets[0].fields[0], must_be_unique=False
                            ),
                        ),
                    ),
                ),
            ).content_hash,
        )
        self.assertEqual(CompiledMigrationPlan.from_json(plan().to_json()), plan())
        payload = {
            "profile": {"id": "unique_recipe"},
            "datasets": [plan().datasets[0].model_dump(mode="json")],
        }
        compiled = compile_profile_document(ProfileDocument.model_validate(payload))
        self.assertEqual(compiled.contract_version, 6)
        self.assertTrue(compiled.datasets[0].fields["vat"].must_be_unique)
        for changes in (
            {"validate_only": True},
            {"value_source": ScalarValueSource.ODOO_DEFAULT},
        ):
            with self.assertRaisesRegex(ValueError, "prepared field value"):
                ScalarFieldMapping("vat", must_be_unique=True, **changes)


if __name__ == "__main__":
    unittest.main()
