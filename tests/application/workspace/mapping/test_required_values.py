"""Required-value coverage reuses policy and never invents row or create proof."""

from dataclasses import replace
from types import SimpleNamespace
import unittest

from impodo.application.workspace.mapping.required_values import (
    RequiredValueProvider as Provider,
    ValueCoverageState as State,
    project_required_values,
)
from impodo.application.workspace.mapping.service import MappingWorkspaceService
from impodo.domain.mapping.contracts import (
    MappingTargetMode,
    ScalarFieldMapping,
    ScalarValueSource,
    TargetFieldDisposition,
    TargetFieldHandling,
)
from impodo.domain.mapping.validation.evidence import (
    MappingValidationIssue,
    MappingValidationResult,
    MappingValidationStatus,
)
from impodo.domain.shared.access import (
    Actor,
    ActorIdentity,
    AuthorizationError,
    CapabilityAuthorizationPolicy,
)
from tests.application.workspace.mapping.test_matching_order_live_check import _evidence


class RequiredValuesTests(unittest.TestCase):
    def inputs(self):
        evidence = _evidence()
        code = replace(evidence.schema.models[1].fields[0], required=True)
        fields = (
            code,
            *(
                replace(code, name=name, label=name)
                for name in (
                    "fixed",
                    "default",
                    "unverified",
                    "managed",
                    "missing",
                )
            ),
            replace(evidence.schema.models[1].fields[1], required=True),
        )
        fields = tuple(
            replace(
                field,
                create_default_present=True,
                create_default_value=79000222,
                type="many2one",
                relation="product.template",
            )
            if field.name == "default"
            else replace(field, computed=True)
            if field.name == "managed"
            else field
            for field in fields
        )
        evidence.schema = replace(
            evidence.schema,
            models=(
                evidence.schema.models[0],
                replace(evidence.schema.models[1], fields=fields),
            ),
        )
        mapping = replace(
            evidence.draft.definition.datasets[0],
            fields=(
                ScalarFieldMapping(
                    "fixed",
                    None,
                    value_source=ScalarValueSource.CONSTANT,
                    literal_value="private constant",
                ),
            ),
            target_field_dispositions=(
                TargetFieldDisposition("default", TargetFieldHandling.ODOO_DEFAULT),
                TargetFieldDisposition("unverified", TargetFieldHandling.ODOO_DEFAULT),
            ),
        )
        evidence.draft = replace(
            evidence.draft,
            definition=replace(
                evidence.draft.definition,
                datasets=(mapping, evidence.draft.definition.datasets[1]),
            ),
        )
        return evidence

    def project(self, evidence, validation=None, **kwargs):
        return project_required_values(
            evidence.selection,
            evidence.schema,
            evidence.governance,
            None,
            evidence.draft,
            validation,
            **kwargs,
        )

    def validation(self, evidence):
        definition = evidence.draft.definition
        return MappingValidationResult(
            definition.content_hash,
            definition.source_selection_hash,
            definition.schema_hash,
            MappingValidationStatus.VALID,
            (),
            (),
            (),
        )

    def test_chosen_providers_required_parents_and_missing_values_are_separate(self):
        evidence = self.inputs()
        result = self.project(evidence)
        rows = {item.target_field: item for item in result.tables[0].values}
        self.assertEqual(rows["code"].provider, Provider.SOURCE)
        self.assertEqual(rows["fixed"].provider, Provider.FIXED)
        self.assertEqual(rows["default"].provider, Provider.DEFAULT)
        self.assertEqual(rows["default"].state, State.CHOSEN)
        self.assertEqual(rows["unverified"].state, State.ATTENTION)
        self.assertEqual(rows["managed"].provider, Provider.MANAGED)
        self.assertEqual(rows["missing"].state, State.MISSING)
        self.assertEqual(result.tables[0].identity_state, State.CHOSEN)
        self.assertEqual(result.tables[0].parent_state, State.CHOSEN)
        self.assertNotIn("79000222", str(result))
        self.assertNotIn("private constant", str(result))

    def test_current_validation_does_not_prove_identity_or_parent_rows(self):
        evidence = self.inputs()
        result = self.project(evidence, self.validation(evidence))
        self.assertTrue(result.validation_current)
        self.assertEqual(result.tables[0].identity_state, State.CHOSEN)
        self.assertEqual(result.tables[0].parent_state, State.CHOSEN)
        self.assertEqual(
            next(
                item for item in result.tables[0].values if item.target_field == "fixed"
            ).state,
            State.CHECKED,
        )
        check = SimpleNamespace(
            identity_results=(),
            relationship_health_results=(
                SimpleNamespace(
                    owner_dataset_id="dataset:bom",
                    target_field="product_tmpl_id",
                    checked=True,
                    blocked_count=3,
                ),
            ),
        )
        self.assertEqual(
            self.project(
                evidence,
                self.validation(evidence),
                live_check=check,
                live_check_current=True,
            )
            .tables[0]
            .parent_state,
            State.ATTENTION,
        )

    def test_draft_source_and_schema_changes_invalidate_checked_choices(self):
        evidence = self.inputs()
        validation = self.validation(evidence)
        evidence.draft = replace(
            evidence.draft,
            definition=replace(
                evidence.draft.definition,
                datasets=(
                    replace(evidence.draft.definition.datasets[0], fields=()),
                    evidence.draft.definition.datasets[1],
                ),
            ),
        )
        self.assertFalse(self.project(evidence, validation).validation_current)
        self.assertEqual(
            next(
                item
                for item in self.project(evidence, validation).tables[0].values
                if item.target_field == "code"
            ).state,
            State.STALE,
        )
        evidence.selection = replace(
            evidence.selection, content_hash="sha256:" + "9" * 64
        )
        self.assertFalse(self.project(evidence, validation).inputs_current)
        self.assertTrue(
            all(
                item.state is State.STALE
                for item in self.project(evidence, validation).tables[0].values
            )
        )
        evidence = self.inputs()
        evidence.governance = replace(
            evidence.governance, catalog_hash="sha256:" + "9" * 64
        )
        self.assertFalse(self.project(evidence).inputs_current)

    def test_reference_and_pinned_update_tables_do_not_inherit_create_requirements(
        self,
    ):
        for mode in (MappingTargetMode.REFERENCE, MappingTargetMode.ODOO_PINNED_UPDATE):
            evidence = self.inputs()
            evidence.draft = replace(
                evidence.draft,
                definition=replace(
                    evidence.draft.definition,
                    datasets=(
                        replace(
                            evidence.draft.definition.datasets[0],
                            mode=mode,
                            fields=(),
                            relationships=(),
                            target_field_dispositions=(),
                        ),
                        evidence.draft.definition.datasets[1],
                    ),
                ),
            )
            table = self.project(evidence).tables[0]
            self.assertFalse(table.create_requirements)
            self.assertEqual(table.values, ())

    def test_unavailable_fields_and_missing_identity_cannot_disappear_behind_counts(
        self,
    ):
        evidence = self.inputs()
        model = evidence.schema.models[1]
        evidence.schema = replace(
            evidence.schema,
            models=(
                evidence.schema.models[0],
                replace(
                    model,
                    fields=tuple(
                        field for field in model.fields if field.name != "fixed"
                    ),
                ),
            ),
        )
        evidence.draft = replace(
            evidence.draft,
            definition=replace(
                evidence.draft.definition,
                datasets=(
                    replace(evidence.draft.definition.datasets[0], target_identity=()),
                    evidence.draft.definition.datasets[1],
                ),
            ),
        )
        table = self.project(evidence).tables[0]
        self.assertEqual(table.identity_state, State.MISSING)
        self.assertEqual(
            next(item for item in table.values if item.target_field == "fixed").state,
            State.ATTENTION,
        )

    def test_application_authorizes_before_inspecting_supplied_metadata(self):
        service = object.__new__(MappingWorkspaceService)
        service.authorization = CapabilityAuthorizationPolicy()
        actor = Actor(ActorIdentity("server", "reader", "Reader"), frozenset())
        with self.assertRaises(AuthorizationError):
            service.required_values(
                "workspace", None, None, None, None, None, None, actor=actor
            )

    def test_optional_captured_fields_keep_mapping_required_default_meaning(self):
        for required, on_create in ((True, False), (False, True)):
            evidence = self.inputs()
            model = evidence.schema.models[1]
            evidence.schema = replace(
                evidence.schema,
                models=(
                    evidence.schema.models[0],
                    replace(
                        model,
                        fields=tuple(
                            replace(
                                field,
                                required=False,
                                create_default_present=True,
                                create_default_value="omitted",
                            )
                            if field.name == "fixed"
                            else field
                            for field in model.fields
                        ),
                    ),
                ),
            )
            mapping = evidence.draft.definition.datasets[0]
            evidence.draft = replace(
                evidence.draft,
                definition=replace(
                    evidence.draft.definition,
                    datasets=(
                        replace(
                            mapping,
                            fields=(
                                ScalarFieldMapping(
                                    "fixed",
                                    value_source=ScalarValueSource.ODOO_DEFAULT,
                                    required=required,
                                    required_on_create=on_create,
                                ),
                            ),
                        ),
                        evidence.draft.definition.datasets[1],
                    ),
                ),
            )
            row = next(
                item
                for item in self.project(evidence).tables[0].values
                if item.target_field == "fixed"
            )
            self.assertEqual(
                (row.provider, row.state), (Provider.DEFAULT, State.CHOSEN)
            )
            self.assertTrue(row.default_verified)

    def test_non_create_modes_ignore_retained_create_only_requirements(self):
        for mode in (MappingTargetMode.REFERENCE, MappingTargetMode.ODOO_PINNED_UPDATE):
            evidence = self.inputs()
            mapping = evidence.draft.definition.datasets[0]
            evidence.draft = replace(
                evidence.draft,
                definition=replace(
                    evidence.draft.definition,
                    datasets=(
                        replace(
                            mapping,
                            mode=mode,
                            fields=(
                                ScalarFieldMapping(
                                    "fixed", "column:bom", required_on_create=True
                                ),
                                ScalarFieldMapping(
                                    "missing", "column:bom", required=True
                                ),
                            ),
                        ),
                        evidence.draft.definition.datasets[1],
                    ),
                ),
            )
            fields = {
                item.target_field for item in self.project(evidence).tables[0].values
            }
            self.assertNotIn("fixed", fields)
            self.assertIn("missing", fields)

    def test_authorized_projection_rejects_foreign_drafts_and_check_receipts(self):
        from impodo.domain.shared.access import LOCAL_ACTOR
        from impodo.domain.workspace.errors import WorkspaceError

        service = object.__new__(MappingWorkspaceService)
        service.authorization = CapabilityAuthorizationPolicy()
        evidence = self.inputs()
        for draft, check in (
            (replace(evidence.draft, workspace_id="foreign"), None),
            (evidence.draft, SimpleNamespace(workspace_id="foreign")),
        ):
            with self.assertRaises(WorkspaceError):
                service.required_values(
                    evidence.workspace_id,
                    evidence.selection,
                    evidence.schema,
                    evidence.governance,
                    None,
                    draft,
                    None,
                    actor=LOCAL_ACTOR,
                    live_check=check,
                )

    def test_validation_messages_cannot_copy_private_values_into_the_projection(self):
        evidence = self.inputs()
        validation = replace(
            self.validation(evidence),
            status=MappingValidationStatus.INVALID,
            issues=(
                MappingValidationIssue(
                    "MAPPING_SELECTION_VALUE_INVALID",
                    "error",
                    "/datasets/0/fields/0",
                    "Private selection literal 79000222 is invalid",
                    "Review the literal",
                    dataset_id="dataset:bom",
                    target_model="mrp.bom",
                    target_field="fixed",
                ),
            ),
        )
        result = self.project(evidence, validation)
        self.assertNotIn("Private selection literal", str(result))
        self.assertNotIn("79000222", str(result))
        self.assertEqual(
            next(
                item for item in result.tables[0].values if item.target_field == "fixed"
            ).state,
            State.ATTENTION,
        )
