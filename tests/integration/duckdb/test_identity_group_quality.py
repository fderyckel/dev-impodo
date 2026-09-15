"""Durable generic group propagation matches complete quality evidence."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
import unittest
from unittest.mock import patch

from impodo.application.workspace.preparation.bounded_quality import build_bounded_quality_run, materialize_staging_run
from impodo.domain.preparation.quality import QualityOutcomePolicy, QualityRuleFamily, default_quality_ruleset, evaluate_quality
from impodo.domain.shared.models import Issue, LogicalReference
from impodo.domain.staging.canonical_projection import canonical_prepared_session_row
from impodo.domain.staging.transformation_impact import TransformationImpactReport
from impodo.domain.preparation.staging_contracts import StagingDatasetRole
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.adapters.duckdb import preparation_identity_group_quality as group_adapter
from tests.integration.duckdb import test_preparation_session as session_fixtures
from tests.integration.duckdb.test_preparation_session import (
    SOURCE_HASH, SELECTION_HASH, MAPPING_HASH, SCHEMA_HASH,
)
from tests.domain.preparation.test_quality import _materialized_bounded_quality


def _link(dataset, key, origin="incoming", incoming_key=None):
    return LogicalReference(origin=origin, dataset=dataset, key=key if isinstance(key, tuple) else (key,), incoming_key=incoming_key)


class IdentityGroupQualityTests(unittest.TestCase):
    setUp = session_fixtures.PreparationSessionRepositoryTests.setUp
    tearDown = session_fixtures.PreparationSessionRepositoryTests.tearDown

    def _stored(self, specifications, model_prefix="x_custom"):
        names = sorted({item["dataset"] for item in specifications})
        bindings = replace(self.bindings, source_hashes={name: SOURCE_HASH for name in names})
        session = self.repository.begin_direct_session(self.workspace_state.workspace_id, bindings, actor=LOCAL_ACTOR)
        rows = []
        counts = Counter()
        for ordinal, spec in enumerate(sorted(specifications, key=lambda item: item["dataset"])):
            dataset = spec["dataset"]
            counts[dataset] += 1
            source_row = counts[dataset] + 1
            identity = spec["key"] if isinstance(spec["key"], tuple) else (spec["key"],)
            issues = (Issue(code="SOURCE_REQUIRED_VALUE_MISSING", message="A value is required", dataset=dataset, row=source_row),) if spec.get("unsafe") else ()
            rows.append(canonical_prepared_session_row(
                dataset=dataset, source_row=source_row, target_model=f"{model_prefix}.{dataset}",
                source_identity=identity, target_identity=spec.get("identity", identity),
                target_scope=spec.get("scope", ()), scalar_values=spec.get("scalars", {"x_amount": 1}),
                references=spec.get("references", {}), issues=issues, ordinal=ordinal,
                mode="upsert", source_hash=SOURCE_HASH, source_selection_hash=SELECTION_HASH,
                mapping_hash=MAPPING_HASH, schema_hash=SCHEMA_HASH, field_sources={},
                physical_dataset_id=f"dataset:{dataset}",
            ))
        self.repository.append_direct_rows(self.workspace_state.workspace_id, session.session_id, rows)
        stored = self.repository.finalize_direct_session(
            self.workspace_state.workspace_id, session.session_id,
            dataset_evidence={name: (f"dataset:{name}", StagingDatasetRole.DIRECT, counts[name], f"{model_prefix}.{name}") for name in names},
            run_issues=(), control_totals=(), impact_report=TransformationImpactReport(
                mapping_content_hash=MAPPING_HASH, evaluated_count=0, changed_count=0,
                fallback_count=0, null_count=0, invalid_count=0, provided_count=0,
                unchanged_count=0, rows=(), detail_limit=0,
            ),
        )
        physical = {f"dataset:{name}": tuple(range(2, counts[name] + 2)) for name in names}
        ruleset = default_quality_ruleset(workspace_id=self.workspace_state.workspace_id,
            mapping_hash=MAPPING_HASH, schema_hash=SCHEMA_HASH, datasets=names)
        return stored, physical, ruleset

    def _assert_parity(self, specifications, model_prefix="x_custom", warnings=()):
        stored, physical, ruleset = self._stored(specifications, model_prefix)
        ruleset = replace(ruleset, rules=tuple(
            replace(rule, outcome=QualityOutcomePolicy.WARNING)
            if rule.family is QualityRuleFamily.RELATIONSHIP_READINESS and rule.dataset in warnings else rule
            for rule in ruleset.rules
        ))
        expected = evaluate_quality(workspace_state=self.workspace_state, staging=materialize_staging_run(stored),
            physical_rows=physical, ruleset=ruleset, published_staging_content_hash=stored.validated_content_hash)
        actual = build_bounded_quality_run(workspace_state=self.workspace_state, staging=stored,
            physical_rows=physical, ruleset=ruleset, published_staging_content_hash=stored.validated_content_hash)
        self.assertEqual(_materialized_bounded_quality(actual).to_json(), expected.to_json())
        return actual

    def test_generic_document_group_preserves_ordinary_lookup_and_other_groups(self):
        for model in ("sale", "account", "x_custom"):
            with self.subTest(model=model):
                actual = self._assert_parity([
                    {"dataset": "documents", "key": "A"}, {"dataset": "documents", "key": "B"},
                    {"dataset": "lookups", "key": "AVAILABLE"},
                    {"dataset": "entries", "key": "A-1", "scope": (_link("documents", "A"),), "references": {"x_item": _link("lookups", "MISSING")}},
                    {"dataset": "entries", "key": "A-2", "scope": (_link("documents", "A"),)},
                    {"dataset": "entries", "key": "B-1", "scope": (_link("documents", "B"),)},
                ], model)
                self.assertEqual(actual.summary_counts["quarantined_count"], 3)
                self.assertEqual(actual.summary_counts["ready_count"], 3)

    def test_nested_groups_multiple_identity_parents_composite_keys_and_null_root(self):
        self._assert_parity([
            {"dataset": "nodes", "key": ("site", "root"), "scope": (None,)},
            {"dataset": "nodes", "key": ("site", "child"), "scope": (_link("nodes", ("site", "root")),)},
            {"dataset": "nodes", "key": ("other", "root"), "scope": (None,)},
            {"dataset": "projects", "key": "project"},
            {"dataset": "details", "key": "detail", "identity": (_link("nodes", ("site", "child")), _link("projects", "project"), "detail"), "unsafe": True},
        ])

    def test_missing_and_duplicate_parents_and_repeated_target_identity(self):
        self._assert_parity([
            {"dataset": "documents", "key": "DUP"}, {"dataset": "documents", "key": "DUP"},
            {"dataset": "documents", "key": "GOOD"},
            {"dataset": "entries", "key": "one", "scope": (_link("documents", "DUP"),)},
            {"dataset": "entries", "key": "two", "scope": (_link("documents", "MISSING"),)},
            {"dataset": "entries", "key": "three", "identity": ("SAME",), "scope": (_link("documents", "GOOD"),)},
            {"dataset": "entries", "key": "four", "identity": ("SAME",), "scope": (_link("documents", "GOOD"),)},
        ])

    def test_safe_and_unsafe_cycles_and_self_link_terminate(self):
        for unsafe in (False, True):
            with self.subTest(unsafe=unsafe):
                self._assert_parity([
                    {"dataset": "nodes", "key": "A", "scope": (_link("nodes", "B"),), "unsafe": unsafe},
                    {"dataset": "nodes", "key": "B", "scope": (_link("nodes", "A"),)},
                    {"dataset": "nodes", "key": "SELF", "scope": (_link("nodes", "SELF"),), "unsafe": unsafe},
                ])

    def test_long_self_referencing_group_matches_complete_quality(self):
        actual = self._assert_parity([
            {"dataset": "nodes", "key": str(index), "unsafe": index == 0,
             "scope": (_link("nodes", str(index - 1)),) if index else (None,)}
            for index in range(1_024)
        ])
        self.assertEqual(actual.summary_counts["quarantined_count"], 1_024)

    def test_warning_policy_does_not_propagate_a_safe_warning_as_an_unsafe_record(self):
        specs = [
            {"dataset": "documents", "key": "A"},
            {"dataset": "entries", "key": "one", "scope": (_link("documents", "A"),), "unsafe": True},
            {"dataset": "entries", "key": "two", "scope": (_link("documents", "A"),)},
        ]
        self._assert_parity(specs, warnings=("documents",))
        self._assert_parity(specs, warnings=("entries",))

    def test_target_catalog_and_hybrid_identity_keep_current_direction_and_key_semantics(self):
        self._assert_parity([
            {"dataset": "documents", "key": "TARGET"},
            {"dataset": "entries", "key": "one", "scope": (
                LogicalReference(origin="target", key=("EXISTING",), model="x_target"),
                _link("documents", "TARGET", origin="target_then_incoming", incoming_key=("DIFFERENT",)),
            ), "unsafe": True},
            {"dataset": "entries", "key": "two", "scope": (_link("documents", "TARGET"),)},
        ])

    def test_interrupted_group_projection_rebuilds_without_changing_canonical_evidence(self):
        stored, physical, ruleset = self._stored([
            {"dataset": "documents", "key": "A"},
            {"dataset": "entries", "key": "one", "scope": (_link("documents", "A"),), "unsafe": True},
            {"dataset": "entries", "key": "two", "scope": (_link("documents", "A"),)},
        ])
        arguments = dict(workspace_state=self.workspace_state, staging=stored,
                         physical_rows=physical, ruleset=ruleset,
                         published_staging_content_hash=stored.validated_content_hash)
        original = group_adapter._project_group_edges

        def interrupted(*args):
            original(*args)
            raise RuntimeError("interrupted after group projection")

        with patch.object(group_adapter, "_project_group_edges", interrupted):
            with self.assertRaisesRegex(RuntimeError, "interrupted after group projection"):
                build_bounded_quality_run(**arguments)
        actual = build_bounded_quality_run(**arguments)
        expected = evaluate_quality(**{**arguments, "staging": materialize_staging_run(stored)})
        self.assertEqual(_materialized_bounded_quality(actual).to_json(), expected.to_json())
        self.assertEqual(materialize_staging_run(stored).content_hash, stored.validated_content_hash)

    def test_projection_pages_only_identity_scope_and_links_for_wide_rows(self):
        stored, _, _ = self._stored([
            {"dataset": "documents", "key": "A", "scalars": {"x_wide": "v" * 100_000}},
            *({"dataset": "entries", "key": str(index), "scope": (_link("documents", "A"),),
               "scalars": {"x_wide": "v" * 100_000}} for index in range(7)),
        ])
        unsafe = next(row.row_id for row in stored.rows if row.dataset == "entries")
        original_batches = group_adapter.iter_encoded_json_batches
        original_projection = group_adapter._iter_group_projection_batches
        transported = []
        projections = []

        def inspect_batches(*args, **kwargs):
            for batch in original_batches(*args, **kwargs):
                transported.append(batch)
                yield batch

        def inspect_projection(*args):
            for batch in original_projection(*args):
                projections.append(batch)
                yield batch

        with (
            patch.object(group_adapter, "PREPARATION_SESSION_ROW_BATCH_SIZE", 2),
            patch.object(group_adapter, "DUCKDB_JSON_BATCH_MAX_BYTES", 300),
            patch.object(group_adapter, "iter_encoded_json_batches", inspect_batches),
            patch.object(group_adapter, "_iter_group_projection_batches", inspect_projection),
            patch("impodo.domain.preparation.staging_contracts.CanonicalRow.from_dict",
                  side_effect=AssertionError("decoded a complete canonical row")),
        ):
            findings = tuple(stored.rows.bounded_identity_group_findings((unsafe,), ("documents", "entries")))
        self.assertEqual(len(findings), 8)
        self.assertGreater(len(transported), 1)
        self.assertTrue(all(len(batch.payload.encode("utf-8")) <= 300 for batch in transported))
        self.assertTrue(all(len(batch) <= 2 for batch in projections))
        self.assertTrue(all(sum(len(str(value).encode("utf-8")) for row in batch for value in row[1:]) <= 300
                            or len(batch) == 1 for batch in projections))

    def test_large_unicode_identity_is_projected_alone_without_dropping_following_rows(self):
        key = "é" * 900
        stored, _, _ = self._stored([
            {"dataset": "documents", "key": key},
            *({"dataset": "entries", "key": str(index), "scope": (_link("documents", key),)}
              for index in range(3)),
        ])
        unsafe = next(row.row_id for row in stored.rows if row.dataset == "entries")
        original = group_adapter._iter_group_projection_batches
        projections = []

        def inspect_projection(*args):
            for batch in original(*args):
                projections.append(batch)
                yield batch

        with (
            patch.object(group_adapter, "DUCKDB_JSON_BATCH_MAX_BYTES", 300),
            patch.object(group_adapter, "_iter_group_projection_batches", inspect_projection),
        ):
            findings = tuple(stored.rows.bounded_identity_group_findings((unsafe,), ("documents", "entries")))
        self.assertEqual(len(findings), 4)
        self.assertEqual([len(batch) for batch in projections], [1, 1, 1, 1])


if __name__ == "__main__":
    unittest.main()
