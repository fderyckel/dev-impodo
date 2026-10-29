from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from impodo.application.workspace.mapping.order_service import MatchingOrderService
from impodo.domain.mapping.contracts import (
    ConstantBusinessReference,
    ConstantReferenceComponent,
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    RelationshipValueSource,
    ResolverOrigin,
)
from impodo.domain.matching_order import (
    MatchingIdentityCheckStatus,
    MatchingIdentityChoiceFilter,
    MatchingIdentityNotCheckableReason,
    MatchingOrderCheckStatus,
    MatchingOrderRelationshipOutcome,
)
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.relationship_health import RelationshipHealthNotCheckableReason
from impodo.domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
    SchemaGovernance,
)
from impodo.domain.shared.access import (
    LOCAL_ACTOR,
    Actor,
    ActorIdentity,
    AuthorizationError,
    Capability,
    CapabilityAuthorizationPolicy,
)
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
)
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.workspace.contracts import (
    MappingWorkingDraft,
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)


class _Keys:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def source_key_tuples(self, workspace_id, dataset_id, source_column_keys):
        self.calls.append(("relationship", workspace_id, dataset_id, source_column_keys))
        return self.rows[(dataset_id, source_column_keys)]

    def source_identity_key_tuples(
        self,
        workspace_id,
        dataset_id,
        source_column_keys,
        row_inclusion,
    ):
        self.calls.append(("identity", workspace_id, dataset_id, source_column_keys))
        return self.rows[(dataset_id, source_column_keys)]

    def source_included_key_tuples(
        self,
        workspace_id,
        dataset_id,
        source_column_keys,
        row_inclusion,
    ):
        self.calls.append(
            ("relationship", workspace_id, dataset_id, source_column_keys)
        )
        return self.rows[(dataset_id, source_column_keys)]


class _Repository:
    def __init__(self) -> None:
        self.check = None
        self.protected = ""
        self.attempts = []

    def get_preference(self, _workspace_id):
        return None

    def update_check_attempt(self, _workspace_id, attempt):
        self.attempts.append(attempt)

    def publish_check(
        self,
        _workspace_id,
        check,
        *,
        protected_snapshot_json,
        actor,
    ):
        self.check = check
        self.protected = protected_snapshot_json
        return MatchingOrderCheckStatus.SUCCEEDED

    def fail_check(self, _workspace_id, attempt, *, actor):
        raise AssertionError(attempt.failure_message)


