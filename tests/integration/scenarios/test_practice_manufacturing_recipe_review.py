"""Test explicit manufacturing review gates before application or target writes."""

from types import SimpleNamespace as NS
from unittest import TestCase, main
from unittest.mock import Mock, patch

from impodo.domain.mapping.validation.evidence import (
    MappingValidationIssue,
    mapping_issue_fingerprint,
)
from scripts import practice_manufacturing_recipe_review as review
from scripts.qualify_practice_customers import PracticeQualificationError


class ManufacturingRecipeReviewTests(TestCase):
    def fixture(
        self,
        *,
        phase="product-status",
        code=review.ADVISORY,
        model="product.template",
        field="active",
        status="VALID_WITH_WARNINGS",
        severity="warning",
        population=(32, 16, 16),
    ):
        issue = MappingValidationIssue(
            code,
            severity,
            "/datasets/0/fields/0",
            "Review candidate type",
            "Review samples",
            dataset_id="dataset",
            target_model=model,
            target_field=field,
        )
        dataset = NS(
            dataset_id="dataset", target_model=model, fields=(NS(target_field=field),)
        )
        revision = NS(
            version=2, definition=NS(datasets=(dataset,), content_hash="mapping")
        )
        validation = NS(
            status=NS(value=status), issues=(issue,), mapping_content_hash="mapping"
        )
        mappings = NS(
            get_mapping_revision=Mock(return_value=revision),
            get_mapping_working_draft=Mock(return_value=NS(version=3)),
            get_mapping_validation=Mock(return_value=validation),
        )
        confirmed = NS(status=NS(value="READY"))
        context = NS(
            mapping_workspace=NS(mappings=mappings, submit_current=Mock()),
            row_inclusion_reviews=NS(
                check_current=Mock(
                    return_value=NS(
                        source_row_count=population[0],
                        included_count=population[1],
                        excluded_count=population[2],
                        cannot_evaluate_count=0,
                    )
                ),
                confirm_current=Mock(),
            ),
            run_planning=NS(
                repository=NS(
                    list_issues=Mock(return_value=(NS(code=code, blocks=True),))
                ),
                confirm_application_mapping=Mock(return_value=confirmed),
            ),
        )
        return (
            NS(context=context, actor="actor"),
            NS(
                status=NS(value="BLOCKED"),
                workspace_id="workspace",
                application_id="application",
            ),
            issue,
            validation,
        )

    def call(self, fixture, application, phase="product-status"):
        with patch.object(
            review,
            "run_local_operation_sync",
            side_effect=lambda operation, *a, **kw: operation(*a, **kw),
        ):
            return review.confirm_manufacturing_recipe(
                fixture, application, phase, expected_population=(32, 16, 16)
            )

    def assert_no_confirmation(self, fixture):
        fixture.context.row_inclusion_reviews.confirm_current.assert_not_called()
        fixture.context.mapping_workspace.submit_current.assert_not_called()
        fixture.context.run_planning.confirm_application_mapping.assert_not_called()

    def test_reviewed_fields_submit_exact_warning_fingerprints(self):
        for phase, model, field in (
            ("product-status", "product.template", "active"),
            ("boms", "mrp.bom", "product_qty"),
        ):
            with self.subTest(phase=phase):
                fixture, app, issue, _ = self.fixture(model=model, field=field)
                self.assertEqual(self.call(fixture, app, phase).status.value, "READY")
                self.assertEqual(
                    fixture.context.mapping_workspace.submit_current.call_args.kwargs[
                        "warning_acknowledgements"
                    ],
                    (mapping_issue_fingerprint(issue),),
                )

    def test_nonblocking_native_default_advice_is_retained_without_acknowledgement(
        self,
    ):
        fixture, app, issue, validation = self.fixture()
        notice = MappingValidationIssue(
            "MAPPING_ODOO_PRODUCT_FIELD_DEFERRED",
            "advisory",
            "/datasets/0",
            "Odoo supplies this field when it creates the product",
            "",
            dataset_id="dataset",
            target_model="product.template",
            target_field="store_by",
        )
        validation.issues += (notice,)
        self.assertEqual(self.call(fixture, app).status.value, "READY")
        self.assertEqual(validation.issues, (issue, notice))
        self.assertEqual(
            fixture.context.mapping_workspace.submit_current.call_args.kwargs[
                "warning_acknowledgements"
            ],
            (mapping_issue_fingerprint(issue),),
        )

    def test_initial_product_recipe_accepts_valid_native_default_advice(self):
        fixture, app, _issue, validation = self.fixture(
            severity="advisory",
            code="MAPPING_ODOO_PRODUCT_FIELD_DEFERRED",
            field="store_by",
            status="VALID",
        )
        app.status.value = "READY"
        self.assertIs(self.call(fixture, app, "products"), app)
        self.assertEqual(len(validation.issues), 1)
        self.assert_no_confirmation(fixture)

    def test_unexpected_field_model_or_code_never_confirms(self):
        for change in (
            {"field": "name"},
            {"model": "mrp.bom"},
            {"code": "MAPPING_REQUIRED_VALUE_MISSING"},
            {"field": None},
        ):
            with self.subTest(change=change):
                fixture, app, _, _ = self.fixture(**change)
                with self.assertRaises(PracticeQualificationError):
                    self.call(fixture, app)
                self.assert_no_confirmation(fixture)

    def test_invalid_or_error_mapping_never_confirms(self):
        for change in ({"status": "INVALID"}, {"severity": "error"}):
            with self.subTest(change=change):
                fixture, app, _, _ = self.fixture(**change)
                with self.assertRaises(PracticeQualificationError):
                    self.call(fixture, app)
                self.assert_no_confirmation(fixture)

    def test_wrong_phase_or_population_never_confirms(self):
        for phase, population in (
            ("categories", (32, 16, 16)),
            ("product-status", (32, 15, 17)),
        ):
            with self.subTest(phase=phase, population=population):
                fixture, app, _, _ = self.fixture(population=population)
                with self.assertRaises(PracticeQualificationError):
                    self.call(fixture, app, phase)
                self.assert_no_confirmation(fixture)

    def test_unknown_application_blocker_never_confirms(self):
        fixture, app, _, _ = self.fixture()
        fixture.context.run_planning.repository.list_issues.return_value += (
            NS(code="RECIPE_TARGET_PERMISSION_CHANGED", blocks=True),
        )
        with self.assertRaises(PracticeQualificationError):
            self.call(fixture, app)
        self.assert_no_confirmation(fixture)

    def test_stale_validation_or_fabricated_field_owner_never_confirms(self):
        for condition in ("stale", "owner"):
            with self.subTest(condition=condition):
                fixture, app, _, validation = self.fixture()
                if condition == "stale":
                    validation.mapping_content_hash = "different"
                else:
                    fixture.context.mapping_workspace.mappings.get_mapping_revision.return_value.definition.datasets[
                        0
                    ].target_model = "other.model"
                with self.assertRaises(PracticeQualificationError):
                    self.call(fixture, app)
                self.assert_no_confirmation(fixture)

    def test_ready_application_needs_no_working_draft_or_new_confirmation(self):
        fixture, app, _, _ = self.fixture()
        app.status.value = "READY"
        fixture.context.mapping_workspace.mappings.get_mapping_working_draft.return_value = None
        self.assertIs(self.call(fixture, app), app)
        fixture.context.mapping_workspace.mappings.get_mapping_working_draft.assert_not_called()
        self.assert_no_confirmation(fixture)

    def test_ready_label_never_hides_invalid_current_mapping(self):
        fixture, app, _, _ = self.fixture(status="INVALID")
        app.status.value = "READY"
        with self.assertRaises(PracticeQualificationError):
            self.call(fixture, app)
        self.assert_no_confirmation(fixture)


if __name__ == "__main__":
    main()
