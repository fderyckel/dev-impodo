from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from uuid import uuid4

from impodo.application.data_version.inspection import (
    CATALOG_CONTRACT_VERSION,
    SourceFileCatalog,
    SourceTableCatalog,
)
from impodo.application.source_workspace_service import (
    SourceWorkspaceService,
    _blocking_header_problem,
    _dataset_name_violations,
)
from impodo.domain.guided_odoo_capture import (
    GuidedOdooCaptureMode,
    GuidedOdooCapturePlanDraft,
)
from impodo.domain.odoo_capture import (
    OdooCaptureFilterPolicy,
    OdooCaptureRole,
)
from impodo.domain.odoo_relationship_scope import (
    OdooRelationshipCaptureAction,
    OdooRelationshipScope,
    OdooRelationshipScopeDecision,
)
from impodo.domain.odoo_source_policy import ODOO_SOURCE_POLICY_HASH
from impodo.domain.odoo_source_scope import RelatedDataHandling
from impodo.domain.shared.access import Actor, ActorIdentity, Capability
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceState,
    WorkspaceStatus,
)


_HASHES = tuple(f"sha256:{digit * 64}" for digit in "123456")


class GuidedOdooCaptureSelectionBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        self.workspace_id = str(uuid4())
        self.data_version_id = str(uuid4())
        self.actor = Actor(
            identity=ActorIdentity("test", "data-manager", "Data Manager"),
            capabilities=frozenset({Capability.SOURCE_SELECT}),
        )
        self.workspace = WorkspaceState(
            workspace_id=self.workspace_id,
            name="Odoo migration",
            source_system="Odoo",
            source_mode=SourceMode.ODOO,
            intended_models=("res.company", "x.root"),
            status=WorkspaceStatus.REGISTERED,
            registered_at=self.now,
            created_at=self.now,
            updated_at=self.now,
        )
        self.authorization = Mock()
        self.authorization.require.return_value = SimpleNamespace(
            data_version_id=self.data_version_id,
        )
        self.workspace_states = Mock()
        self.workspace_states.get.return_value = self.workspace
        self.sources = Mock()
        self.service = SourceWorkspaceService(
            self.workspace_states,
            self.sources,
            self.authorization,
        )
        self.schema = _schema(self.workspace_id, self.now)
        self.scope = _scope(self.now)
        self.drafts = _drafts()

    def test_builds_complete_schema_bound_set_without_persisting(self) -> None:
        selections = self.service.build_guided_odoo_capture_selections(
            self.workspace_id,
            drafts=reversed(self.drafts),
            schema=self.schema,
            relationship_scope=self.scope,
            actor=self.actor,
        )

        self.assertEqual(
            tuple(selection.model for selection in selections),
            ("res.company", "x.root"),
        )
        by_model = {selection.model: selection for selection in selections}
        self.assertIs(
            by_model["x.root"].capture_role,
            OdooCaptureRole.ROOT,
        )
        self.assertIs(
            by_model["x.root"].filter_policy,
            OdooCaptureFilterPolicy.ACTIVE_RECORDS,
        )
        self.assertIs(
            by_model["res.company"].capture_role,
            OdooCaptureRole.LINKED_ONLY,
        )
        self.assertIs(
            by_model["res.company"].filter_policy,
            OdooCaptureFilterPolicy.ACTIVE_AND_ARCHIVED_RECORDS,
        )
        self.assertTrue(all(item.version == 1 for item in selections))
        self.assertTrue(
            all(item.data_version_id == self.data_version_id for item in selections)
        )
        self.assertTrue(
            all(item.schema_scope_hash == self.schema.content_hash for item in selections)
        )
        self.assertEqual(
            {item.created_at for item in selections},
            {selections[0].created_at},
        )
        self.sources.save_odoo_capture_selection.assert_not_called()

    def test_rejects_an_incomplete_or_stale_draft_set(self) -> None:
        with self.assertRaisesRegex(
            WorkspaceError,
            "one current or guided capture plan for every selected Odoo record type",
        ):
            self.service.build_guided_odoo_capture_selections(
                self.workspace_id,
                drafts=(self.drafts[0],),
                schema=self.schema,
                relationship_scope=self.scope,
                actor=self.actor,
            )

        self.sources.save_odoo_capture_selection.assert_not_called()

    def test_merges_a_reviewed_manual_plan_with_remaining_safe_drafts(self) -> None:
        complete = self.service.build_guided_odoo_capture_selections(
            self.workspace_id,
            drafts=self.drafts,
            schema=self.schema,
            relationship_scope=self.scope,
            actor=self.actor,
        )
        manual_root = next(item for item in complete if item.model == "x.root")

        merged = self.service.build_guided_odoo_capture_selections(
            self.workspace_id,
            drafts=(self.drafts[0],),
            current_selections=(manual_root,),
            schema=self.schema,
            relationship_scope=self.scope,
            actor=self.actor,
        )

        self.assertEqual(
            tuple(item.model for item in merged),
            ("res.company", "x.root"),
        )
        self.assertEqual(
            next(item for item in merged if item.model == "x.root"),
            manual_root,
        )
        self.sources.save_odoo_capture_selection.assert_not_called()

    def test_rejects_a_draft_role_or_mode_that_disagrees_with_scope(self) -> None:
        linked = self.drafts[0]
        root = self.drafts[1]
        for changed_drafts, message in (
            (
                (
                    linked,
                    replace(root, capture_role=OdooCaptureRole.LINKED_ONLY),
                ),
                "role",
            ),
            (
                (
                    replace(linked, mode=GuidedOdooCaptureMode.FULL),
                    root,
                ),
                "mode",
            ),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(
                WorkspaceError,
                f"Guided capture {message}",
            ):
                self.service.build_guided_odoo_capture_selections(
                    self.workspace_id,
                    drafts=changed_drafts,
                    schema=self.schema,
                    relationship_scope=self.scope,
                    actor=self.actor,
                )

        self.sources.save_odoo_capture_selection.assert_not_called()

    def test_rejects_fields_not_eligible_in_the_final_live_schema(self) -> None:
        changed = replace(self.drafts[0], field_names=("missing_name",))

        with self.assertRaisesRegex(
            WorkspaceError,
            "missing_name is not eligible",
        ):
            self.service.build_guided_odoo_capture_selections(
                self.workspace_id,
                drafts=(changed, self.drafts[1]),
                schema=self.schema,
                relationship_scope=self.scope,
                actor=self.actor,
            )

        self.sources.save_odoo_capture_selection.assert_not_called()


def _schema(workspace_id: str, now: datetime) -> OdooSchemaCatalog:
    return OdooSchemaCatalog(
        workspace_id=workspace_id,
        policy_hash=ODOO_SOURCE_POLICY_HASH,
        captured_at=now,
        captured_by="Data Manager",
        connection_mode="REMOTE",
        database="production",
        odoo_version="19.0",
        models=(
            SchemaModel(
                name="res.company",
                label="Companies",
                fields=(
                    _field("active", "Active", "boolean"),
                    _field("name", "Company Name", "char", required=True),
                    _field(
                        "write_date",
                        "Last Updated",
                        "datetime",
                        readonly=True,
                    ),
                ),
            ),
            SchemaModel(
                name="x.root",
                label="Main records",
                fields=(
                    _field("active", "Active", "boolean"),
                    _field(
                        "company_id",
                        "Company",
                        "many2one",
                        required=True,
                        relation="res.company",
                    ),
                    _field("name", "Name", "char", required=True),
                    _field(
                        "write_date",
                        "Last Updated",
                        "datetime",
                        readonly=True,
                    ),
                ),
            ),
        ),
        content_hash=_HASHES[1],
        origin=SchemaOrigin.LIVE_API,
        read_credential_binding_hash=_HASHES[2],
        read_principal_hash=_HASHES[3],
        read_permission_hash=_HASHES[4],
        read_context_hash=_HASHES[5],
        connection_target_hash=_HASHES[0],
    )


def _field(
    name: str,
    label: str,
    field_type: str,
    *,
    required: bool = False,
    readonly: bool = False,
    relation: str | None = None,
) -> SchemaField:
    return SchemaField(
        name=name,
        label=label,
        type=field_type,
        required=required,
        readonly=readonly,
        relation=relation,
        relation_field=None,
        selection=(),
        stored=True,
        computed=False,
        has_inverse=False,
        related=False,
        translated=False,
        company_dependent=False,
        searchable=True,
        sortable=True,
        exportable=True,
    )


def _scope(now: datetime) -> OdooRelationshipScope:
    return OdooRelationshipScope.create(
        scope_id=str(uuid4()),
        version=1,
        decisions=(
            OdooRelationshipScopeDecision(
                source_model="x.root",
                field_name="company_id",
                relation_model="res.company",
                required=True,
                handling=RelatedDataHandling.REUSE_DESTINATION,
                action=OdooRelationshipCaptureAction.MATCH_EXISTING,
                relationship_type="many2one",
            ),
        ),
        root_models=("x.root",),
        recorded_at=now,
        recorded_by="Data Manager",
    )


def _drafts() -> tuple[GuidedOdooCapturePlanDraft, ...]:
    return (
        GuidedOdooCapturePlanDraft(
            model="res.company",
            model_label="Companies",
            dataset_name="odoo_res_company",
            field_names=("name",),
            mode=GuidedOdooCaptureMode.IDENTITY_ONLY,
            capture_role=OdooCaptureRole.LINKED_ONLY,
        ),
        GuidedOdooCapturePlanDraft(
            model="x.root",
            model_label="Main records",
            dataset_name="odoo_x_root",
            field_names=("active", "name"),
            mode=GuidedOdooCaptureMode.FULL,
            capture_role=OdooCaptureRole.ROOT,
        ),
    )


class DatasetNameValidationTests(unittest.TestCase):
    def test_valid_name_has_no_violations(self) -> None:
        self.assertEqual(_dataset_name_violations("product_with_uom_v1"), ())

    def test_blank_name_explains_that_a_name_is_required(self) -> None:
        self.assertEqual(
            _dataset_name_violations(""),
            ("Enter a name.",),
        )

    def test_long_name_explains_the_length_limit(self) -> None:
        self.assertEqual(
            _dataset_name_violations("a" * 64),
            ("Use no more than 63 characters.",),
        )

    def test_uppercase_name_explains_each_broken_rule(self) -> None:
        self.assertEqual(
            _dataset_name_violations("Product_withUoM_v1"),
            (
                "Start with a lowercase letter from a to z.",
                "Use only lowercase letters, numbers, and underscores.",
            ),
        )

    def test_punctuation_explains_the_allowed_characters(self) -> None:
        self.assertEqual(
            _dataset_name_violations("product-with-uom"),
            ("Use only lowercase letters, numbers, and underscores.",),
        )


class SourceHeaderValidationTests(unittest.TestCase):
    def test_data_beyond_headers_is_explained_during_source_review(self) -> None:
        table = SourceTableCatalog(
            table_key="sheet:PLW",
            name="PLW",
            kind="WORKSHEET",
            hidden=False,
            header_row=1,
            row_count=1,
            column_count=2,
            columns=(),
            preview_rows=(),
            warnings=(
                "1 data row(s) contain cells beyond the candidate header; "
                "the first value is at C2",
            ),
        )
        catalog = SourceFileCatalog(
            contract_version=CATALOG_CONTRACT_VERSION,
            file_id="stored-source-id",
            display_name="PLW-Article.xlsx",
            source_sha256="sha256:" + "a" * 64,
            source_size_bytes=100,
            format="XLSX",
            inspected_at=datetime.now(timezone.utc),
            encoding=None,
            delimiter=None,
            tables=(table,),
        )

        self.assertEqual(
            _blocking_header_problem(catalog, table),
            "Source file 'PLW-Article.xlsx', sheet 'PLW' has data in column C, "
            "but header cell C1 is empty. Add a name in C1, or remove the "
            "unexpected data from column C, then update the preview.",
        )


if __name__ == "__main__":
    unittest.main()