class MatchingOrderLiveCheckTests(unittest.TestCase):
    def test_identity_choice_review_requires_protected_evidence_capability(
        self,
    ) -> None:
        evidence = _evidence()
        service = MatchingOrderService(
            _Repository(),
            CapabilityAuthorizationPolicy(),
        )
        mapping_only_actor = Actor(
            identity=ActorIdentity(
                issuer="test",
                subject_id="mapping-only",
                display_name="Mapping operator",
            ),
            capabilities=frozenset({Capability.MAPPING_EDIT}),
        )

        with self.assertRaises(AuthorizationError):
            service.identity_choice_review_page(
                evidence.workspace_id,
                evidence.selection,
                evidence.schema,
                evidence.governance,
                evidence.draft,
                MatchingIdentityChoiceFilter(),
                page_size=100,
                actor=mapping_only_actor,
            )

    def test_bounded_plan_removes_edge_only_when_all_keys_are_in_odoo(self) -> None:
        evidence = _evidence()
        keys = _Keys(
            {
                ("dataset:bom", ("column:article",)): (("P001",), ("P002",)),
                ("dataset:article", ("column:article-code",)): (
                    ("P001",),
                    ("P002",),
                ),
            }
        )
        repository = _Repository()
        service = MatchingOrderService(
            repository,
            CapabilityAuthorizationPolicy(),
            keys,
        )
        local = service.recommend(
            evidence.selection,
            evidence.schema,
            evidence.draft.definition,
            tie_break_order=("dataset:bom", "dataset:article"),
        )
        prepared = service.prepare_live_check(
            evidence.workspace_id,
            evidence.selection,
            evidence.schema,
            evidence.governance,
            evidence.draft,
            local,
            tie_break_order=("dataset:bom", "dataset:article"),
            actor=LOCAL_ACTOR,
        )
        captured_requests = []

        def reader(requirements):
            captured_requests.append(requirements)
            metadata = MetadataSnapshot(
                fingerprint=evidence.fingerprint,
                models={
                    "product.template": ModelMetadata(
                        model="product.template",
                        description="Product",
                        fields={
                            "ref": FieldMetadata(name="ref", type="char")
                        },
                    )
                },
            )
            return metadata, RecordSnapshot(
                fingerprint=evidence.fingerprint,
                records={
                    "product.template": (
                        TargetRecord("product.template", 71, {"ref": "P001"}),
                        TargetRecord("product.template", 72, {"ref": "P002"}),
                    )
                },
                requested_fields={"product.template": ("ref",)},
            )

        service._run_live_check(prepared, reader)

        self.assertEqual(len(captured_requests), 1)
        plan = captured_requests[0]
        self.assertEqual(
            plan.metadata_requests[0].fields,
            ("ref",),
        )
        self.assertEqual(len(plan.record_requests), 2)
        self.assertEqual(
            plan.record_requests[0].domain,
            ("|", ["ref", "=ilike", "P001"], ["ref", "=ilike", "P002"]),
        )
        self.assertEqual(len(keys.calls), 2)
        self.assertEqual(
            repository.check.relationship_results[0].outcome,
            MatchingOrderRelationshipOutcome.TARGET,
        )
        relationship_health = repository.check.relationship_health_results[0]
        self.assertTrue(relationship_health.ready)
        self.assertEqual(relationship_health.source_row_count, 2)
        self.assertEqual(relationship_health.target_count, 2)
        self.assertEqual(
            repository.check.ordered_dataset_ids,
            ("dataset:bom", "dataset:article"),
        )
        identities = {
            item.dataset_id: item
            for item in repository.check.identity_results
        }
        self.assertEqual(
            identities["dataset:bom"].not_checkable_reason,
            MatchingIdentityNotCheckableReason.UNCONFIRMED_MATCHING_RULE,
        )
        self.assertEqual(
            identities["dataset:article"].status,
            MatchingIdentityCheckStatus.CHECKED,
        )
        self.assertEqual(
            identities["dataset:article"].expected_existing_count,
            2,
        )
        self.assertEqual(identities["dataset:article"].blocked_count, 0)
        self.assertNotIn("P001", repository.check.to_json())
        self.assertNotIn('"id":71', repository.check.to_json())
        self.assertIn("P001", repository.protected)
        self.assertIn('"id":71', repository.protected)

    def test_relational_identity_scope_is_proved_with_grouped_reads(self) -> None:
        evidence = _evidence()
        bom, article = evidence.draft.definition.datasets
        relationship = bom.relationships[0]
        scoped_bom = replace(
            bom,
            source_identity_column_keys=("column:bom", "column:article"),
            target_scope=(
                IdentityComponentMapping(
                    source_column_keys=("column:article",),
                    target_fields=("product_tmpl_id",),
                    resolver=relationship.resolver,
                ),
            ),
        )
        evidence.draft = replace(
            evidence.draft,
            definition=replace(
                evidence.draft.definition,
                datasets=(scoped_bom, article),
            ),
        )
        evidence.governance = replace(
            evidence.governance,
            business_keys=(
                *evidence.governance.business_keys,
                BusinessKeyDefinition(
                    key_id="bom:code-product",
                    model="mrp.bom",
                    key_fields=("code",),
                    scope_fields=("product_tmpl_id",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
        )
        evidence.draft = replace(
            evidence.draft,
            definition=replace(
                evidence.draft.definition,
                schema_hash=evidence.governance.content_hash,
            ),
        )
        keys = _Keys(
            {
                (
                    "dataset:bom",
                    ("column:bom", "column:article"),
                ): (("B001", "P001"), ("B002", "P002")),
                ("dataset:article", ("column:article-code",)): (
                    ("P001",),
                    ("P002",),
                ),
            }
        )
        repository = _Repository()
        service = MatchingOrderService(
            repository,
            CapabilityAuthorizationPolicy(),
            keys,
        )
        local = service.recommend(
            evidence.selection,
            evidence.schema,
            evidence.draft.definition,
        )
        prepared = service.prepare_live_check(
            evidence.workspace_id,
            evidence.selection,
            evidence.schema,
            evidence.governance,
            evidence.draft,
            local,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(len(keys.calls), 2)
        self.assertEqual(
            {item.model: item.fields for item in prepared.requirements.metadata_requests},
            {
                "mrp.bom": ("code", "product_tmpl_id"),
                "product.template": ("ref",),
            },
        )
        bom_request = next(
            item
            for item in prepared.requirements.record_requests
            if item.model == "mrp.bom"
        )
        self.assertIn(
            ["product_tmpl_id.ref", "=", "P001"],
            bom_request.domain,
        )

        def reader(_requirements):
            metadata = MetadataSnapshot(
                fingerprint=evidence.fingerprint,
                models={
                    "mrp.bom": ModelMetadata(
                        model="mrp.bom",
                        description="BOM",
                        fields={
                            "code": FieldMetadata(name="code", type="char"),
                            "product_tmpl_id": FieldMetadata(
                                name="product_tmpl_id",
                                type="many2one",
                                relation="product.template",
                            ),
                        },
                    ),
                    "product.template": ModelMetadata(
                        model="product.template",
                        description="Product",
                        fields={"ref": FieldMetadata(name="ref", type="char")},
                    ),
                },
            )
            records = RecordSnapshot(
                fingerprint=evidence.fingerprint,
                records={
                    "mrp.bom": (
                        TargetRecord(
                            "mrp.bom",
                            81,
                            {"code": "B001", "product_tmpl_id": [71, "P001"]},
                        ),
                    ),
                    "product.template": (
                        TargetRecord("product.template", 71, {"ref": "P001"}),
                    ),
                },
                requested_fields={
                    "mrp.bom": ("code", "product_tmpl_id"),
                    "product.template": ("ref",),
                },
            )
            return metadata, records

        service._run_live_check(prepared, reader)

        results = {
            item.dataset_id: item for item in repository.check.identity_results
        }
        self.assertEqual(
            results["dataset:bom"].status,
            MatchingIdentityCheckStatus.CHECKED,
        )
        self.assertEqual(results["dataset:bom"].expected_existing_count, 1)
        self.assertEqual(results["dataset:bom"].expected_new_count, 1)
        self.assertEqual(results["dataset:bom"].blocked_count, 0)
        self.assertNotIn("P001", repository.check.to_json())
        self.assertIn('"target_field":"product_tmpl_id"', repository.protected)
        self.assertIn('"unresolved_identity_choices":[]', repository.protected)

        bom_probe = next(
            item
            for item in prepared.identity_probes
            if item.dataset_id == "dataset:bom"
        )
        target_only_probe = replace(
            bom_probe,
            relational_components=(
                replace(
                    bom_probe.relational_components[0],
                    resolver_origin=ResolverOrigin.TARGET_CATALOG,
                ),
            ),
        )
        service._run_live_check(
            replace(
                prepared,
                check_id=str(uuid4()),
                identity_probes=tuple(
                    target_only_probe
                    if item.dataset_id == target_only_probe.dataset_id
                    else item
                    for item in prepared.identity_probes
                ),
            ),
            reader,
        )

        unresolved = {
            item.dataset_id: item for item in repository.check.identity_results
        }
        self.assertEqual(
            unresolved["dataset:bom"].not_checkable_reason,
            MatchingIdentityNotCheckableReason.RELATIONAL_IDENTITY_UNRESOLVED,
        )
        self.assertIn('"cause":"missing_target"', repository.protected)
        self.assertIn('"key":["P002"]', repository.protected)

    def test_nested_identity_and_many2one_health_use_grouped_bottom_up_reads(
        self,
    ) -> None:
        evidence = _evidence()
        original_bom, original_article = evidence.draft.definition.datasets
        company_source = _source(
            "dataset:company",
            "companies",
            ("column:company-code",),
        )
        article_source = _source(
            "dataset:article",
            "articles",
            ("column:article-code", "column:article-company"),
        )
        bom_source = _source(
            "dataset:bom",
            "boms",
            ("column:bom", "column:article", "column:bom-company"),
        )
        evidence.selection = replace(
            evidence.selection,
            datasets=(bom_source, article_source, company_source),
        )
        company_field = SchemaField(
            name="company_id",
            label="Company",
            type="many2one",
            required=False,
            readonly=False,
            relation="res.company",
            relation_field=None,
            selection=(),
        )
        company_code = SchemaField(
            name="code",
            label="Code",
            type="char",
            required=False,
            readonly=False,
            relation=None,
            relation_field=None,
            selection=(),
        )
        product_model, bom_model = evidence.schema.models
        evidence.schema = replace(
            evidence.schema,
            models=(
                replace(
                    product_model,
                    fields=(*product_model.fields, company_field),
                ),
                bom_model,
                SchemaModel("res.company", "Company", (company_code,)),
            ),
        )
        company_mapping = DatasetMapping(
            dataset_id=company_source.dataset_id,
            target_model="res.company",
            source_identity_column_keys=("column:company-code",),
            target_identity=(
                IdentityComponentMapping(
                    source_column_keys=("column:company-code",),
                    target_fields=("code",),
                ),
            ),
        )
        company_resolver = RelationshipResolver(
            origin=ResolverOrigin.TARGET_CATALOG,
            model="res.company",
            key_mappings=(
                ReferenceKeyMapping("column:article-company", "code"),
            ),
        )
        article_mapping = replace(
            original_article,
            source_identity_column_keys=(
                "column:article-code",
                "column:article-company",
            ),
            target_scope=(
                IdentityComponentMapping(
                    source_column_keys=("column:article-company",),
                    target_fields=("company_id",),
                    resolver=company_resolver,
                ),
            ),
        )
        product_resolver = RelationshipResolver(
            origin=ResolverOrigin.TARGET_THEN_DATASET,
            dataset_id=article_source.dataset_id,
            model="product.template",
            key_mappings=(
                ReferenceKeyMapping("column:article", "ref"),
            ),
            scope_mappings=(
                ReferenceKeyMapping("column:bom-company", "company_id"),
            ),
        )
        bom_mapping = replace(
            original_bom,
            source_identity_column_keys=(
                "column:bom",
                "column:article",
                "column:bom-company",
            ),
            target_scope=(
                IdentityComponentMapping(
                    source_column_keys=(
                        "column:article",
                        "column:bom-company",
                    ),
                    target_fields=("product_tmpl_id",),
                    resolver=product_resolver,
                ),
            ),
            relationships=(
                RelationshipMapping(
                    target_field="product_tmpl_id",
                    kind="many2one",
                    source_column_keys=(
                        "column:article",
                        "column:bom-company",
                    ),
                    resolver=product_resolver,
                    required=True,
                ),
            ),
        )
        evidence.governance = replace(
            evidence.governance,
            permitted_models=("mrp.bom", "product.template", "res.company"),
            business_keys=(
                BusinessKeyDefinition(
                    key_id="company:code",
                    model="res.company",
                    key_fields=("code",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
                BusinessKeyDefinition(
                    key_id="product:ref-company",
                    model="product.template",
                    key_fields=("ref",),
                    scope_fields=("company_id",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
                BusinessKeyDefinition(
                    key_id="bom:code-product",
                    model="mrp.bom",
                    key_fields=("code",),
                    scope_fields=("product_tmpl_id",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
        )
        evidence.draft = replace(
            evidence.draft,
            definition=replace(
                evidence.draft.definition,
                schema_hash=evidence.governance.content_hash,
                datasets=(bom_mapping, article_mapping, company_mapping),
            ),
        )
        nested_row_count = 1_000
        nested_bom_rows = tuple(
            (f"B{index:04d}", f"P{index:04d}", "BE")
            for index in range(nested_row_count)
        )
        nested_article_rows = tuple(
            (f"P{index:04d}", "BE") for index in range(nested_row_count)
        )
        keys = _Keys(
            {
                (
                    "dataset:bom",
                    ("column:bom", "column:article", "column:bom-company"),
                ): nested_bom_rows,
                (
                    "dataset:article",
                    ("column:article-code", "column:article-company"),
                ): nested_article_rows,
                ("dataset:company", ("column:company-code",)): (("BE",),),
            }
        )
        repository = _Repository()
        service = MatchingOrderService(
            repository,
            CapabilityAuthorizationPolicy(),
            keys,
        )
        local = service.recommend(
            evidence.selection,
            evidence.schema,
            evidence.draft.definition,
        )
        prepared = service.prepare_live_check(
            evidence.workspace_id,
            evidence.selection,
            evidence.schema,
            evidence.governance,
            evidence.draft,
            local,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(len(keys.calls), 3)
        self.assertEqual(
            {item.model: item.fields for item in prepared.requirements.metadata_requests},
            {
                "mrp.bom": ("code", "product_tmpl_id"),
                "product.template": ("company_id", "ref"),
                "res.company": ("code",),
            },
        )
        self.assertLess(len(prepared.requirements.record_requests), 30)
        product_request = next(
            item
            for item in prepared.requirements.record_requests
            if item.model == "product.template"
            and any("company_id.code" in str(term) for term in item.domain)
        )
        self.assertIn("company_id.code", str(product_request.domain))

        def reader(
            _requirements,
            *,
            include_company=True,
            include_products=True,
            company_code_value="BE",
        ):
            metadata = MetadataSnapshot(
                fingerprint=evidence.fingerprint,
                models={
                    "mrp.bom": ModelMetadata(
                        model="mrp.bom",
                        description="BOM",
                        fields={
                            "code": FieldMetadata(name="code", type="char"),
                            "product_tmpl_id": FieldMetadata(
                                name="product_tmpl_id",
                                type="many2one",
                                relation="product.template",
                            ),
                        },
                    ),
                    "product.template": ModelMetadata(
                        model="product.template",
                        description="Product",
                        fields={
                            "company_id": FieldMetadata(
                                name="company_id",
                                type="many2one",
                                relation="res.company",
                            ),
                            "ref": FieldMetadata(name="ref", type="char"),
                        },
                    ),
                    "res.company": ModelMetadata(
                        model="res.company",
                        description="Company",
                        fields={"code": FieldMetadata(name="code", type="char")},
                    ),
                },
            )
            records = RecordSnapshot(
                fingerprint=evidence.fingerprint,
                records={
                    "mrp.bom": (
                        TargetRecord(
                            "mrp.bom",
                            81,
                            {"code": "B0000", "product_tmpl_id": [71, "P0000"]},
                        ),
                    ),
                    "product.template": (
                        tuple(
                            TargetRecord(
                                "product.template",
                                71 + index,
                                {
                                    "ref": f"P{index:04d}",
                                    "company_id": [91, company_code_value],
                                },
                            )
                            for index in range(nested_row_count)
                        )
                        if include_products
                        else ()
                    ),
                    "res.company": (
                        (
                            TargetRecord(
                                "res.company",
                                91,
                                {"code": company_code_value},
                            ),
                        )
                        if include_company
                        else ()
                    ),
                },
                requested_fields={
                    "mrp.bom": ("code", "product_tmpl_id"),
                    "product.template": ("company_id", "ref"),
                    "res.company": ("code",),
                },
            )
            return metadata, records

        service._run_live_check(prepared, reader)
        results = {
            item.dataset_id: item for item in repository.check.identity_results
        }
        self.assertEqual(
            results["dataset:bom"].status,
            MatchingIdentityCheckStatus.CHECKED,
        )
        self.assertEqual(results["dataset:bom"].expected_existing_count, 1)
        self.assertEqual(
            results["dataset:bom"].expected_new_count,
            nested_row_count - 1,
        )
        relationship_health = repository.check.relationship_health_results[0]
        self.assertTrue(relationship_health.ready)
        self.assertEqual(relationship_health.target_count, nested_row_count)
        self.assertEqual(relationship_health.incoming_count, 0)

        service._run_live_check(
            replace(prepared, check_id=str(uuid4())),
            lambda requirements: reader(requirements, include_products=False),
        )
        incoming_health = repository.check.relationship_health_results[0]
        self.assertTrue(incoming_health.ready)
        self.assertEqual(incoming_health.target_count, 0)
        self.assertEqual(incoming_health.incoming_count, nested_row_count)

        service._run_live_check(
            replace(prepared, check_id=str(uuid4())),
            lambda requirements: reader(
                requirements,
                company_code_value="be",
            ),
        )
        case_health = repository.check.relationship_health_results[0]
        self.assertFalse(case_health.ready)
        self.assertEqual(case_health.case_mismatch_count, nested_row_count)

        service._run_live_check(
            replace(prepared, check_id=str(uuid4())),
            lambda requirements: reader(requirements, include_company=False),
        )
        unresolved = {
            item.dataset_id: item for item in repository.check.identity_results
        }
        self.assertEqual(
            unresolved["dataset:bom"].not_checkable_reason,
            MatchingIdentityNotCheckableReason.RELATIONAL_IDENTITY_UNRESOLVED,
        )
        missing_health = repository.check.relationship_health_results[0]
        self.assertFalse(missing_health.ready)
        self.assertEqual(missing_health.missing_count, nested_row_count)
        self.assertIn(
            '"target_field":"product_tmpl_id.company_id"',
            repository.protected,
        )
        self.assertIn('"related_model":"res.company"', repository.protected)

    def test_recursive_identity_cycle_stops_before_source_or_odoo_reads(self) -> None:
        evidence = _evidence()
        bom, article = evidence.draft.definition.datasets
        product_model, bom_model = evidence.schema.models
        parent_field = SchemaField(
            name="parent_id",
            label="Parent product",
            type="many2one",
            required=False,
            readonly=False,
            relation="product.template",
            relation_field=None,
            selection=(),
        )
        evidence.schema = replace(
            evidence.schema,
            models=(
                replace(
                    product_model,
                    fields=(*product_model.fields, parent_field),
                ),
                bom_model,
            ),
        )
        recursive_resolver = RelationshipResolver(
            origin=ResolverOrigin.TARGET_CATALOG,
            model="product.template",
            key_mappings=(
                ReferenceKeyMapping("column:article-code", "ref"),
            ),
            scope_mappings=(
                ReferenceKeyMapping("column:article-code", "parent_id"),
            ),
        )
        article = replace(
            article,
            target_scope=(
                IdentityComponentMapping(
                    source_column_keys=(
                        "column:article-code",
                        "column:article-code",
                    ),
                    target_fields=("parent_id",),
                    resolver=recursive_resolver,
                ),
            ),
        )
        evidence.governance = replace(
            evidence.governance,
            business_keys=(
                BusinessKeyDefinition(
                    key_id="product:recursive",
                    model="product.template",
                    key_fields=("ref",),
                    scope_fields=("parent_id",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
        )
        evidence.draft = replace(
            evidence.draft,
            definition=replace(
                evidence.draft.definition,
                schema_hash=evidence.governance.content_hash,
                datasets=(
                    replace(
                        bom,
                        relationships=(
                            RelationshipMapping(
                                target_field="product_tmpl_id",
                                kind="many2one",
                                source_column_keys=("column:article",),
                                resolver=recursive_resolver,
                            ),
                        ),
                    ),
                    article,
                ),
            ),
        )
        keys = _Keys({})
        service = MatchingOrderService(
            _Repository(),
            CapabilityAuthorizationPolicy(),
            keys,
        )
        local = service.recommend(
            evidence.selection,
            evidence.schema,
            evidence.draft.definition,
        )
        prepared = service.prepare_live_check(
            evidence.workspace_id,
            evidence.selection,
            evidence.schema,
            evidence.governance,
            evidence.draft,
            local,
            actor=LOCAL_ACTOR,
        )

        article_result = next(
            item
            for item in prepared.identity_results
            if item.dataset_id == "dataset:article"
        )
        self.assertEqual(
            article_result.not_checkable_reason,
            MatchingIdentityNotCheckableReason.INDIRECT_IDENTITY,
        )
        self.assertEqual(
            prepared.relationship_health_results[0].not_checkable_reason,
            RelationshipHealthNotCheckableReason.RELATED_IDENTITY_UNAVAILABLE,
        )
        self.assertEqual(keys.calls, [])
        self.assertEqual(prepared.requirements.record_requests, ())

    def test_schema_change_stops_before_relationship_classification(self) -> None:
        evidence = _evidence()
        keys = _Keys(
            {
                ("dataset:bom", ("column:article",)): (("P001",),),
                ("dataset:article", ("column:article-code",)): (("P001",),),
            }
        )
        repository = _Repository()
        service = MatchingOrderService(
            repository,
            CapabilityAuthorizationPolicy(),
            keys,
        )
        local = service.recommend(
            evidence.selection,
            evidence.schema,
            evidence.draft.definition,
        )
        prepared = service.prepare_live_check(
            evidence.workspace_id,
            evidence.selection,
            evidence.schema,
            evidence.governance,
            evidence.draft,
            local,
            actor=LOCAL_ACTOR,
        )

        service._run_live_check(
            prepared,
            lambda _requirements: (
                MetadataSnapshot(
                    fingerprint=evidence.fingerprint,
                    models={
                        "product.template": ModelMetadata(
                            model="product.template",
                            description="Product",
                            fields={
                                "ref": FieldMetadata(
                                    name="ref",
                                    type="char",
                                    required=True,
                                )
                            },
                        )
                    },
                ),
                RecordSnapshot(
                    fingerprint=evidence.fingerprint,
                    records={"product.template": ()},
                    requested_fields={"product.template": ("ref",)},
                ),
            ),
        )

        self.assertTrue(repository.check.schema_changed)
        self.assertEqual(repository.check.relationship_results, ())
        self.assertEqual(repository.check.unchecked_relationship_count, 1)
        self.assertEqual(
            repository.check.ordered_dataset_ids,
            local.ordered_dataset_ids,
        )
        identities = {
            item.dataset_id: item
            for item in repository.check.identity_results
        }
        self.assertEqual(
            identities["dataset:article"].not_checkable_reason,
            MatchingIdentityNotCheckableReason.ODOO_SCHEMA_CHANGED,
        )

    def test_target_only_relationship_reports_missing_and_required_blank_rows(self) -> None:
        evidence = _evidence()
        original = evidence.draft.definition.datasets[0].relationships[0]
        relationship = replace(
            original,
            resolver=replace(
                original.resolver,
                origin=ResolverOrigin.TARGET_CATALOG,
                dataset_id=None,
            ),
            required=True,
        )
        evidence.draft = _replace_relationship(evidence.draft, relationship)
        keys = _Keys(
            {
                ("dataset:bom", ("column:article",)): (
                    ("P001",),
                    ("P404",),
                    (None,),
                ),
                ("dataset:article", ("column:article-code",)): (("P001",),),
            }
        )
        repository = _Repository()
        service = MatchingOrderService(
            repository,
            CapabilityAuthorizationPolicy(),
            keys,
        )
        local = service.recommend(
            evidence.selection,
            evidence.schema,
            evidence.draft.definition,
        )
        prepared = service.prepare_live_check(
            evidence.workspace_id,
            evidence.selection,
            evidence.schema,
            evidence.governance,
            evidence.draft,
            local,
            actor=LOCAL_ACTOR,
        )

        service._run_live_check(
            prepared,
            lambda _requirements: _product_snapshot(
                evidence,
                records=(TargetRecord("product.template", 71, {"ref": "P001"}),),
            ),
        )

        result = repository.check.relationship_health_results[0]
        self.assertEqual(result.target_count, 1)
        self.assertEqual(result.missing_count, 1)
        self.assertEqual(result.blank_row_count, 1)
        self.assertEqual(result.blocked_count, 2)
        self.assertIsNone(result.dependency_dataset_id)

    def test_incoming_only_relationship_never_adds_an_odoo_relationship_read(self) -> None:
        evidence = _evidence()
        original = evidence.draft.definition.datasets[0].relationships[0]
        relationship = replace(
            original,
            resolver=replace(
                original.resolver,
                origin=ResolverOrigin.DATASET,
                model=None,
            ),
        )
        evidence.draft = _replace_relationship(evidence.draft, relationship)
        keys = _Keys(
            {
                ("dataset:bom", ("column:article",)): (
                    ("P001",),
                    ("P002",),
                    ("P404",),
                ),
                ("dataset:article", ("column:article-code",)): (
                    ("P001",),
                    ("P002",),
                    ("P002",),
                ),
            }
        )
        repository = _Repository()
        service = MatchingOrderService(
            repository,
            CapabilityAuthorizationPolicy(),
            keys,
        )
        local = service.recommend(
            evidence.selection,
            evidence.schema,
            evidence.draft.definition,
        )
        prepared = service.prepare_live_check(
            evidence.workspace_id,
            evidence.selection,
            evidence.schema,
            evidence.governance,
            evidence.draft,
            local,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(len(prepared.requirements.record_requests), 1)
        self.assertEqual(
            prepared.requirements.record_requests[0].domain,
            (["ref", "in", ["P001", "P002"]],),
        )
        service._run_live_check(
            prepared,
            lambda _requirements: _product_snapshot(evidence, records=()),
        )

        result = repository.check.relationship_health_results[0]
        self.assertEqual(result.incoming_count, 1)
        self.assertEqual(result.ambiguous_count, 1)
        self.assertEqual(result.missing_count, 1)
        self.assertEqual(result.target_count, 0)

    def test_constant_existing_relationship_counts_every_included_owner_row(self) -> None:
        evidence = _evidence()
        original = evidence.draft.definition.datasets[0].relationships[0]
        relationship = replace(
            original,
            source_column_keys=(),
            resolver=RelationshipResolver(
                origin=ResolverOrigin.TARGET_CATALOG,
                model="product.template",
            ),
            value_source=RelationshipValueSource.CONSTANT_EXISTING,
            constant_reference=ConstantBusinessReference(
                key_values=(ConstantReferenceComponent("ref", "P001"),),
            ),
            required=True,
        )
        evidence.draft = _replace_relationship(evidence.draft, relationship)
        keys = _Keys(
            {
                ("dataset:bom", ()): ((), ()),
                ("dataset:article", ("column:article-code",)): (("P001",),),
            }
        )
        repository = _Repository()
        service = MatchingOrderService(
            repository,
            CapabilityAuthorizationPolicy(),
            keys,
        )
        local = service.recommend(
            evidence.selection,
            evidence.schema,
            evidence.draft.definition,
        )
        prepared = service.prepare_live_check(
            evidence.workspace_id,
            evidence.selection,
            evidence.schema,
            evidence.governance,
            evidence.draft,
            local,
            actor=LOCAL_ACTOR,
        )
        service._run_live_check(
            prepared,
            lambda _requirements: _product_snapshot(
                evidence,
                records=(TargetRecord("product.template", 71, {"ref": "P001"}),),
            ),
        )

        result = repository.check.relationship_health_results[0]
        self.assertEqual(result.source_row_count, 2)
        self.assertEqual(result.populated_choice_count, 2)
        self.assertEqual(result.target_count, 2)
        self.assertTrue(result.ready)

    def test_many2many_list_is_expanded_without_a_source_row_request_loop(self) -> None:
        evidence = _evidence()
        bom_model = evidence.schema.models[1]
        relationship_field = replace(
            bom_model.fields[1],
            type="many2many",
        )
        evidence.schema = replace(
            evidence.schema,
            models=(
                evidence.schema.models[0],
                replace(
                    bom_model,
                    fields=(bom_model.fields[0], relationship_field),
                ),
            ),
        )
        original = evidence.draft.definition.datasets[0].relationships[0]
        relationship = replace(
            original,
            kind="many2many",
            resolver=replace(
                original.resolver,
                origin=ResolverOrigin.TARGET_CATALOG,
                dataset_id=None,
            ),
            required=True,
        )
        evidence.draft = _replace_relationship(evidence.draft, relationship)
        keys = _Keys(
            {
                ("dataset:bom", ("column:article",)): (("P001; P002; P001",),),
                ("dataset:article", ("column:article-code",)): (("P001",),),
            }
        )
        repository = _Repository()
        service = MatchingOrderService(
            repository,
            CapabilityAuthorizationPolicy(),
            keys,
        )
        local = service.recommend(
            evidence.selection,
            evidence.schema,
            evidence.draft.definition,
        )
        prepared = service.prepare_live_check(
            evidence.workspace_id,
            evidence.selection,
            evidence.schema,
            evidence.governance,
            evidence.draft,
            local,
            actor=LOCAL_ACTOR,
        )
        service._run_live_check(
            prepared,
            lambda _requirements: _product_snapshot(
                evidence,
                records=(
                    TargetRecord("product.template", 71, {"ref": "P001"}),
                    TargetRecord("product.template", 72, {"ref": "P002"}),
                ),
            ),
        )

        result = repository.check.relationship_health_results[0]
        self.assertEqual(result.source_row_count, 1)
        self.assertEqual(result.populated_choice_count, 2)
        self.assertEqual(result.target_count, 2)
        self.assertEqual(
            prepared.requirements.record_requests[0].domain,
            ("|", ["ref", "=ilike", "P001"], ["ref", "=ilike", "P002"]),
        )


class _Evidence:
    pass


def _evidence():
    result = _Evidence()
    result.workspace_id = str(uuid4())
    result.fingerprint = TargetFingerprint(
        target_hash="sha256:" + "7" * 64,
        connection_mode="REMOTE",
        database="odoo",
        odoo_version="19.0",
        snapshot_timestamp="2026-09-10T10:00:00+00:00",
    )
    now = datetime.now(UTC)
    article = _source(
        "dataset:article",
        "articles",
        ("column:article-code",),
    )
    bom = _source(
        "dataset:bom",
        "boms",
        ("column:bom", "column:article"),
    )
    result.selection = SourceSelection(
        selection_id=str(uuid4()),
        version=1,
        data_version_id=str(uuid4()),
        created_at=now,
        created_by="Data manager",
        datasets=(bom, article),
        content_hash="sha256:" + "a" * 64,
    )
    char = SchemaField(
        name="ref",
        label="Reference",
        type="char",
        required=False,
        readonly=False,
        relation=None,
        relation_field=None,
        selection=(),
    )
    result.schema = OdooSchemaCatalog(
        workspace_id=result.workspace_id,
        policy_hash="sha256:" + "1" * 64,
        captured_at=now,
        captured_by="Data manager",
        connection_mode="REMOTE",
        database="odoo",
        odoo_version="19.0",
        models=(
            SchemaModel("product.template", "Product", (char,)),
            SchemaModel(
                "mrp.bom",
                "BOM",
                (
                    SchemaField(
                        name="code",
                        label="Code",
                        type="char",
                        required=False,
                        readonly=False,
                        relation=None,
                        relation_field=None,
                        selection=(),
                    ),
                    SchemaField(
                        name="product_tmpl_id",
                        label="Product",
                        type="many2one",
                        required=False,
                        readonly=False,
                        relation="product.template",
                        relation_field=None,
                        selection=(),
                    ),
                ),
            ),
        ),
        content_hash="sha256:" + "2" * 64,
        origin=SchemaOrigin.LIVE_API,
        read_credential_binding_hash="sha256:" + "3" * 64,
        read_principal_hash="sha256:" + "4" * 64,
        read_permission_hash="sha256:" + "5" * 64,
        read_context_hash="sha256:" + "6" * 64,
        connection_target_hash=result.fingerprint.target_hash,
    )
    result.governance = SchemaGovernance(
        governance_id=str(uuid4()),
        version=1,
        workspace_id=result.workspace_id,
        catalog_hash=result.schema.content_hash,
        permitted_models=("mrp.bom", "product.template"),
        business_keys=(
            BusinessKeyDefinition(
                key_id="product:ref",
                model="product.template",
                key_fields=("ref",),
                status=BusinessKeyStatus.CONFIRMED,
            ),
        ),
        recorded_at=now,
        recorded_by="Data manager",
    )
    definition = MappingDefinition(
        mapping_id=str(uuid4()),
        source_selection_hash=result.selection.content_hash,
        schema_hash=result.governance.content_hash,
        datasets=(
            DatasetMapping(
                dataset_id=bom.dataset_id,
                target_model="mrp.bom",
                source_identity_column_keys=("column:bom",),
                target_identity=(
                    IdentityComponentMapping(
                        source_column_keys=("column:bom",),
                        target_fields=("code",),
                    ),
                ),
                relationships=(
                    RelationshipMapping(
                        target_field="product_tmpl_id",
                        kind="many2one",
                        source_column_keys=("column:article",),
                        resolver=RelationshipResolver(
                            origin=ResolverOrigin.TARGET_THEN_DATASET,
                            dataset_id=article.dataset_id,
                            model="product.template",
                            key_mappings=(
                                ReferenceKeyMapping(
                                    source_column_key="column:article",
                                    target_field="ref",
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            DatasetMapping(
                dataset_id=article.dataset_id,
                target_model="product.template",
                source_identity_column_keys=("column:article-code",),
                target_identity=(
                    IdentityComponentMapping(
                        source_column_keys=("column:article-code",),
                        target_fields=("ref",),
                    ),
                ),
            ),
        ),
    )
    result.draft = MappingWorkingDraft(
        mapping_id=definition.mapping_id,
        version=1,
        workspace_id=result.workspace_id,
        base_mapping_version=None,
        definition=definition,
        updated_at=now,
        updated_by="Data manager",
    )
    return result


def _replace_relationship(draft, relationship):
    definition = replace(
        draft.definition,
        datasets=(
            replace(draft.definition.datasets[0], relationships=(relationship,)),
            draft.definition.datasets[1],
        ),
    )
    return replace(draft, definition=definition)


def _product_snapshot(evidence, *, records):
    metadata = MetadataSnapshot(
        fingerprint=evidence.fingerprint,
        models={
            "product.template": ModelMetadata(
                model="product.template",
                description="Product",
                fields={"ref": FieldMetadata(name="ref", type="char")},
            )
        },
    )
    return metadata, RecordSnapshot(
        fingerprint=evidence.fingerprint,
        records={"product.template": tuple(records)},
        requested_fields={"product.template": ("ref",)},
    )


def _source(
    dataset_id: str,
    name: str,
    columns: tuple[str, ...],
) -> SourceDataset:
    return SourceDataset(
        dataset_id=dataset_id,
        name=name,
        source=FileSourceBinding(
            file_id=f"file:{dataset_id}",
            table_key=name,
            source_sha256="sha256:" + "a" * 64,
            catalog_hash="sha256:" + "b" * 64,
            encoding="utf-8",
            delimiter=",",
            header_row=1,
        ),
        row_count=2,
        columns=tuple(
            SourceDatasetColumn(index, key, key, "string")
            for index, key in enumerate(columns, start=1)
        ),
    )


if __name__ == "__main__":
    unittest.main()
