from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from impodo.application.schema_workspace_service import SchemaWorkspaceService
from impodo.domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.contracts import (
    OdooModelSummary,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import SourceMode


class CompleteSchemaGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = SimpleNamespace(
            content_hash="sha256:" + "a" * 64,
            origin=SchemaOrigin.LOCAL_MANUAL,
            models=(
                _model("res.partner", "Contact", "ref"),
                _model("mrp.bom", "Bill of Materials", "code"),
            ),
        )
        self.workspaces = Mock()
        self.workspaces.get.return_value = SimpleNamespace(
            source_mode=SourceMode.FILE
        )
        self.sources = Mock()
        self.schemas = Mock()
        self.schemas.get_odoo_schema_catalog.return_value = self.schema
        self.schemas.get_schema_governance.return_value = None
        self.authorization = Mock()
        self.service = SchemaWorkspaceService(
            self.workspaces,
            self.sources,
            self.schemas,
            self.authorization,
        )

    def test_complete_governance_requires_one_rule_for_every_model(self) -> None:
        with self.assertRaisesRegex(
            WorkspaceError,
            "Bill of Materials",
        ):
            self.service.govern_complete(
                "workspace",
                business_keys=(_key("res.partner", "ref"),),
                expected_catalog_hash=self.schema.content_hash,
                actor=LOCAL_ACTOR,
            )

        self.schemas.save_schema_governance.assert_not_called()

    def test_live_governance_blocks_when_required_support_is_unavailable(
        self,
    ) -> None:
        self.schema = _live_product_schema()
        self.schemas.get_odoo_schema_catalog.return_value = self.schema
        self.schemas.get_odoo_model_catalog.return_value = _model_catalog(
            "product.template",
        )

        with self.assertRaisesRegex(
            WorkspaceError,
            "product.category",
        ):
            self.service.govern_complete(
                "workspace",
                business_keys=(
                    _key("product.template", "default_code"),
                ),
                expected_catalog_hash=self.schema.content_hash,
                actor=LOCAL_ACTOR,
            )

        self.schemas.save_schema_governance.assert_not_called()

    def test_live_governance_accepts_available_required_support(self) -> None:
        self.schema = _live_product_schema()
        self.schemas.get_odoo_schema_catalog.return_value = self.schema
        self.schemas.get_odoo_model_catalog.return_value = _model_catalog(
            "product.template",
            "product.category",
        )

        governance = self.service.govern_complete(
            "workspace",
            business_keys=(
                _key("product.template", "default_code"),
            ),
            expected_catalog_hash=self.schema.content_hash,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(
            tuple(item.model for item in governance.business_keys),
            ("product.template",),
        )
        self.schemas.save_schema_governance.assert_called_once()

    def test_complete_governance_saves_the_exact_reviewed_set(self) -> None:
        keys = (
            _key("mrp.bom", "code"),
            _key("res.partner", "ref"),
        )

        governance = self.service.govern_complete(
            "workspace",
            business_keys=keys,
            expected_catalog_hash=self.schema.content_hash,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(governance.business_keys, keys)
        self.schemas.save_schema_governance.assert_called_once()

    def test_complete_governance_rejects_stale_schema(self) -> None:
        with self.assertRaisesRegex(WorkspaceError, "Odoo details changed"):
            self.service.govern_complete(
                "workspace",
                business_keys=(
                    _key("res.partner", "ref"),
                    _key("mrp.bom", "code"),
                ),
                expected_catalog_hash="sha256:" + "b" * 64,
                actor=LOCAL_ACTOR,
            )

        self.workspaces.get.assert_not_called()
        self.schemas.save_schema_governance.assert_not_called()


def _model(name: str, label: str, field: str) -> SchemaModel:
    return SchemaModel(
        name=name,
        label=label,
        fields=(
            SchemaField(
                name=field,
                label=field,
                type="char",
                readonly=False,
                required=False,
                relation=None,
                relation_field=None,
                selection=(),
            ),
        ),
    )


def _live_product_schema() -> SimpleNamespace:
    return SimpleNamespace(
        content_hash="sha256:" + "a" * 64,
        origin=SchemaOrigin.LIVE_API,
        models=(
            SchemaModel(
                name="product.template",
                label="Products",
                fields=(
                    SchemaField(
                        name="default_code",
                        label="Internal Reference",
                        type="char",
                        readonly=False,
                        required=False,
                        relation=None,
                        relation_field=None,
                        selection=(),
                    ),
                    SchemaField(
                        name="categ_id",
                        label="Product Category",
                        type="many2one",
                        readonly=False,
                        required=True,
                        relation="product.category",
                        relation_field=None,
                        selection=(),
                        exportable=True,
                    ),
                ),
            ),
        ),
    )


def _model_catalog(*model_names: str) -> SimpleNamespace:
    labels = {
        "product.template": "Products",
        "product.category": "Product Categories",
    }
    return SimpleNamespace(
        models=tuple(
            OdooModelSummary(
                name=name,
                label=labels[name],
                modules=("product",),
                state="base",
            )
            for name in model_names
        )
    )


def _key(model: str, field: str) -> BusinessKeyDefinition:
    return BusinessKeyDefinition(
        key_id=f"{model}:{field}",
        model=model,
        key_fields=(field,),
        status=BusinessKeyStatus.CONFIRMED,
    )


if __name__ == "__main__":
    unittest.main()
