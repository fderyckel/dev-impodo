"""Prepare original child rows with their confirmed hierarchy parent and company."""

from unittest import TestCase
from unittest.mock import PropertyMock, patch

from impodo.application.hierarchy_parent_lookup import HierarchyParentChoice
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    IdentityNullPolicy,
    ReferenceKeyMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.workspace.derived_entities import (
    HierarchyConsumerBinding,
    derived_dataset_links,
)
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.integration.web import test_existing_hierarchy_parents as existing


class HierarchyConsumerPreparationTests(TestCase):
    def setUp(self):
        self.fixture = existing.ExistingHierarchyParentWebTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def hierarchy(self, major, mode):
        fixture = self.fixture
        freeze = fixture.freeze_files
        # Repeated child names belong to different parents, including parents
        # beyond the catalogue's twenty-row preview.
        rows = "Aisle,Bin\n" + "".join(f"A{i:03d},Same\n" for i in range(25))
        with patch.object(
            fixture,
            "freeze_files",
            side_effect=lambda _: freeze({"places": rows.encode()}),
        ):
            workspace_id, form, _schema = fixture.configure(major, mode)
        form.update(
            hierarchy_company_id="7", hierarchy_start="existing", hierarchy_level_2=""
        )
        prefix = f"/workspaces/{workspace_id}/derived-entities/hierarchy"
        self.assertEqual(fixture._post(prefix + "/parents", form).status_code, 200)
        form["existing_parent_choice"] = HierarchyParentChoice(
            ("Physical", "Stock"), (None, "Example")
        ).value
        response = fixture._post(prefix + "/save", form)
        self.assertEqual(response.status_code, 303, response.text[:500])
        return workspace_id

    def mappings(self, workspace_id, *, wrong_company=False):
        context = self.fixture.context
        source = context.queries.get_source_selection(workspace_id).datasets[0]
        link = derived_dataset_links(
            context.queries.get_derived_entity_plan(workspace_id)
        )[0]
        incoming = RelationshipResolver(
            origin=ResolverOrigin.DATASET, dataset_id=link.derived_dataset_id
        )

        def company(key):
            return RelationshipResolver(
                origin=ResolverOrigin.TARGET_CATALOG,
                model="res.company",
                key_mappings=(ReferenceKeyMapping(key, "name"),),
            )

        child_company = (
            link.source_column_key if wrong_company else link.company_column_key
        )
        return (
            DatasetMapping(
                source.dataset_id,
                existing.MODEL,
                source_identity_column_keys=tuple(
                    column.stable_key for column in source.columns
                ),
                target_identity=(
                    IdentityComponentMapping(
                        (source.columns[1].stable_key,), ("name",)
                    ),
                ),
                target_scope=(
                    IdentityComponentMapping(
                        (link.source_column_key,), ("container_id",), resolver=incoming
                    ),
                    IdentityComponentMapping(
                        (child_company,),
                        ("company_id",),
                        resolver=company(child_company),
                    ),
                ),
            ),
            DatasetMapping(
                link.derived_dataset_id,
                existing.MODEL,
                source_identity_column_keys=(link.canonical_key_column_key,),
                target_identity=(
                    IdentityComponentMapping((link.name_column_key,), ("name",)),
                ),
                target_scope=(
                    IdentityComponentMapping(
                        (link.parent_key_column_key,),
                        ("container_id",),
                        resolver=incoming,
                        null_policy=IdentityNullPolicy.EXPLICIT_SCOPE_NULL,
                    ),
                    IdentityComponentMapping(
                        (link.company_column_key,),
                        ("company_id",),
                        resolver=company(link.company_column_key),
                        null_policy=IdentityNullPolicy.EXPLICIT_SCOPE_NULL,
                    ),
                ),
            ),
        )

    def test_original_children_reuse_confirmed_company_in_both_versions_and_modes(self):
        fixture = self.fixture
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    workspace_id = self.hierarchy(major, mode)
                    fixture.submit(workspace_id, self.mappings(workspace_id))
                    page = fixture.client.get(
                        f"/workspaces/{workspace_id}/mapping?mapping_dataset=0&task=identity"
                    )
                    self.assertEqual(page.status_code, 200)
                    self.assertIn("Company from location_hierarchy", page.text)
                    staged, quality = fixture.prepare(workspace_id)
                    self.assertEqual(
                        (quality.blocked_count, quality.quarantined_count), (0, 0)
                    )
                    self.assertEqual(
                        len(staged.rows), 52
                    )  # 25 children, 25 parents, two anchors.
                    children = [row for row in staged.rows if row.dataset == "places"]
                    self.assertEqual(len(children), 25)
                    for i, child in enumerate(children):
                        self.assertEqual(
                            child.target_scope[0].key,
                            (f'["physical","stock","a{i:03d}"]',),
                        )
                        self.assertEqual(child.target_scope[1].key, ("Example",))
                    owner = fixture.context.migration_workspaces.get(
                        workspace_id, actor=fixture.actor
                    )
                    draft = run_local_operation_sync(
                        fixture.context.recipe_publication.draft,
                        project_id=owner.project_id,
                        data_version_id=owner.data_version_id,
                        workspace_id=workspace_id,
                        actor=fixture.actor,
                    )
                    self.assertTrue(draft.can_publish, draft.issues)

    def test_new_company_choice_preserves_saved_mapping_during_correction(self):
        fixture = self.fixture
        with patch.object(
            HierarchyConsumerBinding,
            "company_source_column_key",
            new_callable=PropertyMock,
            return_value=None,
        ):
            workspace_id = self.hierarchy(20, "REMOTE")
            original = fixture.submit(
                workspace_id, self.mappings(workspace_id, wrong_company=True)
            )
        page = fixture.client.get(
            f"/workspaces/{workspace_id}/mapping?mapping_dataset=0&task=identity"
        )
        self.assertEqual(page.status_code, 200, page.text[:500])
        self.assertIn("Company from location_hierarchy", page.text)
        updated = fixture.submit(workspace_id, self.mappings(workspace_id))
        self.assertEqual(
            updated.definition.datasets[0].source_identity_column_keys,
            original.definition.datasets[0].source_identity_column_keys,
        )
        _staged, quality = fixture.prepare(workspace_id)
        self.assertEqual((quality.blocked_count, quality.quarantined_count), (0, 0))
