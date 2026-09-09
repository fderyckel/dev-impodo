"""Protect complete target keys, current evidence, and run-only mutations."""

from dataclasses import replace
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock
from uuid import uuid4

from impodo.application.run.target_matches import RecipeTargetMatchService, apply_target_match_decisions
from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy, DatasetMapping, MappingDefinition, ReferenceKeyMapping,
    RelationshipMapping, RelationshipResolver, ResolverOrigin,
)
from impodo.domain.run.contracts import RecipeApplicationStatus
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.supporting_lookups import SupportingLookupChoice, portable_supporting_value


class TargetMatchServiceTests(TestCase):
    def fixture(self, *, keys=("code", "region"), scopes=("company",), repeated=False):
        columns = (*keys, *scopes)
        self.source_values = tuple(f"value-{key}" for key in columns)
        self.key = portable_supporting_value(self.source_values)
        relationship = RelationshipMapping(
            target_field="category_id", kind="many2one", source_column_keys=columns,
            resolver=RelationshipResolver(
                origin=ResolverOrigin.TARGET_CATALOG, model="x.category",
                key_mappings=tuple(ReferenceKeyMapping(key, key) for key in keys),
                scope_mappings=tuple(ReferenceKeyMapping(key, key) for key in scopes),
            ), categorical_policy=CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
        )
        relationships = (relationship, replace(relationship, target_field="other_id")) if repeated else (relationship,)
        self.definition = MappingDefinition(
            mapping_id=str(uuid4()), source_selection_hash=content_hash("source"),
            schema_hash=content_hash("schema"),
            datasets=(DatasetMapping(dataset_id="contacts", target_model="res.partner", relationships=relationships),),
        )
        self.working = NS(version=3, definition=self.definition)
        self.results = tuple(NS(
            path=f"/datasets/0/relationships/{index}", source_column_keys=columns,
            distinct_values=(NS(values=self.source_values, count=2),), uncovered_values=(), status="COVERED",
        ) for index in range(len(relationships)))
        self.evidence = NS(field_results=self.results, content_hash=content_hash("coverage"))
        self.coverage = NS(evidence=self.evidence, issues=())
        self.schema = NS(
            content_hash=content_hash("schema"), connection_target_hash=content_hash("target"),
            read_credential_binding_hash=content_hash("credential"), read_principal_hash=content_hash("principal"),
            read_permission_hash=content_hash("permission"), read_context_hash=content_hash("context"),
            models=(NS(name="res.partner", label="Contacts", fields=tuple(
                NS(name=item.target_field, type="many2one", label="Category") for item in relationships
            )), NS(name="x.category", fields=tuple(NS(name=key) for key in (*columns, "name")))),
        )
        self.mapping = NS(
            authorization=Mock(), recipe_applications=NS(assert_mapping_adaptation=Mock()),
            mappings=Mock(get_mapping_working_draft=Mock(return_value=self.working), get_mapping_revision=Mock(return_value=NS(version=2))),
            sources=Mock(get_mapping_source_selection=Mock(return_value=NS(datasets=(NS(
                dataset_id="contacts", name="Contacts", columns=tuple(NS(stable_key=key, source_name=key.title()) for key in columns),
            ),)))), schemas=Mock(get_odoo_schema_catalog=Mock(return_value=self.schema)),
            categorical_coverage=Mock(collect=Mock(return_value=self.coverage)),
            save_working_draft=Mock(), check_definition=Mock(), submit_current=Mock(),
        )
        self.application = NS(
            application_id="application", workspace_id="workspace", migration_run_id="run",
            status=RecipeApplicationStatus.BLOCKED,
        )
        self.planning = Mock(repository=Mock(
            get_bundle=Mock(return_value=NS(workspaces=(NS(workspace_id="setup", recipe_application_id=None),))),
            get_application=Mock(return_value=self.application),
        ))
        self.snapshot = NS(
            choices=(SupportingLookupChoice(self.key, "Sales category"),), ambiguous_values=(), content_hash=content_hash("lookup"),
        )
        self.lookups = Mock(current=Mock(return_value=self.snapshot))
        self.service = RecipeTargetMatchService(self.mapping, self.planning, self.lookups)
        return self.service.build_review(self.application, actor=LOCAL_ACTOR)

    def confirm(self, review, **overrides):
        arguments = dict(
            decisions={}, expected_working_draft_version=review.working_draft_version,
            expected_definition_hash=review.definition_hash, expected_evidence_hash=review.evidence_hash,
            actor=LOCAL_ACTOR,
        )
        arguments.update(overrides)
        return self.service.confirm(self.application, **arguments)

    def test_composite_and_scope_are_verified_together_with_one_scan_and_lookup(self):
        review = self.fixture(repeated=True)
        self.assertTrue(review.can_confirm)
        self.assertEqual(review.verified_count, 2)
        self.assertTrue(all(not field.editable for field in review.fields))
        self.assertEqual(review.fields[0].rows[0].source_label, "value-code · value-region · value-company")
        self.mapping.categorical_coverage.collect.assert_called_once()
        self.lookups.current.assert_called_once()
        self.assertEqual(self.lookups.current.call_args.kwargs["scope_fields"], ("company",))
        self.assertEqual(apply_target_match_decisions(self.definition, review, {}), self.definition.datasets)

    def test_same_key_in_another_company_is_not_a_match(self):
        self.fixture()
        self.snapshot.choices = (SupportingLookupChoice(portable_supporting_value((*self.source_values[:-1], "another-company")), "Sales category"),)
        review = self.service.build_review(self.application, actor=LOCAL_ACTOR)
        self.assertFalse(review.can_confirm)
        self.assertEqual(review.verified_count, 0)
        self.assertIn("including every company", review.fields[0].blocking_reason)
        with self.assertRaises(WorkspaceError):
            self.confirm(review)
        self.mapping.save_working_draft.assert_not_called()

    def test_ambiguous_keys_cannot_be_selected_or_suggested(self):
        self.fixture(keys=("code",), scopes=())
        self.snapshot.ambiguous_values = (self.key,)
        review = self.service.build_review(self.application, actor=LOCAL_ACTOR)
        self.assertFalse(review.can_confirm)
        self.assertEqual(review.fields[0].choices, ())
        self.assertEqual(review.fields[0].rows[0].selected_target_value, "")
        self.snapshot.ambiguous_values = ()
        self.snapshot.choices = (SupportingLookupChoice("first", "Sales category"), SupportingLookupChoice("second", "Sales category"))
        self.results[0].distinct_values = (NS(values=("Sales category",), count=1),)
        review = self.service.build_review(self.application, actor=LOCAL_ACTOR)
        self.assertEqual(review.fields[0].rows[0].selected_target_value, "")

    def test_complete_keys_keep_delimiters_and_component_order(self):
        self.assertNotEqual(portable_supporting_value(("a,b", "c")), portable_supporting_value(("a", "b,c")))
        self.assertNotEqual(portable_supporting_value(("a", "b")), portable_supporting_value(("b", "a")))
        self.assertEqual(portable_supporting_value(("BE",)), "BE")

    def test_changed_target_or_source_evidence_rejects_an_open_form_before_mutation(self):
        for evidence in ("lookup", "source", "schema", "identity", "version"):
            with self.subTest(evidence=evidence):
                review = self.fixture()
                if evidence == "lookup":
                    self.snapshot.content_hash = content_hash("new lookup")
                elif evidence == "source":
                    self.evidence.content_hash = content_hash("new source")
                elif evidence == "schema":
                    self.schema.content_hash = content_hash("new schema")
                elif evidence == "identity":
                    self.schema.read_permission_hash = content_hash("new permissions")
                else:
                    self.working.version += 1
                with self.assertRaisesRegex(WorkspaceError, "changed"):
                    self.confirm(review)
                self.mapping.save_working_draft.assert_not_called()
                self.mapping.check_definition.assert_not_called()

    def test_missing_capture_and_oversized_domain_are_not_shown_as_matched(self):
        for missing in (True, False):
            with self.subTest(missing_capture=missing):
                self.fixture()
                if missing:
                    self.lookups.current.return_value = None
                else:
                    self.results[0].status = "UNSUPPORTED"
                    self.results[0].distinct_values = ()
                review = self.service.build_review(self.application, actor=LOCAL_ACTOR)
                self.assertFalse(review.can_confirm)
                self.assertTrue(review.fields[0].blocking_reason)

    def test_unrepresented_provider_failure_blocks_otherwise_valid_choices(self):
        self.fixture()
        self.coverage.issues = (NS(path="/datasets/0/fields/0", message="The fixed Recipe fallback is not a current Odoo value."),)
        review = self.service.build_review(self.application, actor=LOCAL_ACTOR)
        self.assertFalse(review.can_confirm)
        self.assertIn("fixed Recipe fallback", review.blocking_reasons[0])

    def test_forged_readonly_decision_is_rejected_before_save(self):
        review = self.fixture()
        with self.assertRaisesRegex(WorkspaceError, "Only the target choices"):
            self.confirm(review, decisions={review.fields[0].rows[0].input_name: self.key})
        self.mapping.save_working_draft.assert_not_called()

    def test_fixed_recipe_guard_runs_before_any_mapping_write(self):
        review = self.fixture()
        self.mapping.recipe_applications.assert_mapping_adaptation.side_effect = WorkspaceError("Recipe rules changed")
        with self.assertRaisesRegex(WorkspaceError, "Recipe rules changed"):
            self.confirm(review)
        self.mapping.save_working_draft.assert_not_called()
        self.mapping.check_definition.assert_not_called()

    def test_started_preparation_cannot_resubmit_target_matches(self):
        review = self.fixture()
        self.application.status = RecipeApplicationStatus.PREPARED
        with self.assertRaisesRegex(WorkspaceError, "beyond target-value review"):
            self.confirm(review)
        self.mapping.check_definition.assert_not_called()

    def test_validation_failure_never_submits_or_advances_application(self):
        review = self.fixture()
        self.mapping.check_definition.return_value = (NS(version=3), NS(
            status=NS(value="INVALID"), issues=(NS(severity="error", message="The Recipe identity is incompatible."),),
        ))
        with self.assertRaisesRegex(WorkspaceError, "identity is incompatible"):
            self.confirm(review)
        self.mapping.submit_current.assert_not_called()
        self.planning.confirm_application_mapping.assert_not_called()
