"""Exercise Recipe mapping guards and quality rebinding against real DuckDB."""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import shutil
from types import SimpleNamespace as NS
from unittest import TestCase
from uuid import uuid4

import duckdb

from impodo.adapters.duckdb.recipe_quality_seed_repository import RecipeQualitySeedRepository
from impodo.adapters.duckdb.schema.workspace_engine import WorkspaceEngineSchemaMixin
from impodo.domain.mapping.contracts import (
    BusinessControlDefinition, CategoricalCoveragePolicy, DatasetMapping,
    MappingControlExpectation, MappingDefinition, RowInclusionCondition,
    RowInclusionMode, RowInclusionPolicy, ScalarFieldMapping,
    SelectionConditionOperator, ValueMapping,
)
from impodo.domain.preparation.quality import manager_quality_rule, QualityRuleFamily, QualityOutcomePolicy
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.errors import WorkspaceError


class RecipeApplicationEvidenceTests(TestCase):
    def setUp(self):
        Path(".tmp").mkdir(exist_ok=True)
        self.path = Path(".tmp") / f"recipe-evidence-{uuid4()}"
        self.path.mkdir()
        self.addCleanup(shutil.rmtree, self.path)
        (self.path / "workspace-engine.duckdb").touch()
        self.connection = duckdb.connect(":memory:")
        self.addCleanup(self.connection.close)
        schema = WorkspaceEngineSchemaMixin()
        schema._initialize_workspace_database(self.connection)
        self.application_id = str(uuid4())
        self.workspace_id = str(uuid4())

        @contextmanager
        def connect(path):
            yield self.connection

        self.database = NS(
            workspace_directory=lambda workspace_id: self.path,
            assert_workspace_mutable=lambda workspace_id: None,
            resolve_workspace_access_context=lambda workspace_id: NS(recipe_application_id=self.application_id),
            _connect=connect,
            _ensure_workspace_database_schema=schema._ensure_workspace_database_schema,
            _insert_workspace_audit=lambda *args, **kwargs: None,
        )
        self.repository = RecipeQualitySeedRepository(self.database)
        self.repository._workspace_revision = lambda connection: 1
        self.definition = MappingDefinition(
            mapping_id=str(uuid4()), source_selection_hash=content_hash("sources"),
            schema_hash=content_hash("schema"), datasets=(DatasetMapping(
                dataset_id="contacts", target_model="res.partner",
                fields=(ScalarFieldMapping(
                    target_field="name", source_column_key="name",
                    categorical_policy=CategoricalCoveragePolicy.EXACT_TARGET_VALUE,
                ),),
                control_definitions=(BusinessControlDefinition(
                    control_id="total", name="Total", target_field="amount",
                ),),
            ),),
        )
        self.rule = manager_quality_rule(
            workspace_id=self.workspace_id, dataset="contacts",
            family=QualityRuleFamily.EQUALITY, name="Names agree",
            input_fields=("name", "display_name"), outcome=QualityOutcomePolicy.WARNING,
        )

    def save_seed(self):
        self.repository.save_quality_seed(
            self.workspace_id, application_id=self.application_id,
            mapping_content_hash=self.definition.content_hash,
            mapping_definition=self.definition, rules=(self.rule,), actor=LOCAL_ACTOR,
        )

    def test_missing_or_stale_rules_block_recipe_preparation(self):
        with self.assertRaisesRegex(WorkspaceError, "business checks are missing"):
            self.repository.get_quality_seed(self.workspace_id, self.definition.content_hash)
        self.save_seed()
        with self.assertRaisesRegex(WorkspaceError, "earlier mapping"):
            self.repository.get_quality_seed(self.workspace_id, content_hash("changed"))
        self.application_id = None
        self.connection.execute("DELETE FROM recipe_quality_seed")
        self.assertEqual(self.repository.get_quality_seed(self.workspace_id, self.definition.content_hash), ())

    def test_target_choices_and_missing_legacy_totals_preserve_quality_rules(self):
        self.save_seed()
        dataset = self.definition.datasets[0]
        changed = replace(self.definition, datasets=(replace(
            dataset, fields=(replace(
                dataset.fields[0], value_mappings=(ValueMapping("A", "B"),),
                categorical_policy=CategoricalCoveragePolicy.EXPLICIT_VALUE_MATCH,
            ),), control_expectations=(MappingControlExpectation("total", "500"),),
        ),))
        self.repository.rebind_quality_seed(self.workspace_id, definition=changed, actor=LOCAL_ACTOR)
        self.assertEqual(self.repository.get_quality_seed(self.workspace_id, changed.content_hash), (self.rule,))
        row = self.connection.execute("SELECT mapping_definition_json FROM recipe_quality_seed").fetchone()
        self.assertEqual(row[0], self.definition.to_json())

    def test_totals_accepted_before_compilation_cannot_change_in_the_matcher(self):
        dataset = replace(self.definition.datasets[0], control_expectations=(MappingControlExpectation("total", "125.50"),))
        self.definition = replace(self.definition, datasets=(dataset,))
        self.save_seed()
        changed = replace(self.definition, datasets=(replace(dataset,
            control_expectations=(MappingControlExpectation("total", "999"),),
        ),))
        with self.assertRaisesRegex(WorkspaceError, "accepted delivery totals"):
            self.repository.rebind_quality_seed(self.workspace_id, definition=changed, actor=LOCAL_ACTOR)
        self.assertEqual(self.repository.get_quality_seed(self.workspace_id, self.definition.content_hash), (self.rule,))

    def test_semantic_changes_and_invariant_totals_are_rejected(self):
        self.save_seed()
        dataset = self.definition.datasets[0]
        for changed in (
            replace(dataset, target_model="product.template"),
            replace(dataset, source_identity_column_keys=("other",)),
            replace(dataset, fields=(replace(dataset.fields[0], source_column_key="other"),)),
            replace(dataset, control_definitions=(replace(dataset.control_definitions[0], tolerance="100"),)),
            replace(
                dataset,
                row_inclusion=RowInclusionPolicy(
                    mode=RowInclusionMode.MATCHING_ROWS,
                    conditions=(
                        RowInclusionCondition(
                            condition_id=str(uuid4()),
                            source_column_key="name",
                            operator=SelectionConditionOperator.EQUALS,
                            comparison_value="Active",
                        ),
                    ),
                ),
            ),
        ):
            with self.subTest(changed=changed), self.assertRaisesRegex(WorkspaceError, "alters the pinned Recipe"):
                self.repository.rebind_quality_seed(
                    self.workspace_id, definition=replace(self.definition, datasets=(changed,)), actor=LOCAL_ACTOR,
                )
        self.definition = replace(self.definition, datasets=(replace(
            dataset, control_definitions=(replace(dataset.control_definitions[0], invariant_expectation=True),),
            control_expectations=(MappingControlExpectation("total", "1"),),
        ),))
        self.save_seed()
        with self.assertRaisesRegex(WorkspaceError, "alters the pinned Recipe"):
            self.repository.assert_mapping_adaptation(self.workspace_id, replace(
                self.definition, datasets=(replace(self.definition.datasets[0], control_expectations=(MappingControlExpectation("total", "2"),)),),
            ))

    def test_upgrade_keeps_old_checks_but_requires_a_baseline_for_new_choices(self):
        self.save_seed()
        self.connection.execute("ALTER TABLE recipe_quality_seed DROP COLUMN mapping_definition_json")
        self.connection.execute("UPDATE schema_version SET version = 9")
        self.assertEqual(self.repository.get_quality_seed(self.workspace_id, self.definition.content_hash), (self.rule,))
        self.repository.assert_mapping_adaptation(self.workspace_id, self.definition)
        with self.assertRaisesRegex(WorkspaceError, "no saved Recipe mapping baseline"):
            self.repository.assert_mapping_adaptation(self.workspace_id, replace(self.definition, schema_hash=content_hash("new schema")))
