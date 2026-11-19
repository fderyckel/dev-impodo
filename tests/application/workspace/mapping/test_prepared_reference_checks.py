"""Calculate linked references before bounded Stage 3 destination requests."""

from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from impodo.application.workspace.mapping.categorical_coverage import SourceKeyProjection
from impodo.application.workspace.mapping.order_service import (
    MatchingOrderService,
    _MatchingSourceTupleCache,
)
from impodo.domain.mapping.contracts import (
    IdentityComponentMapping,
    ReferenceKeyMapping,
    ResolverOrigin,
    RowInclusionPolicy,
)
from impodo.domain.mapping.reference_preparation import (
    ReferencePreparation,
    prepare_reference_value,
)
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.shared.access import LOCAL_ACTOR, CapabilityAuthorizationPolicy
from impodo.domain.shared.models import TargetRecord
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.composition.target_readers import _capture_mapping_supporting_values
from tests.application.workspace.mapping import (
    test_matching_order_live_check as fixtures,
)


def reference():
    return ReferenceKeyMapping(
        "column:article",
        "ref",
        ReferencePreparation(
            formula="concat(value, column_3)",
            bindings=(("column_3", "suffix"),),
        ),
    )


class PreparedReferenceCheckTests(TestCase):
    def test_dependency_projection_is_cached_without_mutating_original_values(self):
        keys = fixtures._Keys(
            {
                ("dataset:bom", ("column:article", "suffix")): (
                    ("P", "001"),
                    ("P", "002"),
                )
            }
        )
        cache = _MatchingSourceTupleCache(keys, {})
        prepared = cache.read(
            "workspace",
            "dataset:bom",
            ("column:article",),
            RowInclusionPolicy(),
            identity=False,
            reference_mappings=(reference(),),
        )
        raw = cache.read(
            "workspace",
            "dataset:bom",
            ("column:article",),
            RowInclusionPolicy(),
            identity=False,
        )
        self.assertEqual(prepared, (("P001",), ("P002",)))
        self.assertEqual(raw, (("P",), ("P",)))
        self.assertEqual(len(keys.calls), 1)

    def test_partial_prepared_reference_cannot_become_an_optional_blank(self):
        keys = fixtures._Keys(
            {("dataset:bom", ("column:article", "suffix")): (("P", None),)}
        )
        cache = _MatchingSourceTupleCache(
            keys, {}, source_column_names={"dataset:bom": {"suffix": "Article suffix"}},
        )
        with self.assertRaisesRegex(WorkspaceError, "“Article suffix” is blank in 1 included row"):
            cache.read(
                "workspace",
                "dataset:bom",
                ("column:article",),
                RowInclusionPolicy(),
                identity=False,
                reference_mappings=(reference(),),
            )

    def test_one_source_can_supply_independently_prepared_matching_components(self):
        keys = fixtures._Keys(
            {("dataset:bom", ("column:article", "suffix")): (("P", "001"),)}
        )
        first = reference()
        second = replace(
            first,
            target_field="company_ref",
            preparation=ReferencePreparation(
                formula='concat("company-", value)',
            ),
        )
        result = _MatchingSourceTupleCache(keys, {}).read(
            "workspace",
            "dataset:bom",
            ("column:article", "column:article"),
            RowInclusionPolicy(),
            identity=False,
            reference_mappings=(first, second),
        )
        self.assertEqual(result, (("P001", "company-P"),))
        self.assertEqual(len(keys.calls), 1)

    def test_repeated_raw_inputs_calculate_the_reference_once_per_projection(self):
        keys = fixtures._Keys(
            {
                ("dataset:bom", ("column:article", "suffix")): (
                    ("P", "001"),
                    ("P", "001"),
                )
            }
        )
        with patch(
            "impodo.application.workspace.mapping.prepared_references.prepare_reference_value",
            wraps=prepare_reference_value,
        ) as calculate:
            result = _MatchingSourceTupleCache(keys, {}).read(
                "workspace",
                "dataset:bom",
                ("column:article",),
                RowInclusionPolicy(),
                identity=False,
                reference_mappings=(reference(),),
            )
        self.assertEqual(result, (("P001",), ("P001",)))
        self.assertEqual(calculate.call_count, 1)

    def setup_check(self, major=20, *, identity=False):
        evidence = fixtures._evidence()
        evidence.schema = replace(evidence.schema, odoo_version=f"{major}.0")
        evidence.fingerprint = replace(evidence.fingerprint, odoo_version=f"{major}.0")
        original = evidence.draft.definition.datasets[0].relationships[0]
        relationship = replace(
            original,
            required=True,
            resolver=replace(
                original.resolver,
                origin=ResolverOrigin.TARGET_CATALOG,
                dataset_id=None,
                key_mappings=(reference(),),
            ),
        )
        evidence.draft = fixtures._replace_relationship(evidence.draft, relationship)
        bom, article = evidence.draft.definition.datasets
        if identity:
            bom = replace(
                bom,
                target_scope=(
                    IdentityComponentMapping(
                        ("column:article",),
                        ("product_tmpl_id",),
                        resolver=relationship.resolver,
                    ),
                ),
            )
            evidence.governance = replace(
                evidence.governance,
                business_keys=(
                    *evidence.governance.business_keys,
                    BusinessKeyDefinition(
                        "bom:scoped",
                        "mrp.bom",
                        ("code",),
                        ("product_tmpl_id",),
                        status=BusinessKeyStatus.CONFIRMED,
                    ),
                ),
            )
            evidence.draft = replace(
                evidence.draft,
                definition=replace(
                    evidence.draft.definition,
                    datasets=(bom, article),
                    schema_hash=evidence.governance.content_hash,
                ),
            )
        rows = {("dataset:article", ("column:article-code",)): (("P001",),)}
        rows[
            (
                "dataset:bom",
                (
                    ("column:bom", "column:article", "suffix")
                    if identity
                    else ("column:article", "suffix")
                ),
            )
        ] = (
            (("B001", "P", "001"), ("B002", "P", "404"), ("B003", None, None))
            if identity
            else (("P", "001"), ("P", "404"), (None, None))
        )
        keys = fixtures._Keys(rows)
        repository = fixtures._Repository()
        service = MatchingOrderService(
            repository, CapabilityAuthorizationPolicy(), keys
        )
        local = service.recommend(
            evidence.selection, evidence.schema, evidence.draft.definition
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
        return evidence, service, repository, prepared, keys

    def test_related_lookup_queries_prepared_values_for_both_supported_majors(self):
        for major in (19, 20):
            with self.subTest(major=major):
                evidence, service, repository, prepared, keys = self.setup_check(major)
                product_requests = [
                    request
                    for request in prepared.requirements.record_requests
                    if request.model == "product.template"
                ]
                self.assertTrue(
                    any("P404" in str(request.domain) for request in product_requests)
                )
                self.assertFalse(
                    any("suffix" in request.fields for request in product_requests)
                )
                service._run_live_check(
                    prepared,
                    lambda _requirements: fixtures._product_snapshot(
                        evidence,
                        records=(
                            TargetRecord("product.template", 71, {"ref": "P001"}),
                        ),
                    ),
                )
                health = repository.check.relationship_health_results[0]
                self.assertEqual(
                    (health.target_count, health.missing_count, health.blank_row_count),
                    (1, 1, 1),
                )
                self.assertEqual(health.blocked_count, 2)
                self.assertEqual(len(keys.calls), 2)

    def test_relational_identity_scope_uses_prepared_key_and_reuses_source_scan(self):
        _evidence, _service, _repository, prepared, keys = self.setup_check(
            identity=True
        )
        probe = next(
            item
            for item in prepared.identity_probes
            if item.dataset_id == "dataset:bom"
        )
        self.assertEqual(
            probe.relational_components[0].source_keys, (("P001",), ("P404",))
        )
        self.assertEqual(
            len([call for call in keys.calls if call[2] == "dataset:bom"]), 1
        )

    def test_prepared_reference_ambiguity_still_blocks_the_relationship(self):
        evidence, service, repository, prepared, _keys = self.setup_check()
        service._run_live_check(
            prepared,
            lambda _requirements: fixtures._product_snapshot(
                evidence,
                records=(
                    TargetRecord("product.template", 71, {"ref": "P001"}),
                    TargetRecord("product.template", 72, {"ref": "P001"}),
                ),
            ),
        )
        health = repository.check.relationship_health_results[0]
        self.assertEqual(health.ambiguous_count, 1)
        self.assertFalse(health.ready)

    def test_supporting_capture_aggregates_prepared_keys_after_one_narrow_read(self):
        evidence = fixtures._evidence()
        schema = replace(evidence.schema, models=(evidence.schema.models[1],))
        bom = evidence.draft.definition.datasets[0]
        relationship = replace(
            bom.relationships[0],
            resolver=replace(
                bom.relationships[0].resolver,
                origin=ResolverOrigin.TARGET_CATALOG,
                dataset_id=None,
                key_mappings=(reference(),),
            ),
        )
        bom = replace(bom, relationships=(relationship,))
        calls = []

        identity = (bom.dataset_id, bom.row_inclusion)

        def read(_workspace, projections, datasets):
            calls.append(projections)
            self.assertEqual(datasets, (bom,))
            return {identity: SourceKeyProjection({("P", "001"): 2, (None, None): 1}, {})}

        context = SimpleNamespace(
            queries=SimpleNamespace(get=lambda _: object()),
            categorical_coverage=SimpleNamespace(source_key_projection_counts=read),
        )
        with (
            patch(
                "impodo.web.composition.target_readers.run_local_operation_sync",
                side_effect=lambda callback: callback(),
            ),
            patch(
                "impodo.web.composition.target_readers._capture_supporting_values",
            ) as capture,
        ):
            publish = _capture_mapping_supporting_values(
                context, "workspace", schema, (bom,), actor=LOCAL_ACTOR
            )
        self.assertIs(publish, capture.return_value)
        publish.assert_not_called()
        self.assertEqual(calls, [{identity: ("column:article", "suffix")}])
        self.assertEqual(
            capture.call_args.kwargs["targeted_values"],
            {
                ("product.template", ("ref",), ()): (("P001",),),
            },
        )
