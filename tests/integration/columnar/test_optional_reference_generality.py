"""Apply absent linked scopes to unrelated document and approver metadata."""

from dataclasses import replace
from unittest import TestCase

from impodo.domain.compiler.browser_mapping_compiler import compile_browser_mapping
from impodo.domain.execution.planner import plan_record_requests
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    IdentityNullPolicy,
    MappingDefinition,
    ReferenceKeyMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
)
from impodo.domain.preparation.source import SourceRow
from impodo.domain.workspace.contracts import SourceDatasetColumn
from tests.integration.columnar import test_optional_linked_scope as fixtures
from tests.support.optional_scope import optional_scope_fixture


class OptionalReferenceGeneralityTests(TestCase):
    def test_document_approver_uses_the_same_native_absence_contract(self):
        selection, old_definition, _rows = optional_scope_fixture()
        names = ("row", "document", "approver", "department", "section", "quantity")
        source = replace(
            selection.datasets[0],
            columns=tuple(
                SourceDatasetColumn(index, name, name, "string")
                for index, name in enumerate(names, 1)
            ),
        )
        selection = replace(selection, datasets=(source,))
        resolver = RelationshipResolver(
            ResolverOrigin.TARGET_CATALOG,
            model="x.person",
            key_mappings=(ReferenceKeyMapping("approver", "badge"),),
            scope_mappings=(ReferenceKeyMapping("department", "unit"),),
        )
        mapping = DatasetMapping(
            source.dataset_id,
            "x.document",
            source_identity_column_keys=("row",),
            target_identity=(IdentityComponentMapping(("document",), ("reference",)),),
            target_scope=(
                IdentityComponentMapping(("section",), ("section",)),
                IdentityComponentMapping(
                    ("approver", "department"),
                    ("reviewer_id",),
                    resolver=resolver,
                    null_policy=IdentityNullPolicy.OPTIONAL_SCOPE_NULL,
                    blank_source_column_key="approver",
                ),
            ),
            fields=(ScalarFieldMapping("revision", "quantity", value_type="integer"),),
        )
        definition = MappingDefinition(
            "optional-reviewer",
            selection.content_hash,
            old_definition.schema_hash,
            (mapping,),
        )
        rows = tuple(
            SourceRow(
                index,
                dict(
                    zip(
                        names,
                        (str(index), "DOC-01", approver, "Engineering", "A", "2"),
                        strict=True,
                    )
                ),
            )
            for index, approver in enumerate((None, "EMP-001", "EMP-002"), 2)
        )
        _selection, _definition, records = fixtures.OptionalLinkedScopeTests().prepare(
            case=(selection, definition, rows)
        )
        self.assertEqual([record.issues for record in records], [(), (), ()])
        self.assertEqual(records[0].target_scope, ("A", None))
        self.assertEqual(records[1].target_scope[1].key, ("EMP-001",))
        self.assertEqual(records[2].target_scope[1].scope, ("Engineering",))
        requests = plan_record_requests(
            compile_browser_mapping(definition, selection), records
        )
        domains = [
            request.domain for request in requests if request.model == "x.document"
        ]
        self.assertIn("['reviewer_id', '=', False]", str(domains))
        self.assertNotIn("['reviewer_id.badge', '=', False]", str(domains))
