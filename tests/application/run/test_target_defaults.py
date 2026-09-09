from __future__ import annotations

from types import SimpleNamespace
import unittest
from uuid import uuid4

from impodo.application.run.target_defaults import mapped_target_defaults
from impodo.domain.mapping.contracts import (
    ScalarValueSource,
    TargetFieldHandling,
)
from impodo.domain.mapping.create_field_policy import VerifiedCreateDefaultAction
from impodo.domain.project.foundation import utc_now
from impodo.domain.serialization import content_hash
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)


class MappedTargetDefaultTests(unittest.TestCase):
    def test_projection_deduplicates_mapping_forms_and_filters_review_policy(self):
        workspace_id = str(uuid4())
        definition = SimpleNamespace(
            datasets=(
                SimpleNamespace(
                    target_model="sale.order",
                    fields=(
                        SimpleNamespace(
                            target_field="shipping_policy",
                            value_source=ScalarValueSource.ODOO_DEFAULT,
                        ),
                    ),
                    target_field_dispositions=(
                        SimpleNamespace(
                            target_field="shipping_policy",
                            handling=TargetFieldHandling.ODOO_DEFAULT,
                        ),
                        SimpleNamespace(
                            target_field="module_code",
                            handling=TargetFieldHandling.ODOO_DEFAULT,
                        ),
                    ),
                ),
            ),
        )
        schema = OdooSchemaCatalog(
            workspace_id=workspace_id,
            policy_hash=content_hash("schema-policy"),
            captured_at=utc_now(),
            captured_by="Test operator",
            connection_mode="REMOTE",
            database="fictional_test",
            odoo_version="19.0",
            models=(
                SchemaModel(
                    "sale.order",
                    "Sales Order",
                    (
                        SchemaField(
                            name="shipping_policy",
                            label="Shipping Policy",
                            type="selection",
                            required=True,
                            readonly=False,
                            relation=None,
                            relation_field=None,
                            selection=(("direct", "As soon as possible"),),
                            company_dependent=False,
                            create_default_present=True,
                            create_default_value="direct",
                        ),
                        SchemaField(
                            name="module_code",
                            label="Module code",
                            type="char",
                            required=True,
                            readonly=False,
                            relation=None,
                            relation_field=None,
                            selection=(),
                            company_dependent=False,
                            create_default_present=True,
                            create_default_value="AUTO",
                        ),
                    ),
                ),
            ),
            content_hash=content_hash("schema-evidence"),
            origin=SchemaOrigin.LIVE_API,
            read_credential_binding_hash=content_hash("credential"),
            read_principal_hash=content_hash("principal"),
            read_permission_hash=content_hash("permissions"),
            read_context_hash=content_hash("context"),
            connection_target_hash=content_hash("target"),
        )

        all_defaults = mapped_target_defaults(definition, schema)
        review_defaults = mapped_target_defaults(
            definition,
            schema,
            action=VerifiedCreateDefaultAction.REQUIRE_REVIEW,
        )

        self.assertEqual(
            tuple(item.key for item in all_defaults),
            (
                ("sale.order", "module_code"),
                ("sale.order", "shipping_policy"),
            ),
        )
        self.assertEqual(
            tuple(item.key for item in review_defaults),
            (("sale.order", "shipping_policy"),),
        )


if __name__ == "__main__":
    unittest.main()
