"""Exercise saved-data field policies through the existing transfer runtime."""

from __future__ import annotations

import unittest
from dataclasses import fields, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from impodo.application.destination_matching_service import (
    DestinationMatchingService,
    DestinationMatchKeyChoice,
)
from impodo.application.transfer_execution_service import (
    compile_transfer_execution_snapshot,
)
from impodo.application.transfer_order_service import TransferOrderService
from impodo.application.transfer_preflight_service import TransferPreflightService
from impodo.application.transfer_review_service import TransferReviewService
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.odoo_provenance import OdooOriginBatch, OdooRelationshipOriginColumn
from impodo.domain.odoo_restore import (
    RestoreCreateDefault,
    RestoreFieldRule,
    RestoreLinkPolicy,
    RestoreValuePolicy,
    compile_odoo_restore,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
    target_record_binding_hash,
)
from impodo.domain.workspace.contracts import (
    SchemaField,
    SchemaModel,
    SourceDatasetColumn,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.transfer_review import (
    TransferReviewApproval,
    TransferReviewPackage,
)
from tests.application.workspace import test_transfer_execution as fixture_module
from tests.application.workspace.test_destination_matching import (
    BINDING_HASH,
    _identity,
    _SourceValues,
)


def _qualified(field):
    return replace(
        field,
        stored=True,
        computed=False,
        has_inverse=False,
        related=False,
        company_dependent=False,
        exportable=True,
    )


def _scenario(
    major=20,
    *,
    rules=(),
    rename=False,
    rename_relation=False,
    removed=False,
    default=False,
    current_links=(7,),
    saved_links=(7, 8),
    reference_only=False,
):
    fixture = fixture_module.TransferExecutionCompilerTests()
    fixture.setUp()
    product, uom = fixture.selection.datasets
    fixture.source_values.rows[(uom.dataset_id, ("uom-name",))] = (
        ("Unit",),
        ("Kilogram",),
    )
    product = replace(
        product,
        columns=(
            *product.columns,
            SourceDatasetColumn(3, "x_note", "product-note", "TEXT"),
        ),
    )
    fixture.selection = replace(fixture.selection, datasets=(product, uom))
    fixture.rows[product.dataset_id] = tuple(
        replace(row, values={**row.values, "x_note": f"Saved note {row.number}"})
        for row in fixture.rows[product.dataset_id]
    )
    batch = fixture.origins[product.dataset_id][0]
    fixture.origins[product.dataset_id] = (
        replace(
            batch,
            relationships=tuple(
                replace(column, values=(saved_links, (8,)))
                if column.field_name == "alternate_uom_ids"
                else column
                for column in batch.relationships
            ),
        ),
    )
    workspace = replace(
        fixture.workspace,
        saved_restore_generation_id=str(uuid4()),
        destination_verified_odoo_version=f"{major}.0",
    )
    schema = replace(
        fixture.schema,
        odoo_version=f"{major}.0",
        models=tuple(
            replace(
                model,
                fields=tuple(_qualified(field) for field in model.fields)
                + (
                    (
                        _qualified(
                            SchemaField(
                                "x_note",
                                "Saved note",
                                "char",
                                False,
                                False,
                                None,
                                None,
                                (),
                            )
                        ),
                    )
                    if model.name == "product.template"
                    else ()
                ),
            )
            for model in fixture.schema.models
        ),
    )
    target_models = []
    for model in schema.models:
        model_fields = list(model.fields)
        if model.name == "product.template":
            if rename_relation:
                model_fields = [
                    replace(field, name="x_other_uom_ids")
                    if field.name == "alternate_uom_ids"
                    else field
                    for field in model_fields
                ]
            if rename:
                model_fields = [
                    replace(field, name="x_label") if field.name == "x_note" else field
                    for field in model_fields
                ]
            if removed:
                model_fields = [
                    field for field in model_fields if field.name != "x_note"
                ]
            if default:
                model_fields.append(
                    _qualified(
                        SchemaField(
                            "x_required",
                            "Required setup",
                            "char",
                            True,
                            False,
                            None,
                            None,
                            (),
                        )
                    )
                )
        target_models.append(replace(model, fields=tuple(model_fields)))
    if reference_only:
        target_models = [
            replace(
                model,
                fields=tuple(replace(field, readonly=True) for field in model.fields),
            )
            if model.name == "uom.uom"
            else model
            for model in target_models
        ]
    plans = {
        source.name: compile_odoo_restore(
            source,
            target,
            source_version=f"{major}.0",
            destination_version=f"{major}.0",
            identity_fields=("default_code",)
            if source.name == "product.template"
            else ("name",),
            rules=rules if source.name == "product.template" else (),
            defaults=(RestoreCreateDefault("x_required", "Configured"),)
            if default and source.name == "product.template"
            else (),
        )
        for source, target in zip(schema.models, target_models, strict=True)
        if not (reference_only and source.name == "uom.uom")
    }
    captured = []
    metadata_names = {field.name for field in fields(FieldMetadata)}

    def reader(*args):
        metadata, records = fixture.reader(*args)
        metadata = replace(
            metadata,
            fingerprint=replace(metadata.fingerprint, odoo_version=f"{major}.0"),
            models={
                target.name: replace(
                    metadata.models[target.name],
                    fields={
                        field.name: FieldMetadata(
                            **{name: getattr(field, name) for name in metadata_names}
                        )
                        for field in target.fields
                    },
                )
                for target in target_models
            },
        )
        records = replace(
            records,
            fingerprint=replace(records.fingerprint, odoo_version=f"{major}.0"),
            records={
                **records.records,
                "product.template": tuple(
                    replace(
                        record,
                        values={
                            **record.values,
                            "name": "Current name",
                            "x_label" if rename else "x_note": "Current note",
                            "uom_id": 7,
                            "x_other_uom_ids"
                            if rename_relation
                            else "alternate_uom_ids": current_links,
                            **({"x_required": "Existing setup"} if default else {}),
                        },
                    )
                    for record in records.records["product.template"]
                ),
            },
        )
        if reference_only:
            records = replace(
                records,
                records={
                    **records.records,
                    "uom.uom": (
                        *records.records["uom.uom"],
                        TargetRecord("uom.uom", 88, {"name": "Kilogram"}),
                    ),
                },
            )
        captured.append((records, args[3]))
        return metadata, records

    choices = (
        DestinationMatchKeyChoice(product.dataset_id, "product-code"),
        DestinationMatchKeyChoice(
            uom.dataset_id,
            "uom-name",
            destination_handling="reference_only" if reference_only else "transfer",
        ),
    )
    options = {
        "api_key": "fixture-key",
        "credential_binding_hash": BINDING_HASH,
        "read_identity": _identity(workspace),
        "reader": reader,
        "recorded_by": "Data manager",
        "source_origins": fixture.origins,
        "restore_plans": plans,
    }
    match = DestinationMatchingService(fixture.source_values).check(
        workspace, fixture.selection, schema, choices, **options
    )
    matched = replace(workspace, destination_match_plan=match)
    order = TransferOrderService().build(matched, match, recorded_by="Data manager")
    ordered = replace(matched, transfer_order_plan=order)
    package = TransferReviewService().build(
        ordered,
        match,
        order,
        run_id=str(uuid4()),
        data_version_id=fixture.selection.data_version_id,
        built_by=LOCAL_ACTOR.identity,
        restore_plans=plans,
    )
    approval = TransferReviewApproval.approve(
        package,
        approval_id=str(uuid4()),
        actor=LOCAL_ACTOR,
        approved_at=datetime.now(UTC),
    )
    approved = replace(
        ordered, transfer_review_package=package, transfer_review_approval=approval
    )
    report = TransferPreflightService().build(
        approved,
        package,
        approval,
        match,
        match,
        recorded_by=LOCAL_ACTOR.identity,
        restore_plans=plans,
    )
    return SimpleNamespace(
        fixture=fixture,
        workspace=replace(approved, transfer_preflight_report=report),
        schema=schema,
        plans=plans,
        choices=choices,
        options=options,
        match=match,
        package=package,
        approval=approval,
        report=report,
        records=captured[-1][0],
        requests=captured[-1][1],
    )


def _compile(scenario, *, plans=None):
    fixture = scenario.fixture
    return compile_transfer_execution_snapshot(
        scenario.workspace,
        fixture.selection,
        scenario.schema,
        scenario.package,
        scenario.report,
        scenario.match,
        scenario.records,
        source_rows=fixture.rows,
        source_origins=fixture.origins,
        source_snapshots=fixture.snapshots,
        source_manifest_hashes=fixture.manifests,
        restore_plans=scenario.plans if plans is None else plans,
    )


def _coa_scenario(major: int, *, destination_company="Company A", identity_aliases=()):
    """Keep one company-scoped native account surface and explicit setup reuse."""

    fixture = fixture_module.TransferExecutionCompilerTests()
    fixture.setUp()
    account_dataset, company_dataset = fixture.selection.datasets
    account_dataset = replace(
        account_dataset,
        name="Accounts",
        source=replace(account_dataset.source, model="account.account"),
        columns=tuple(
            SourceDatasetColumn(index, name, f"account-{name}", "TEXT")
            for index, name in enumerate(
                ("code", "name", "account_type", "reconcile"), start=1
            )
        ),
    )
    company_dataset = replace(
        company_dataset,
        name="Companies",
        row_count=1,
        source=replace(company_dataset.source, model="res.company"),
        columns=(SourceDatasetColumn(1, "name", "company-name", "TEXT"),),
    )
    fixture.selection = replace(
        fixture.selection, datasets=(account_dataset, company_dataset)
    )
    scalar = lambda name, kind="char", required=False: _qualified(
        SchemaField(name, name, kind, required, False, None, None, ())
    )
    account = SchemaModel(
        "account.account",
        "Account",
        (
            replace(
                scalar("code"),
                stored=False,
                computed=True,
                has_inverse=True,
                searchable=True,
            ),
            replace(scalar("name", required=True), translated=True),
            replace(
                scalar("account_type", "selection", True),
                selection=(("asset_receivable", "Receivable"), ("income", "Income")),
            ),
            scalar("reconcile", "boolean"),
            replace(scalar("company_ids", "many2many", True), relation="res.company"),
        ),
    )
    company = SchemaModel("res.company", "Company", (scalar("name", required=True),))
    schema = replace(
        fixture.schema, odoo_version=f"{major}.0", models=(account, company)
    )
    chosen_company = OdooCompanySelection(
        fixture.workspace.destination_verified_target_hash, 22, destination_company
    )
    workspace = replace(
        fixture.workspace,
        destination_company=chosen_company,
        saved_restore_generation_id=str(uuid4()),
        destination_verified_odoo_version=f"{major}.0",
    )
    identity = replace(
        _identity(workspace),
        readable_models=("account.account", "res.company"),
        context_hash=chosen_company.context_hash,
    )
    fingerprint = TargetFingerprint(
        workspace.destination_verified_target_hash,
        "REMOTE",
        "destination",
        f"{major}.0",
        datetime.now(UTC).isoformat(),
    )
    metadata_fields = {field.name for field in fields(FieldMetadata)}
    metadata = MetadataSnapshot(
        fingerprint,
        {
            "account.account": ModelMetadata(
                "account.account",
                "Account",
                {
                    field.name: FieldMetadata(
                        **{name: getattr(field, name) for name in metadata_fields}
                    )
                    for field in account.fields
                },
            ),
            "res.company": ModelMetadata(
                "res.company",
                "Company",
                {
                    "name": FieldMetadata(
                        "name",
                        "char",
                        "Company",
                        required=True,
                        readonly=True,
                        stored=True,
                        exportable=True,
                        company_dependent=False,
                    )
                },
            ),
        },
    )
    fixture.origins = {
        account_dataset.dataset_id: (
            OdooOriginBatch(
                1,
                (101, 102),
                (fixture.now, fixture.now),
                (
                    OdooRelationshipOriginColumn(
                        "company_ids", "many2many", "res.company", ((7,), (7,))
                    ),
                ),
            ),
        ),
        company_dataset.dataset_id: (OdooOriginBatch(1, (7,), (fixture.now,)),),
    }
    fixture.rows = {
        account_dataset.dataset_id: (
            replace(
                fixture.rows[next(iter(fixture.rows))][0],
                values={
                    "code": "1000",
                    "name": "Receivables",
                    "account_type": "asset_receivable",
                    "reconcile": True,
                },
            ),
            replace(
                fixture.rows[next(iter(fixture.rows))][1],
                values={
                    "code": "7000",
                    "name": "Sales",
                    "account_type": "income",
                    "reconcile": False,
                },
            ),
        ),
        company_dataset.dataset_id: (
            replace(
                fixture.rows[next(iter(fixture.rows))][0], values={"name": "Company A"}
            ),
        ),
    }
    source_values = _SourceValues(
        {
            (account_dataset.dataset_id, "account-code"): (
                {"value": "1000", "count": 1},
                {"value": "7000", "count": 1},
            ),
            (company_dataset.dataset_id, "company-name"): (
                {"value": "Company A", "count": 1},
            ),
        },
        {
            (account_dataset.dataset_id, "account-code"): ("1000", "7000"),
            (company_dataset.dataset_id, "company-name"): ("Company A",),
            (company_dataset.dataset_id, ("company-name",)): (("Company A",),),
        },
    )
    records = RecordSnapshot(
        fingerprint,
        {
            "account.account": (
                TargetRecord(
                    "account.account",
                    41,
                    {
                        "code": "1000",
                        "name": "Existing receivables",
                        "account_type": "asset_receivable",
                        "reconcile": False,
                        "company_ids": (22,),
                    },
                ),
            ),
            "res.company": (
                TargetRecord("res.company", 22, {"name": destination_company}),
            )
            if destination_company == "Company A" or identity_aliases
            else (),
        },
        requested_fields={},
    )
    plans = {
        "account.account": compile_odoo_restore(
            account,
            account,
            source_version=f"{major}.0",
            destination_version=f"{major}.0",
            identity_fields=("code",),
        )
    }
    choices = (
        DestinationMatchKeyChoice(account_dataset.dataset_id, "account-code"),
        DestinationMatchKeyChoice(
            company_dataset.dataset_id,
            "company-name",
            destination_handling="reference_only",
            identity_aliases=identity_aliases,
        ),
    )

    observed_records = []

    def reader(_destination, _key, _metadata, requests):
        observed = replace(
            records,
            requested_fields={request.model: request.fields for request in requests},
        )
        observed_records.append(observed)
        return metadata, observed

    match = DestinationMatchingService(source_values).check(
        workspace,
        fixture.selection,
        schema,
        choices,
        api_key="fixture",
        credential_binding_hash=BINDING_HASH,
        read_identity=identity,
        reader=reader,
        recorded_by="Data manager",
        source_origins=fixture.origins,
        restore_plans=plans,
    )
    return SimpleNamespace(
        fixture=fixture,
        workspace=workspace,
        schema=schema,
        plans=plans,
        match=match,
        records=observed_records[-1],
    )


class SavedOdooTransferTests(unittest.TestCase):
    major = 20

    def _scenario(self, major=None, **options):
        return _scenario(self.major if major is None else major, **options)

    def test_same_major_restore_field_policies_reach_real_execution(self) -> None:
        for major in (19, 20):
            for policy in (
                RestoreValuePolicy.USE_SAVED,
                RestoreValuePolicy.KEEP_DESTINATION,
                RestoreValuePolicy.FILL_EMPTY,
            ):
                with self.subTest(major=major, policy=policy):
                    scenario = self._scenario(
                        major, rules=(RestoreFieldRule("name", policy=policy),)
                    )
                    self.assertTrue(scenario.report.ready)
                    snapshot = _compile(scenario)
                    existing = next(
                        row
                        for row in snapshot.rows
                        if row.target_model == "product.template"
                        and row.source_row == 1
                    )
                    new = next(
                        row
                        for row in snapshot.rows
                        if row.target_model == "product.template"
                        and row.source_row == 2
                    )
                    names = {intent.field: intent for intent in existing.fields}
                    if policy is RestoreValuePolicy.USE_SAVED:
                        self.assertEqual(names["name"].value, "Product 1")
                    else:
                        self.assertNotIn("name", names)
                    self.assertEqual(
                        next(
                            intent.value
                            for intent in new.fields
                            if intent.field == "name"
                        ),
                        "Product 2",
                    )
                    self.assertIn(
                        "name",
                        next(
                            request.fields
                            for request in scenario.requests
                            if request.model == "product.template"
                        ),
                    )

    def test_rename_uses_original_saved_column_and_approved_target_scope(self) -> None:
        scenario = self._scenario(
            rename=True, rules=(RestoreFieldRule("x_note", target_field="x_label"),)
        )
        self.assertTrue(scenario.report.ready)
        product = next(
            item
            for item in scenario.package.datasets
            if item.model == "product.template"
        )
        self.assertIn("x_label", product.scalar_write_fields)
        self.assertNotIn("x_note", product.scalar_write_fields)
        snapshot = _compile(scenario)
        product = next(
            row
            for row in snapshot.rows
            if row.target_model == "product.template" and row.source_row == 1
        )
        self.assertEqual(
            next(
                intent.value for intent in product.fields if intent.field == "x_label"
            ),
            "Saved note 1",
        )

    def test_removed_field_requires_explicit_exclusion(self) -> None:
        with self.assertRaisesRegex(
            WorkspaceError, "Resolve every selected restore field plan"
        ):
            self._scenario(removed=True)
        scenario = self._scenario(
            removed=True, rules=(RestoreFieldRule("x_note", excluded=True),)
        )
        self.assertTrue(scenario.report.ready)
        self.assertFalse(
            any(
                intent.field == "x_note"
                for row in _compile(scenario).rows
                if row.target_model == "product.template"
                for intent in row.fields
            )
        )

    def test_relationship_rename_keeps_original_protected_source_membership(
        self,
    ) -> None:
        scenario = self._scenario(
            rename_relation=True,
            rules=(
                RestoreFieldRule("alternate_uom_ids", target_field="x_other_uom_ids"),
            ),
        )
        self.assertTrue(scenario.report.ready)
        snapshot = _compile(scenario)
        row = next(
            row
            for row in snapshot.rows
            if row.target_model == "product.template" and row.source_row == 1
        )
        intent = next(
            intent for intent in row.fields if intent.field == "x_other_uom_ids"
        )
        self.assertEqual(
            {reference.key for reference in intent.value}, {("Unit",), ("Kilogram",)}
        )
        self.assertNotIn("alternate_uom_ids", {intent.field for intent in row.fields})

    def test_excluded_relationship_has_no_write_intent_or_reviewed_link(self) -> None:
        scenario = self._scenario(
            rules=(RestoreFieldRule("alternate_uom_ids", excluded=True),)
        )
        self.assertTrue(scenario.report.ready)
        self.assertNotIn(
            "alternate_uom_ids",
            {relation.field_name for relation in scenario.package.relationships},
        )
        self.assertFalse(
            any(
                intent.field == "alternate_uom_ids"
                for row in _compile(scenario).rows
                for intent in row.fields
            )
        )

    def test_review_binds_exact_policy_generation_and_round_trips(self) -> None:
        scenario = self._scenario()
        self.assertEqual(
            TransferReviewPackage.from_json(scenario.package.to_json()),
            scenario.package,
        )
        self.assertIsNotNone(scenario.package.restore_policy_hash)
        changed = {
            **scenario.plans,
            "product.template": replace(
                scenario.plans["product.template"],
                fields=tuple(
                    replace(field, policy=RestoreValuePolicy.KEEP_DESTINATION)
                    if field.source.name == "name"
                    else field
                    for field in scenario.plans["product.template"].fields
                ),
            ),
        }
        with self.assertRaisesRegex(WorkspaceError, "field policies differ"):
            _compile(scenario, plans=changed)
        with self.assertRaisesRegex(WorkspaceError, "field policies changed"):
            TransferPreflightService().build(
                scenario.workspace,
                scenario.package,
                scenario.approval,
                scenario.match,
                scenario.match,
                recorded_by=LOCAL_ACTOR.identity,
                restore_plans=changed,
            )
        with self.assertRaisesRegex(WorkspaceError, "field policies changed"):
            TransferPreflightService().build(
                replace(scenario.workspace, saved_restore_generation_id=str(uuid4())),
                scenario.package,
                scenario.approval,
                scenario.match,
                scenario.match,
                recorded_by=LOCAL_ACTOR.identity,
                restore_plans=scenario.plans,
            )

    def test_restore_policies_cannot_be_used_on_an_ordinary_transfer(self) -> None:
        scenario = self._scenario()
        with self.assertRaisesRegex(
            WorkspaceError, "generation and explicit field plans"
        ):
            DestinationMatchingService(scenario.fixture.source_values).check(
                replace(scenario.workspace, saved_restore_generation_id=None),
                scenario.fixture.selection,
                scenario.schema,
                scenario.choices,
                **scenario.options,
            )
        with self.assertRaisesRegex(
            WorkspaceError, "generation and explicit field plans"
        ):
            TransferPreflightService().build(
                scenario.workspace,
                scenario.package,
                scenario.approval,
                scenario.match,
                scenario.match,
                recorded_by=LOCAL_ACTOR.identity,
            )

    def test_writable_models_cannot_fall_back_to_unconfigured_transfer_policies(
        self,
    ) -> None:
        scenario = self._scenario()
        with self.assertRaisesRegex(WorkspaceError, "every writable record type"):
            DestinationMatchingService(scenario.fixture.source_values).check(
                scenario.workspace,
                scenario.fixture.selection,
                scenario.schema,
                scenario.choices,
                **{**scenario.options, "restore_plans": {}},
            )
        with self.assertRaisesRegex(WorkspaceError, "every writable record type"):
            _compile(scenario, plans={})

    def test_reused_readonly_models_need_no_write_policy_or_write_intents(self) -> None:
        scenario = self._scenario(reference_only=True)
        self.assertNotIn("uom.uom", scenario.plans)
        self.assertTrue(scenario.report.ready)
        snapshot = _compile(scenario)
        for row in snapshot.rows:
            if row.target_model == "uom.uom":
                self.assertEqual(row.disposition, "UNCHANGED")
                self.assertEqual(row.fields, ())

    def test_new_required_default_uses_existing_protected_create_provider(self) -> None:
        scenario = self._scenario(default=True)
        self.assertTrue(scenario.report.ready)
        decision = next(
            item
            for item in scenario.match.create_field_decisions
            if item.field_name == "x_required"
        )
        self.assertTrue(decision.reviewed)
        self.assertEqual(decision.provider_kind, "fixed_value")
        rows = [
            row
            for row in _compile(scenario).rows
            if row.target_model == "product.template"
        ]
        existing = next(row for row in rows if row.source_row == 1)
        self.assertNotIn("x_required", {intent.field for intent in existing.fields})
        intent = next(
            intent
            for row in rows
            if row.disposition == "CREATE"
            for intent in row.fields
            if intent.field == "x_required"
        )
        self.assertEqual(intent.action, "SET_PROTECTED")
        self.assertEqual(intent.protected_value_hash, decision.value_hash)

    def test_many2many_keep_and_fill_do_not_replace_current_links(self) -> None:
        for rule in (
            RestoreFieldRule("alternate_uom_ids", link_policy=RestoreLinkPolicy.KEEP),
            RestoreFieldRule("alternate_uom_ids", policy=RestoreValuePolicy.FILL_EMPTY),
        ):
            with self.subTest(rule=rule):
                scenario = self._scenario(rules=(rule,), current_links=(999,))
                row = next(
                    row
                    for row in _compile(scenario).rows
                    if row.target_model == "product.template" and row.source_row == 1
                )
                self.assertEqual(
                    next(
                        intent.action
                        for intent in row.fields
                        if intent.field == "alternate_uom_ids"
                    ),
                    "OMIT",
                )

    def test_many2many_add_requires_fresh_portable_current_members(self) -> None:
        rule = RestoreFieldRule(
            "alternate_uom_ids", link_policy=RestoreLinkPolicy.ADD_MISSING
        )
        with self.assertRaisesRegex(
            WorkspaceError, "fresh evidence for every current linked record"
        ):
            _compile(self._scenario(rules=(rule,), current_links=(999,)))
        scenario = self._scenario(rules=(rule,), saved_links=(8,))
        row = next(
            row
            for row in _compile(scenario).rows
            if row.target_model == "product.template" and row.source_row == 1
        )
        intent = next(
            intent for intent in row.fields if intent.field == "alternate_uom_ids"
        )
        self.assertEqual(
            {reference.key for reference in intent.value}, {("Unit",), ("Kilogram",)}
        )

    def test_source_schema_and_population_hashes_are_not_rewritten(self) -> None:
        scenario = self._scenario(
            rules=(
                RestoreFieldRule("name", policy=RestoreValuePolicy.KEEP_DESTINATION),
            )
        )
        snapshot = _compile(scenario)
        self.assertEqual(
            scenario.match.source_schema_hash, scenario.schema.content_hash
        )
        self.assertEqual(
            scenario.match.source_selection_hash,
            scenario.fixture.selection.content_hash,
        )
        self.assertEqual(
            snapshot.staging_content_hash, scenario.fixture.selection.content_hash
        )


class SavedOdooAccountingTests(unittest.TestCase):
    """Qualify selected CoA inputs without claiming a complete accounting backup."""

    def test_native_account_inputs_and_company_links_compile_for_both_majors(
        self,
    ) -> None:
        for major in (19, 20):
            with self.subTest(major=major):
                scenario = _coa_scenario(major)
                self.assertTrue(scenario.match.ready)
                matched = replace(
                    scenario.workspace, destination_match_plan=scenario.match
                )
                order = TransferOrderService().build(
                    matched, scenario.match, recorded_by="Data manager"
                )
                ordered = replace(matched, transfer_order_plan=order)
                package = TransferReviewService().build(
                    ordered,
                    scenario.match,
                    order,
                    run_id=str(uuid4()),
                    data_version_id=scenario.fixture.selection.data_version_id,
                    built_by=LOCAL_ACTOR.identity,
                    restore_plans=scenario.plans,
                )
                approval = TransferReviewApproval.approve(
                    package,
                    approval_id=str(uuid4()),
                    actor=LOCAL_ACTOR,
                    approved_at=datetime.now(UTC),
                )
                approved = replace(
                    ordered,
                    transfer_review_package=package,
                    transfer_review_approval=approval,
                )
                report = TransferPreflightService().build(
                    approved,
                    package,
                    approval,
                    scenario.match,
                    scenario.match,
                    recorded_by=LOCAL_ACTOR.identity,
                    restore_plans=scenario.plans,
                )
                self.assertTrue(report.ready)
                scenario.workspace = replace(approved, transfer_preflight_report=report)
                scenario.package, scenario.report = package, report
                snapshot = _compile(scenario)
                new_account = next(
                    row
                    for row in snapshot.rows
                    if row.target_model == "account.account"
                    and row.disposition == "CREATE"
                )
                by_field = {intent.field: intent for intent in new_account.fields}
                self.assertEqual(by_field["code"].value, "7000")
                self.assertEqual(by_field["account_type"].value, "income")
                self.assertIs(by_field["reconcile"].value, False)
                company_link = by_field["company_ids"]
                self.assertEqual(company_link.related_identity_fields, ("name",))
                self.assertEqual(company_link.value[0].model, "res.company")
                self.assertEqual(company_link.value[0].key, ("Company A",))
                self.assertEqual(
                    company_link.target_binding_hashes,
                    (target_record_binding_hash("res.company", 22),),
                )
                self.assertNotEqual(
                    company_link.target_binding_hashes,
                    (target_record_binding_hash("res.company", 7),),
                )
                company_row = next(
                    row for row in snapshot.rows if row.target_model == "res.company"
                )
                self.assertEqual(company_row.disposition, "UNCHANGED")
                self.assertEqual(company_row.fields, ())

    def test_different_company_name_is_an_existing_setup_prerequisite(self) -> None:
        for major in (19, 20):
            with self.subTest(major=major):
                scenario = _coa_scenario(major, destination_company="Company B")
                self.assertFalse(scenario.match.ready)
                company = next(
                    item
                    for item in scenario.match.model_matches
                    if item.model == "res.company"
                )
                self.assertEqual(company.destination_handling, "reference_only")
                self.assertIn(
                    "DESTINATION_REUSE_RECORD_MISSING", company.blocking_reasons
                )
                self.assertTrue(
                    any(
                        relation.missing_related_record_count
                        for relation in scenario.match.relationship_matches
                    )
                )
                with self.assertRaises(WorkspaceError):
                    TransferOrderService().build(
                        replace(
                            scenario.workspace, destination_match_plan=scenario.match
                        ),
                        scenario.match,
                        recorded_by="Data manager",
                    )


class SavedOdoo19TransferTests(SavedOdooTransferTests):
    """Run the same complete restore contract against Odoo 19 metadata."""

    major = 19


if __name__ == "__main__":
    unittest.main()
