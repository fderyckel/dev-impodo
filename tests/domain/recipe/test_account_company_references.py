"""Publish and rebind reviewed lists without permitting supporting writes."""

import unittest
from itertools import product
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

from impodo.application.recipe_application_compilation import RecipeApplicationCompiler
from impodo.application.recipe_compilation_service import RecipeCompiler
from impodo.domain.mapping.artifacts import MappingRevision, MappingSubmission
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
from impodo.domain.preparation.quality import default_quality_ruleset
from impodo.domain.schema.governance import SchemaGovernance
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaModel,
    SchemaOrigin,
    SourceSelection,
)
from impodo.domain.workspace.reference_keys import (
    StandardReferenceFieldContract,
    reference_policy_hash,
)
from impodo.domain.workspace.supporting_lookups import (
    SupportingLookupChoice,
    SupportingLookupSnapshot,
)
from impodo.domain.workspace.supporting_review import SupportingChoice
from tests.domain.recipe.test_representative_shapes import (
    _column,
    _dataset,
    _field,
    _key,
)
from tests.support.recipe_compiler import Evidence

HASH = "sha256:" + "1" * 64


class AccountCompanyRecipeTests(unittest.TestCase):
    def test_company_reuse_survives_publication_and_new_target_assessment(self):
        for major, constant, authoring_mode, target_mode, related in product(
            (19, 20), (False, True), ("LOCAL", "REMOTE"), ("LOCAL", "REMOTE"),
            ("res.company", "account.account.tag"),
        ):
            field = "company_ids" if related == "res.company" else "tag_ids"
            with self.subTest(
                major=major, constant=constant,
                authoring_mode=authoring_mode, target_mode=target_mode, related=related,
            ):
                now = datetime.now(UTC)
                dataset = _dataset(
                    "Accounts", (("code", "string"), ("company", "string")), "a"
                )
                selection = SourceSelection(
                    str(uuid4()), 1, str(uuid4()), now, "Manager", (dataset,), HASH
                )
                workspace_id = str(
                    uuid5(NAMESPACE_URL, f"workspace:{selection.data_version_id}")
                )
                schema = OdooSchemaCatalog(
                    workspace_id=workspace_id,
                    policy_hash=HASH,
                    captured_at=now,
                    captured_by="Manager",
                    connection_mode=authoring_mode,
                    database="authoring",
                    odoo_version=f"{major}.0",
                    models=(
                        SchemaModel(
                            "account.account",
                            "Accounts",
                            (
                                _field("code", required=True),
                                _field(
                                    field,
                                    "many2many",
                                    relation=related,
                                    required=True,
                                ),
                            ),
                        ),
                    ),
                    content_hash=HASH,
                    origin=SchemaOrigin.LIVE_API,
                    read_credential_binding_hash=HASH,
                    read_principal_hash=HASH,
                    read_permission_hash=HASH,
                    read_context_hash=HASH,
                    connection_target_hash=HASH,
                )
                governance = SchemaGovernance(
                    str(uuid4()),
                    1,
                    workspace_id,
                    schema.content_hash,
                    ("account.account",),
                    (_key("account.account", "code"),),
                    now,
                    "Manager",
                )
                relationship = RelationshipMapping(
                    field,
                    "many2many",
                    (_column(dataset, "company"),),
                    RelationshipResolver(
                        ResolverOrigin.TARGET_CATALOG,
                        model=related,
                        key_mappings=(
                            ReferenceKeyMapping(_column(dataset, "company"), "name"),
                        ),
                    ),
                    required=True,
                )
                if constant:
                    relationship = replace(
                        relationship,
                        source_column_keys=(),
                        resolver=RelationshipResolver(
                            ResolverOrigin.TARGET_CATALOG, model=related
                        ),
                        value_source=RelationshipValueSource.CONSTANT_EXISTING,
                        constant_reference=ConstantBusinessReference(
                            key_values=(ConstantReferenceComponent("name", "Example"),)
                        ),
                    )
                mapping = DatasetMapping(
                    dataset.dataset_id,
                    "account.account",
                    source_identity_column_keys=(_column(dataset, "code"),),
                    target_identity=(
                        IdentityComponentMapping(
                            (_column(dataset, "code"),), ("code",)
                        ),
                    ),
                    relationships=(relationship,),
                )
                definition = MappingDefinition(
                    str(uuid4()),
                    selection.content_hash,
                    governance.content_hash,
                    (mapping,),
                )
                revision = MappingRevision(
                    definition.mapping_id, 1, None, definition, now, "Manager"
                )
                submission = MappingSubmission(
                    str(uuid4()),
                    definition.mapping_id,
                    1,
                    definition.content_hash,
                    HASH,
                    (),
                    now,
                    "Manager",
                )
                ruleset = default_quality_ruleset(
                    workspace_id=workspace_id,
                    mapping_hash=definition.content_hash,
                    schema_hash=governance.content_hash,
                    datasets=(dataset.name,),
                )
                lookup = SupportingLookupSnapshot.capture(
                    workspace_id=workspace_id,
                    relation_model=related,
                    key_fields=("name",),
                    scope_fields=(),
                    display_field="name",
                    field_contracts=(
                        StandardReferenceFieldContract("name", "char", True, False),
                    ),
                    target_hash=HASH,
                    read_credential_binding_hash=HASH,
                    read_principal_hash=HASH,
                    read_permission_hash=HASH,
                    read_context_hash=HASH,
                    captured_at=now,
                    captured_by="Manager",
                    choices=(SupportingLookupChoice("Example", "Example"),),
                    ambiguous_values=(),
                    reference_policy_hash=reference_policy_hash(major),
                )
                evidence = Evidence(
                    selection=selection,
                    revision=revision,
                    submission=submission,
                    schema=schema,
                    governance=governance,
                    ruleset=ruleset,
                    supporting_references=(lookup,),
                )
                choice = SupportingChoice(
                    "account.account",
                    field,
                    "REUSE",
                    key_fields=("name",),
                    display_field="name",
                )
                compiler = RecipeCompiler(
                    evidence,
                    evidence,
                    evidence,
                    evidence,
                    evidence,
                    evidence,
                    supporting_references=evidence,
                    supporting_decisions=SimpleNamespace(
                        require_mapping=lambda *_: SimpleNamespace(choices=(choice,))
                    ),
                )
                compiled, issues = compiler.compile_workspace(workspace_id)
                self.assertIsNotNone(compiled, issues)
                recipe = compiled.recipe
                contract = recipe["odoo_target_contract"]
                self.assertEqual(contract["odoo_major_version"], major)
                self.assertEqual(
                    contract["reference_policy_hash"], reference_policy_hash(major)
                )
                self.assertEqual(
                    set(contract["approved_write_fields"]), {"account.account"}
                )
                company = next(
                    m for m in contract["models"] if m["model"] == related
                )
                self.assertEqual(
                    company["reference_evidence_kind"], "CAPTURED_GOVERNED"
                )
                self.assertTrue(all(not f["write_use"] for f in company["fields"]))
                self.assertEqual(
                    company["reference_paths"][0]["relationship_type"], "many2many"
                )
                portable = recipe["mapping"]["datasets"][0]["relationships"][0]
                rebound = RecipeApplicationCompiler()._relationship(
                    portable,
                    {}
                    if constant
                    else {portable["source_column_ids"][0]: "next-company"},
                )
                self.assertEqual(rebound.kind, "many2many")
                self.assertEqual(rebound.resolver.model, related)
                self.assertEqual(
                    rebound.source_column_keys, () if constant else ("next-company",)
                )
                if constant:
                    self.assertEqual(
                        rebound.constant_reference, relationship.constant_reference
                    )
                fresh = replace(
                    schema,
                    connection_mode=target_mode,
                    workspace_id=str(uuid4()),
                    database="next_target",
                    connection_target_hash="sha256:" + "9" * 64,
                    models=(
                        *schema.models,
                        SchemaModel(
                            related, "Linked record", (_field("name", required=True),)
                        ),
                    ),
                )
                _assessment, target_issues, _defaults = (
                    RecipeApplicationCompiler()._target_assessment(recipe, fresh)
                )
                self.assertFalse(
                    [issue for issue in target_issues if issue.blocks], target_issues
                )
