"""Build the shared customer exercises through normal application services.

It creates isolated TestClient applications only and never opens a browser or
connects to a live Odoo target. Pair with practice_customer_target.stock_fields
or pass the stateful target from that helper to ``author``.

Baseline P04 deliberately has three normal runs: headers, schedule, customers.
Stock Odoo creates a 100% zero-day child when a term is created. Schedule then
updates that captured child; it must not append another 100% child.
``incoming-immediate`` is a separate explicitly supplied overlay exercising a
new incoming term plus existing and new contact owners in the same load.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from impodo.application.workspace.derived_entities import (
    ExactJoinGroupAddition,
    ExactJoinGroupProjection,
    ExactJoinGroupRequest,
)
from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    IdentityComponentMapping,
    MappingTargetMode,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    RowInclusionCondition,
    RowInclusionJoin,
    RowInclusionMode,
    RowInclusionPolicy,
    ScalarFieldMapping,
    ScalarValueSource,
    SelectionConditionOperator,
)
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo_source_policy import odoo_source_policy_hash
from impodo.domain.recipe.value_rules import ScalarTransformPolicy, TextTransformStep
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.serialization import content_hash
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from tests.integration.web.test_fresh_data_controls import field as form_value
from tests.support.browser_scenarios import POST_HEADERS, _browser_schema
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture

COMPANY_NAMES = {"NORTH": "Morrow Assembly North", "SOUTH": "Morrow Assembly South"}
CONTACT_FILES = ("customers_core.csv", "party_details.csv", "addresses.csv")
PHASE_FILES = {
    "enrichment": CONTACT_FILES,
    "headers": ("payment_terms.csv",),
    "schedule": ("payment_terms.csv", "payment_term_lines.csv"),
    "customers": (*CONTACT_FILES, "payment_terms.csv"),
    "incoming-immediate": (*CONTACT_FILES, "payment_terms.csv"),
}


def _key(dataset, name):
    return next(
        column.stable_key for column in dataset.columns if column.source_name == name
    )


def _company_rows(dataset, company):
    return RowInclusionPolicy(
        mode=RowInclusionMode.MATCHING_ROWS,
        conditions=(
            RowInclusionCondition(
                str(uuid4()),
                _key(dataset, "CompanyCode"),
                SelectionConditionOperator.EQUALS,
                company,
            ),
        ),
    )


def _company_resolver(dataset):
    key = _key(dataset, "CompanyName")
    return RelationshipResolver(
        origin=ResolverOrigin.TARGET_CATALOG,
        model="res.company",
        key_mappings=(ReferenceKeyMapping(key, "name"),),
    )


def _constant(field, value, kind="string"):
    return ScalarFieldMapping(
        field,
        value_source=ScalarValueSource.CONSTANT,
        literal_value=value,
        value_type=kind,
    )


class PracticeCustomerWorkflow(HierarchyWorkflowFixture):
    """Use real source acceptance, joins, mapping, preparation, and Recipe setup.

    Execution/comparison deliberately remain outside this fixture. The calling
    test supplies the independent target and uses the actual preflight,
    execution, and reconciliation services after reopening the application.
    """

    def practice_files(
        self, root, *, phase, delivery="2026-04-15", source_overrides=None
    ):
        if phase not in PHASE_FILES:
            raise ValueError(f"Unknown customer practice phase: {phase}")
        directory = Path(root) / "deliveries" / delivery
        overrides = dict(source_overrides or {})
        unknown = set(overrides) - set(PHASE_FILES[phase])
        if unknown:
            raise ValueError(f"Overrides are outside this phase: {sorted(unknown)}")
        if phase == "incoming-immediate" and not {
            "payment_terms.csv",
            "customers_core.csv",
        }.issubset(overrides):
            raise ValueError(
                "The immediate exercise requires explicit reviewed term and owner overlays"
            )
        # The caller independently checks the immediate term's zero-day
        # schedule. This helper never changes the baseline NET45 rule.
        return {
            name.removesuffix(".csv"): overrides.get(
                name, (directory / name).read_bytes()
            )
            for name in PHASE_FILES[phase]
        }

    def install_practice_schema(self, workspace_id, *, target, major, mode, company):
        """Publish exact-major controlled metadata and select one company.

        ``target.fields`` comes from the independently reviewed stock field
        fixture. It is not inferred from expected output or from mappings.
        """
        if major not in (19, 20) or mode not in ("LOCAL", "REMOTE"):
            raise ValueError("This helper covers Odoo19/20 and LOCAL/REMOTE only")
        context, actor = self.context, self.actor
        state = context.queries.get(workspace_id)
        state = context.workspace_states.update_target(
            workspace_id,
            actor=actor,
            expected_revision=state.revision,
            odoo_connection_mode=mode,
            odoo_base_url="http://127.0.0.1:8069"
            if mode == "LOCAL"
            else "https://odoo.example.test",
            odoo_database=f"fictional_morrow_{major}",
            intended_applications=(),
            intended_models=(
                "res.partner",
                "account.payment.term",
                "account.payment.term.line",
            ),
        )
        target_hash = _browser_schema(state).fingerprint.target_hash
        selection = OdooCompanySelection(
            target_hash, target.company_ids[company], COMPANY_NAMES[company]
        )
        state = context.workspace_states.select_odoo_company(
            workspace_id,
            actor=actor,
            expected_revision=state.revision,
            selection=selection,
        )
        owner = context.target_credential_workspace(
            workspace_id, workspace_state=state, actor=actor
        )
        read = store_target_credential(
            context.secret_store,
            owner,
            TargetCredentialRole.READ,
            "fictional-morrow-read-key",
            persistent=False,
        )
        models = tuple(
            SchemaModel(
                model,
                model,
                tuple(
                    SchemaField(
                        name=name,
                        label=name,
                        type=metadata["type"],
                        required=metadata.get("required", False),
                        readonly=metadata.get("readonly", False),
                        relation=metadata.get("relation"),
                        relation_field=metadata.get("relation_field"),
                        selection=tuple(
                            tuple(choice) for choice in metadata.get("selection", ())
                        ),
                        digits=tuple(metadata["digits"])
                        if metadata.get("digits")
                        else None,
                    )
                    for name, metadata in fields.items()
                ),
            )
            for model, fields in target.fields.items()
        )
        policy_hash = odoo_source_policy_hash(major)
        self.assertIsNotNone(policy_hash)
        schema = OdooSchemaCatalog(
            workspace_id=workspace_id,
            policy_hash=policy_hash,
            captured_at=datetime.now(UTC),
            captured_by="Fictional practice operator",
            connection_mode=mode,
            database=state.odoo_database,
            odoo_version=f"{major}.0",
            models=models,
            content_hash=content_hash((major, [asdict(item) for item in models])),
            origin=SchemaOrigin.LIVE_API,
            read_credential_binding_hash=read.binding_hash,
            read_principal_hash=content_hash(("practice-principal", major, mode)),
            read_permission_hash=content_hash(("practice-permissions", major, mode)),
            read_context_hash=selection.context_hash,
            connection_target_hash=target_hash,
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id, schema, actor=actor
        )
        keys = (
            BusinessKeyDefinition(
                "practice:company-name",
                "res.company",
                ("name",),
                status=BusinessKeyStatus.CONFIRMED,
            ),
            BusinessKeyDefinition(
                "practice:partner-ref",
                "res.partner",
                ("ref",),
                status=BusinessKeyStatus.CONFIRMED,
            ),
            BusinessKeyDefinition(
                "practice:term-name-company",
                "account.payment.term",
                ("name",),
                scope_fields=("company_id",),
                status=BusinessKeyStatus.CONFIRMED,
            ),
            # This exercise has exactly one generated 100% line per term.
            # Multiple lines must become AMBIGUOUS rather than choosing one.
            BusinessKeyDefinition(
                "practice:sole-term-line",
                "account.payment.term.line",
                ("payment_id",),
                status=BusinessKeyStatus.CONFIRMED,
            ),
        )
        context.schema_workspace.govern(workspace_id, business_keys=keys, actor=actor)
        target.schema = schema
        target.target_hash = target_hash
        return schema

    def save_customer_combination(self, workspace_id):
        selection = self.context.queries.get_source_selection(workspace_id)
        by_name = {dataset.name: dataset for dataset in selection.datasets}
        primary, party, addresses = (
            by_name[name] for name in ("customers_core", "party_details", "addresses")
        )
        projected = (
            (primary, "CustomerRef", "CustomerRef"),
            (primary, "AccountNum", "AccountNum"),
            (primary, "Partition", "Partition"),
            (primary, "Party", "Party"),
            (primary, "CompanyCode", "CompanyCode"),
            (primary, "PaymentTermCode", "PaymentTermCode"),
            (primary, "RecId", "CustomerRecId"),
            (party, "Name", "Name"),
            (party, "Email", "Email"),
            (party, "Phone", "Phone"),
            (party, "RecId", "PartyRecId"),
            (addresses, "Street", "Street"),
            (addresses, "Postcode", "Postcode"),
            (addresses, "City", "City"),
            (addresses, "RecId", "AddressRecId"),
        )
        request = ExactJoinGroupRequest(
            output_dataset_name="customers_complete",
            primary_dataset_id=primary.dataset_id,
            primary_key_column_keys=(
                _key(primary, "Partition"),
                _key(primary, "Party"),
            ),
            key_value_types=("string", "string"),
            additions=(
                ExactJoinGroupAddition(
                    party.dataset_id, (_key(party, "Partition"), _key(party, "RecId"))
                ),
                ExactJoinGroupAddition(
                    addresses.dataset_id,
                    (_key(addresses, "Partition"), _key(addresses, "Party")),
                ),
            ),
            projections=tuple(
                ExactJoinGroupProjection(
                    dataset.dataset_id, _key(dataset, name), output
                )
                for dataset, name, output in projected
            ),
        )
        plan, preview = run_local_operation_sync(
            self.context.derived_entities.save_join_group,
            workspace_id,
            request,
            expected_parent_version=None,
            actor=self.actor,
        )
        self.assertTrue(preview.can_save)
        self.assertEqual(preview.reviews[-1].actual_output_rows, 18)
        effective = self.context.queries.get_mapping_source_selection(workspace_id)
        output = next(
            item for item in effective.datasets if item.name == "customers_complete"
        )
        self.assertEqual(output.row_count, 18)
        return output, plan

    def term_mapping(self, dataset, company, *, reference=False):
        code = _key(dataset, "CompanyCode")
        return DatasetMapping(
            dataset.dataset_id,
            "account.payment.term",
            mode=MappingTargetMode.REFERENCE if reference else MappingTargetMode.UPSERT,
            row_inclusion=_company_rows(dataset, company),
            source_identity_column_keys=(code, _key(dataset, "TermCode")),
            target_identity=(
                IdentityComponentMapping((_key(dataset, "Name"),), ("name",)),
            ),
            target_scope=(
                IdentityComponentMapping(
                    (_key(dataset, "CompanyName"),),
                    ("company_id",),
                    resolver=_company_resolver(dataset),
                ),
            ),
            fields=() if reference else (_constant("sequence", "10", "integer"),),
        )

    def customer_mapping(self, dataset, company, *, terms=None, customer_refs=()):
        row_policy = _company_rows(dataset, company)
        if customer_refs:
            prefix = "MA-N-" if company == "NORTH" else "MA-S-"
            self.assertTrue(
                all(reference.startswith(prefix) for reference in customer_refs)
            )
            row_policy = RowInclusionPolicy(
                mode=RowInclusionMode.MATCHING_ROWS,
                join=RowInclusionJoin.ANY,
                conditions=tuple(
                    RowInclusionCondition(
                        str(uuid4()),
                        _key(dataset, "CustomerRef"),
                        SelectionConditionOperator.EQUALS,
                        reference,
                    )
                    for reference in customer_refs
                ),
            )
        text = ScalarTransformPolicy(
            trim=True, collapse_whitespace=True, empty_as_null=True
        )
        phone = ScalarTransformPolicy(
            trim=True,
            empty_as_null=True,
            text_steps=(
                TextTransformStep(
                    kind="remove_separators_between_digits", characters="-. "
                ),
                TextTransformStep(
                    search_value="00",
                    replacement_value="+",
                    search_mode="starts_with",
                    replace_all=False,
                ),
            ),
        )
        fields = (
            ScalarFieldMapping(
                "name", _key(dataset, "Name"), transform=text, required_on_create=True
            ),
            ScalarFieldMapping(
                "email",
                _key(dataset, "Email"),
                transform=replace(text, case_mode="lowercase"),
                null_policy="equivalent",
            ),
            ScalarFieldMapping("phone", _key(dataset, "Phone"), transform=phone),
            ScalarFieldMapping("street", _key(dataset, "Street"), transform=text),
            ScalarFieldMapping("zip", _key(dataset, "Postcode"), transform=text),
            ScalarFieldMapping("city", _key(dataset, "City"), transform=text),
        )
        relationships = (
            ()
            if terms is None
            else (
                RelationshipMapping(
                    "property_payment_term_id",
                    "many2one",
                    (_key(dataset, "CompanyCode"), _key(dataset, "PaymentTermCode")),
                    resolver=RelationshipResolver(
                        ResolverOrigin.DATASET, dataset_id=terms.dataset_id
                    ),
                    categorical_policy=CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
                ),
            )
        )
        return DatasetMapping(
            dataset.dataset_id,
            "res.partner",
            row_inclusion=row_policy,
            source_identity_column_keys=(_key(dataset, "CustomerRef"),),
            target_identity=(
                IdentityComponentMapping((_key(dataset, "CustomerRef"),), ("ref",)),
            ),
            fields=fields,
            relationships=relationships,
        )

    def schedule_mapping(self, dataset, company, *, terms):
        parent_columns = (_key(dataset, "CompanyCode"), _key(dataset, "TermCode"))
        return DatasetMapping(
            dataset.dataset_id,
            "account.payment.term.line",
            row_inclusion=_company_rows(dataset, company),
            source_identity_column_keys=(_key(dataset, "LineKey"),),
            target_identity=(
                IdentityComponentMapping(
                    parent_columns,
                    ("payment_id",),
                    resolver=RelationshipResolver(
                        ResolverOrigin.DATASET, dataset_id=terms.dataset_id
                    ),
                ),
            ),
            fields=(
                replace(
                    _constant("value", "percent"),
                    categorical_policy=CategoricalCoveragePolicy.EXACT_TARGET_VALUE,
                ),
                _constant("value_amount", "100", "decimal"),
                replace(
                    _constant("delay_type", "days_after"),
                    categorical_policy=CategoricalCoveragePolicy.EXACT_TARGET_VALUE,
                ),
                ScalarFieldMapping(
                    "nb_days", _key(dataset, "Days"), value_type="integer"
                ),
            ),
        )

    def author(
        self,
        *,
        root,
        target,
        major,
        mode,
        company="NORTH",
        phase="customers",
        delivery="2026-04-15",
        source_overrides=None,
        customer_refs=(),
    ):
        files = self.practice_files(
            root, phase=phase, delivery=delivery, source_overrides=source_overrides
        )
        workspace_id = self.freeze_files(files)
        schema = self.install_practice_schema(
            workspace_id, target=target, major=major, mode=mode, company=company
        )
        selection = self.context.queries.get_source_selection(workspace_id)
        by_name = {dataset.name: dataset for dataset in selection.datasets}
        mappings = []
        terms = by_name.get("payment_terms")
        if terms is not None:
            mappings.append(self.term_mapping(terms, company))
        if phase == "schedule":
            mappings.append(
                self.schedule_mapping(
                    by_name["payment_term_lines"], company, terms=terms
                )
            )
        if phase in {"customers", "incoming-immediate", "enrichment"}:
            output, _plan = self.save_customer_combination(workspace_id)
            mappings.append(
                self.customer_mapping(
                    output, company, terms=terms, customer_refs=customer_refs
                )
            )
        revision = self.submit(workspace_id, tuple(mappings), confirm_rows=True)
        return workspace_id, schema, revision

    def publish_practice_recipe(
        self,
        workspace_id,
        *,
        label,
        business_purpose="Reuse reviewed Morrow customer exercise rules",
    ):
        owner = self.context.migration_workspaces.get(workspace_id, actor=self.actor)
        draft = self.context.recipe_publication.draft(
            project_id=owner.project_id,
            data_version_id=owner.data_version_id,
            workspace_id=workspace_id,
            actor=self.actor,
        )
        self.assertTrue(draft.can_publish, draft.issues)
        return run_local_operation_sync(
            self.context.recipe_publication.publish,
            project_id=owner.project_id,
            data_version_id=owner.data_version_id,
            workspace_id=workspace_id,
            display_name=label,
            business_purpose=business_purpose,
            actor=self.actor,
        )

    def apply_practice_recipe(
        self,
        publication,
        *,
        source_workspace_id,
        root,
        target,
        major,
        mode,
        company="NORTH",
        phase="customers",
        delivery="2026-05-15",
        source_overrides=None,
        run_label="Fresh Morrow customer exercise",
    ):
        context, actor = self.context, self.actor
        owner = context.migration_workspaces.get(source_workspace_id, actor=actor)
        setup = context.test_runs.start_setup(
            owner.project_id,
            expected_workspace_revision=context.migration_projects.get(
                owner.project_id, actor=actor
            ).optimistic_revision,
            recipe_revisions=(
                (publication.recipe.recipe_id, publication.revision.version),
            ),
            dependencies=(),
            label=run_label,
            export_as_of=delivery,
            operation_id=str(uuid4()),
            actor=actor,
        )
        url = f"/projects/{owner.project_id}/test-runs/{setup.run.migration_run_id}/fresh-data"
        page = self.client.get(url)
        files = self.practice_files(
            root, phase=phase, delivery=delivery, source_overrides=source_overrides
        )
        response = self.client.post(
            url + "/files",
            data={
                "csrf_token": form_value(page, "csrf_token"),
                "revision": form_value(page, "revision"),
            },
            files=[
                ("source_file", (name + ".csv", values, "text/csv"))
                for name, values in files.items()
            ],
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text)
        page = self.client.get(url)
        response = self.client.post(
            url + "/register",
            data={
                "csrf_token": form_value(page, "csrf_token"),
                "revision": form_value(page, "revision"),
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text)
        page = self.client.get(url)
        response = self.client.post(
            url + "/accept",
            data={
                "csrf_token": form_value(page, "csrf_token"),
                "parameter_revision": "",
                "warnings_acknowledged": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text)
        schema = self.install_practice_schema(
            setup.setup_workspace.workspace_id,
            target=target,
            major=major,
            mode=mode,
            company=company,
        )
        activated = run_local_operation_sync(
            context.test_runs.activate,
            owner.project_id,
            setup.run.migration_run_id,
            expected_workspace_revision=context.migration_projects.get(
                owner.project_id, actor=actor
            ).optimistic_revision,
            target_schema=schema,
            target_reference_bundle=None,
            credential_generation=schema.read_credential_binding_hash,
            operation_id=str(uuid4()),
            actor=actor,
        )
        self.assertEqual(len(activated.applications), 1)
        application = activated.applications[0]
        return application, context.queries.get_odoo_schema_catalog(
            application.workspace_id
        )
