from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
import unittest

from impodo.application.workspace.mapping.order_service import MatchingOrderService
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.matching_order import (
    MatchingOrderConfidence,
    MatchingOrderPreference,
    MatchingOrderSource,
)
from impodo.domain.shared.access import (
    CapabilityAuthorizationPolicy,
    LOCAL_ACTOR,
)
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.workspace.contracts import (
    SchemaField,
    SchemaModel,
    SourceDataset,
    SourceDatasetColumn,
)
from impodo.domain.workspace.derived_entities import (
    DerivedDatasetLink,
    RelatedDatasetLink,
)
from impodo.domain.workspace.errors import WorkspaceError


class _PreferenceRepository:
    def __init__(self) -> None:
        self.current: MatchingOrderPreference | None = None
        self.saved_expected_version: int | None = None
        self.reset_expected_version: int | None = None

    def get_preference(self, workspace_id: str) -> MatchingOrderPreference | None:
        if self.current is not None:
            assert self.current.workspace_id == workspace_id
        return self.current

    def save_preference(
        self,
        workspace_id: str,
        preference: MatchingOrderPreference,
        *,
        expected_version: int | None,
        actor,
    ) -> None:
        assert preference.workspace_id == workspace_id
        assert actor is LOCAL_ACTOR
        self.saved_expected_version = expected_version
        self.current = preference

    def reset_preference(
        self,
        workspace_id: str,
        *,
        expected_version: int | None,
        actor,
    ) -> None:
        assert actor is LOCAL_ACTOR
        self.reset_expected_version = expected_version
        self.current = None


def _source(dataset_id: str, name: str) -> SourceDataset:
    return SourceDataset(
        dataset_id=dataset_id,
        name=name,
        source=FileSourceBinding(
            file_id=f"file:{dataset_id}",
            table_key=name,
            source_sha256="sha256:" + "1" * 64,
            catalog_hash="sha256:" + "2" * 64,
            encoding="utf-8",
            delimiter=",",
            header_row=1,
        ),
        row_count=1,
        columns=(SourceDatasetColumn(1, "Code", f"{dataset_id}:code", "string"),),
    )


def _mapping(
    dataset_id: str,
    model: str,
    *,
    relationship_dataset: str | None = None,
) -> DatasetMapping:
    relationship = ()
    if relationship_dataset is not None:
        relationship = (
            RelationshipMapping(
                target_field="article_id",
                kind="many2one",
                source_column_keys=(f"{dataset_id}:code",),
                resolver=RelationshipResolver(
                    origin=ResolverOrigin.DATASET,
                    dataset_id=relationship_dataset,
                ),
            ),
        )
    return DatasetMapping(
        dataset_id=dataset_id,
        target_model=model,
        source_identity_column_keys=(f"{dataset_id}:code",),
        target_identity=(
            IdentityComponentMapping(
                source_column_keys=(f"{dataset_id}:code",),
                target_fields=("x_code",),
            ),
        ),
        relationships=relationship,
    )


def _field(
    name: str,
    *,
    field_type: str = "char",
    relation: str | None = None,
    readonly: bool = False,
) -> SchemaField:
    return SchemaField(
        name=name,
        label=name.replace("_", " ").title(),
        type=field_type,
        required=False,
        readonly=readonly,
        relation=relation,
        relation_field=None,
        selection=(),
    )


class MatchingOrderServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MatchingOrderService()

    def test_saved_incoming_relationship_is_confirmed_order_evidence(self) -> None:
        article = _source("dataset:article", "plw_article")
        bom = _source("dataset:bom", "plw_bom")
        selection = SimpleNamespace(datasets=(bom, article))
        definition = SimpleNamespace(
            datasets=(
                _mapping(
                    bom.dataset_id,
                    "mrp.bom",
                    relationship_dataset=article.dataset_id,
                ),
                _mapping(article.dataset_id, "product.template"),
            )
        )
        schema = SimpleNamespace(
            models=(
                SchemaModel(
                    "mrp.bom",
                    "Bill of Material",
                    (
                        _field(
                            "article_id",
                            field_type="many2one",
                            relation="product.template",
                        ),
                    ),
                ),
                SchemaModel("product.template", "Product", ()),
            )
        )

        recommendation = self.service.recommend(selection, schema, definition)

        self.assertEqual(
            recommendation.ordered_dataset_ids,
            (article.dataset_id, bom.dataset_id),
        )
        self.assertEqual(len(recommendation.facts), 1)
        self.assertIs(
            recommendation.facts[0].source,
            MatchingOrderSource.SAVED_MAPPING,
        )
        self.assertIs(
            recommendation.facts[0].confidence,
            MatchingOrderConfidence.CONFIRMED,
        )
        self.assertFalse(recommendation.preliminary)

    def test_source_preparation_links_are_confirmed_without_saved_mapping(self) -> None:
        child = _source("dataset:lines", "plw_routeopr")
        parent = _source("dataset:routes", "plw_route")
        extracted = _source("dataset:workcenters", "plw_workcenter")
        selection = SimpleNamespace(datasets=(child, parent, extracted))
        schema = SimpleNamespace(models=())

        recommendation = self.service.recommend(
            selection,
            schema,
            None,
            related_links=(
                RelatedDatasetLink(
                    parent_dataset_id=parent.dataset_id,
                    child_dataset_id=child.dataset_id,
                    reference_column_keys=("route",),
                    child_identity_column_keys=("route", "operation"),
                ),
            ),
            derived_links=(
                DerivedDatasetLink(
                    derived_dataset_id=extracted.dataset_id,
                    consumer_dataset_id=child.dataset_id,
                    source_column_key="workcenter",
                    canonical_key_column_key="workcenter:key",
                    name_column_key="workcenter:name",
                    parent_key_column_key=None,
                    target_model="mrp.workcenter",
                    target_name_field="name",
                ),
            ),
        )

        self.assertLess(
            recommendation.ordered_dataset_ids.index(parent.dataset_id),
            recommendation.ordered_dataset_ids.index(child.dataset_id),
        )
        self.assertLess(
            recommendation.ordered_dataset_ids.index(extracted.dataset_id),
            recommendation.ordered_dataset_ids.index(child.dataset_id),
        )
        self.assertEqual(
            {fact.source for fact in recommendation.facts},
            {
                MatchingOrderSource.RELATED_TABLE,
                MatchingOrderSource.DERIVED_TABLE,
            },
        )

    def test_unique_schema_relation_is_preliminary_order_evidence(self) -> None:
        bom = _source("dataset:bom", "plw_bom")
        article = _source("dataset:article", "plw_article")
        selection = SimpleNamespace(datasets=(bom, article))
        definition = SimpleNamespace(
            datasets=(
                _mapping(bom.dataset_id, "mrp.bom"),
                _mapping(article.dataset_id, "product.template"),
            )
        )
        schema = SimpleNamespace(
            models=(
                SchemaModel(
                    "mrp.bom",
                    "Bill of Material",
                    (
                        _field(
                            "product_tmpl_id",
                            field_type="many2one",
                            relation="product.template",
                        ),
                    ),
                ),
                SchemaModel("product.template", "Product", ()),
            )
        )

        recommendation = self.service.recommend(selection, schema, definition)

        self.assertEqual(
            recommendation.ordered_dataset_ids,
            (article.dataset_id, bom.dataset_id),
        )
        self.assertTrue(recommendation.preliminary)
        self.assertIs(
            recommendation.facts[0].source,
            MatchingOrderSource.ODOO_SCHEMA,
        )

    def test_ambiguous_schema_relation_and_filename_similarity_add_no_edge(
        self,
    ) -> None:
        bom = _source("dataset:bom", "plw_bom")
        first = _source("dataset:article", "plw_article")
        second = _source("dataset:article-copy", "plw_article_archive")
        selection = SimpleNamespace(datasets=(bom, first, second))
        definition = SimpleNamespace(
            datasets=(
                _mapping(bom.dataset_id, "mrp.bom"),
                _mapping(first.dataset_id, "product.template"),
                _mapping(second.dataset_id, "product.template"),
            )
        )
        schema = SimpleNamespace(
            models=(
                SchemaModel(
                    "mrp.bom",
                    "Bill of Material",
                    (
                        _field(
                            "product_tmpl_id",
                            field_type="many2one",
                            relation="product.template",
                        ),
                    ),
                ),
                SchemaModel("product.template", "Product", ()),
            )
        )

        recommendation = self.service.recommend(selection, schema, definition)

        self.assertEqual(recommendation.ordered_dataset_ids, tuple(
            item.dataset_id for item in selection.datasets
        ))
        self.assertEqual(recommendation.facts, ())

    def test_next_table_skips_datasets_with_minimum_saved_choices(self) -> None:
        first = _source("dataset:first", "first")
        second = _source("dataset:second", "second")
        third = _source("dataset:third", "third")
        selection = SimpleNamespace(datasets=(first, second, third))
        definition = SimpleNamespace(
            datasets=(
                _mapping(first.dataset_id, "x.first"),
                _mapping(second.dataset_id, "x.second"),
            )
        )
        recommendation = self.service.recommend(
            selection,
            SimpleNamespace(models=()),
            definition,
        )

        next_dataset = self.service.next_incomplete_dataset_id(
            selection,
            recommendation,
            definition,
            current_dataset_id=first.dataset_id,
        )

        self.assertEqual(next_dataset, third.dataset_id)

    def test_custom_order_is_versioned_and_kept_out_of_mapping_contracts(self) -> None:
        first = _source("dataset:first", "first")
        second = _source("dataset:second", "second")
        selection = SimpleNamespace(
            datasets=(first, second),
            content_hash="sha256:" + "a" * 64,
        )
        preferences = _PreferenceRepository()
        service = MatchingOrderService(
            preferences,
            CapabilityAuthorizationPolicy(),
        )

        saved = service.save_preference(
            "workspace:test",
            selection,
            (second.dataset_id, first.dataset_id),
            expected_version=None,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(saved.version, 1)
        self.assertEqual(saved.source_selection_hash, selection.content_hash)
        self.assertEqual(
            saved.ordered_dataset_ids,
            (second.dataset_id, first.dataset_id),
        )
        self.assertEqual(preferences.saved_expected_version, None)
        self.assertIs(
            service.current_preference("workspace:test", actor=LOCAL_ACTOR),
            saved,
        )

        service.reset_preference(
            "workspace:test",
            expected_version=1,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(preferences.reset_expected_version, 1)
        self.assertIsNone(preferences.current)

    def test_custom_order_must_be_an_exact_current_table_permutation(self) -> None:
        first = _source("dataset:first", "first")
        second = _source("dataset:second", "second")
        selection = SimpleNamespace(
            datasets=(first, second),
            content_hash="sha256:" + "a" * 64,
        )
        service = MatchingOrderService(
            _PreferenceRepository(),
            CapabilityAuthorizationPolicy(),
        )

        for invalid in (
            (first.dataset_id,),
            (first.dataset_id, first.dataset_id),
            (first.dataset_id, "dataset:outside"),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(WorkspaceError, "every current"):
                    service.save_preference(
                        "workspace:test",
                        selection,
                        invalid,
                        expected_version=None,
                        actor=LOCAL_ACTOR,
                    )

    def test_stale_custom_order_reconciliation_is_deterministic(self) -> None:
        preference = MatchingOrderPreference(
            workspace_id="workspace:test",
            version=3,
            source_selection_hash="sha256:" + "a" * 64,
            ordered_dataset_ids=(
                "dataset:removed",
                "dataset:bom",
                "dataset:article",
            ),
            updated_at=datetime.now(UTC),
            actor_issuer=LOCAL_ACTOR.identity.issuer,
            actor_subject=LOCAL_ACTOR.identity.subject_id,
            actor_display_name=LOCAL_ACTOR.identity.display_name,
        )

        effective = MatchingOrderService.effective_preference_order(
            preference,
            ("dataset:article", "dataset:new", "dataset:bom"),
        )

        self.assertEqual(
            effective,
            ("dataset:bom", "dataset:article", "dataset:new"),
        )

    def test_accessible_moves_change_exactly_one_position(self) -> None:
        order = ("dataset:article", "dataset:route", "dataset:bom")

        moved_up = MatchingOrderService.move_dataset(
            order,
            "dataset:bom",
            direction="up",
        )
        moved_down = MatchingOrderService.move_dataset(
            moved_up,
            "dataset:bom",
            direction="down",
        )

        self.assertEqual(
            moved_up,
            ("dataset:article", "dataset:bom", "dataset:route"),
        )
        self.assertEqual(moved_down, order)
        with self.assertRaisesRegex(WorkspaceError, "cannot move further"):
            MatchingOrderService.move_dataset(
                order,
                "dataset:article",
                direction="up",
            )

    def test_next_table_follows_saved_custom_work_order(self) -> None:
        first = _source("dataset:first", "first")
        second = _source("dataset:second", "second")
        third = _source("dataset:third", "third")
        selection = SimpleNamespace(datasets=(first, second, third))
        recommendation = self.service.recommend(
            selection,
            SimpleNamespace(models=()),
            None,
        )

        next_dataset = self.service.next_incomplete_dataset_id(
            selection,
            recommendation,
            None,
            current_dataset_id=first.dataset_id,
            work_order=(first.dataset_id, third.dataset_id, second.dataset_id),
        )

        self.assertEqual(next_dataset, third.dataset_id)


if __name__ == "__main__":
    unittest.main()
