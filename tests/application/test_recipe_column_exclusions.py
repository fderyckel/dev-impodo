"""Preserve explicit column exclusions through portable Recipe reuse."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime

from impodo.application.recipe_application_compilation import RecipeApplicationCompiler
from impodo.application.recipe_compilation_service import RecipeCompiler
from impodo.domain.mapping.artifacts import MappingRevision
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    ScalarFieldMapping,
)
from impodo.domain.preparation.quality import default_quality_ruleset
from impodo.domain.recipe_applications import (
    RecipeApplicationError,
    RecipeApplicationIssueLevel,
)
from impodo.domain.recipe_parameters import RecipeParameterDefinitions
from impodo.domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
    SchemaGovernance,
)
from impodo.domain.serialization import canonical_json
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)

_NOW = datetime(2026, 11, 17, tzinfo=UTC)
_HASH = "sha256:" + "a" * 64


def _selection(
    prefix: str, *, notes: tuple[str, ...] = ("Internal notes",)
) -> SourceSelection:
    headings = ("Code", "Name", *notes)
    dataset = SourceDataset(
        dataset_id=f"{prefix}-dataset",
        name="Customers",
        source=FileSourceBinding(
            file_id=f"{prefix}-file",
            table_key="Customers.csv",
            source_sha256=_HASH,
            catalog_hash=_HASH,
            encoding="utf-8",
            delimiter=",",
            header_row=1,
        ),
        row_count=2,
        columns=tuple(
            SourceDatasetColumn(index, heading, f"{prefix}-column-{index}", "string")
            for index, heading in enumerate(headings, start=1)
        ),
    )
    return SourceSelection(
        selection_id=f"{prefix}-selection",
        version=1,
        data_version_id=f"{prefix}-data-version",
        created_at=_NOW,
        created_by="Data manager",
        datasets=(dataset,),
        content_hash=_HASH,
    )


def _mapping(selection: SourceSelection, *, excluded: bool = True) -> DatasetMapping:
    dataset = selection.datasets[0]
    return DatasetMapping(
        dataset_id=dataset.dataset_id,
        target_model="res.partner",
        source_identity_column_keys=(dataset.columns[0].stable_key,),
        target_identity=(
            IdentityComponentMapping(
                source_column_keys=(dataset.columns[0].stable_key,),
                target_fields=("ref",),
            ),
        ),
        fields=(
            ScalarFieldMapping(
                target_field="name",
                source_column_key=dataset.columns[1].stable_key,
            ),
        ),
        excluded_source_column_keys=(dataset.columns[2].stable_key,)
        if excluded
        else (),
    )


def _recipe(
    selection: SourceSelection,
    mapping: DatasetMapping,
    major: int = 20,
    mode: str = "REMOTE",
):
    fields = tuple(
        SchemaField(name, name.title(), "char", name == "name", False, None, None, ())
        for name in ("ref", "name")
    )
    schema = OdooSchemaCatalog(
        workspace_id="authoring-workspace",
        policy_hash=_HASH,
        captured_at=_NOW,
        captured_by="Data manager",
        connection_mode=mode,
        database="fictional",
        odoo_version=f"{major}.0",
        models=(SchemaModel("res.partner", "Contacts", fields),),
        content_hash=_HASH,
        origin=SchemaOrigin.LIVE_API,
        read_credential_binding_hash=_HASH,
        read_principal_hash=_HASH,
        read_permission_hash=_HASH,
        read_context_hash=_HASH,
        connection_target_hash=_HASH,
    )
    governance = SchemaGovernance(
        governance_id="fictional-governance",
        version=1,
        workspace_id="authoring-workspace",
        catalog_hash=schema.content_hash,
        permitted_models=("res.partner",),
        business_keys=(
            BusinessKeyDefinition(
                key_id="partner-ref",
                model="res.partner",
                key_fields=("ref",),
                status=BusinessKeyStatus.CONFIRMED,
            ),
        ),
        recorded_at=_NOW,
        recorded_by="Data manager",
    )
    definition = MappingDefinition(
        mapping_id="fictional-mapping",
        source_selection_hash=selection.content_hash,
        schema_hash=governance.content_hash,
        datasets=(mapping,),
    )
    revision = MappingRevision(
        "fictional-mapping", 1, None, definition, _NOW, "Data manager"
    )
    ruleset = default_quality_ruleset(
        workspace_id="authoring-workspace",
        mapping_hash=definition.content_hash,
        schema_hash=governance.content_hash,
        datasets=(selection.datasets[0].name,),
    )
    compiler = object.__new__(RecipeCompiler)
    compiled = compiler._definition(
        selection=selection,
        base_selection=selection,
        revision=revision,
        schema=schema,
        governance=governance,
        ruleset=ruleset,
        preparation=None,
        references=None,
        parameter_definitions=RecipeParameterDefinitions(),
        supporting_references=(),
    )
    compiler._validate_portability(compiled)
    return compiled.recipe


def _restore(recipe, selection, overrides=None):
    compiler = RecipeApplicationCompiler()
    bindings, candidates, issues = compiler._source_assessment(
        recipe, selection, overrides or {}
    )
    effective = compiler._effective_bindings(selection, bindings, definition=recipe)
    mappings = compiler._mapping_datasets(recipe, effective, selection, None, None)
    return mappings[0], effective, candidates, issues


class RecipeColumnExclusionTests(unittest.TestCase):
    def test_compile_and_reapply_uses_fresh_keys_for_each_supported_target(
        self,
    ) -> None:
        authoring = _selection("authoring")
        fresh = _selection("fresh")
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    recipe = _recipe(authoring, _mapping(authoring), major, mode)
                    source_columns = recipe["source_shape"]["datasets"][0]["columns"]
                    note = next(
                        item
                        for item in source_columns
                        if item["source_name"] == "Internal notes"
                    )
                    self.assertEqual(note["required_by"], ["exclusion"])
                    self.assertEqual(
                        recipe["mapping"]["datasets"][0]["excluded_source_column_ids"],
                        [note["logical_column_id"]],
                    )
                    self.assertNotIn("authoring-column", canonical_json(recipe))
                    restored, bindings, candidates, issues = _restore(recipe, fresh)
                    self.assertFalse(candidates)
                    self.assertFalse(issues)
                    self.assertEqual(restored.dataset_id, fresh.datasets[0].dataset_id)
                    self.assertEqual(
                        restored.excluded_source_column_keys, ("fresh-column-3",)
                    )
                    self.assertEqual(
                        restored.fields[0].source_column_key, "fresh-column-2"
                    )
                    self.assertEqual(
                        bindings[note["logical_column_id"]], "fresh-column-3"
                    )

    def test_missing_or_ambiguous_exclusion_only_column_is_optional(self) -> None:
        authoring = _selection("authoring")
        recipe = _recipe(authoring, _mapping(authoring))
        note_id = recipe["mapping"]["datasets"][0]["excluded_source_column_ids"][0]
        for headings in ((), ("Internal notes", "Internal notes")):
            with self.subTest(headings=headings):
                restored, bindings, candidates, issues = _restore(
                    recipe, _selection("fresh", notes=headings)
                )
                self.assertEqual(restored.excluded_source_column_keys, ())
                self.assertNotIn(note_id, bindings)
                self.assertNotIn(note_id, candidates)
                self.assertFalse(
                    any(
                        issue.level is RecipeApplicationIssueLevel.BLOCKER
                        for issue in issues
                    )
                )

    def test_explicit_override_can_resolve_an_ambiguous_exclusion(self) -> None:
        authoring = _selection("authoring")
        recipe = _recipe(authoring, _mapping(authoring))
        note_id = recipe["mapping"]["datasets"][0]["excluded_source_column_ids"][0]
        ambiguous = _selection("fresh", notes=("Internal notes", "Internal notes"))
        restored, bindings, _candidates, issues = _restore(
            recipe, ambiguous, {note_id: "fresh-column-4"}
        )
        self.assertEqual(bindings[note_id], "fresh-column-4")
        self.assertEqual(restored.excluded_source_column_keys, ("fresh-column-4",))
        self.assertFalse(
            any(issue.level is RecipeApplicationIssueLevel.BLOCKER for issue in issues)
        )

    def test_renamed_heading_override_remains_exact_through_effective_bindings(
        self,
    ) -> None:
        authoring = _selection("authoring")
        recipe = _recipe(authoring, _mapping(authoring))
        note_id = recipe["mapping"]["datasets"][0]["excluded_source_column_ids"][0]
        fresh = _selection(
            "fresh", notes=("Renamed notes", "Renamed notes", "Renamed notes")
        )
        restored, bindings, _candidates, issues = _restore(
            recipe, fresh, {note_id: "fresh-column-4"}
        )
        self.assertEqual(restored.excluded_source_column_keys, ("fresh-column-4",))
        self.assertEqual(bindings[note_id], "fresh-column-4")
        self.assertNotIn("column:customers.renamed_notes", bindings)
        self.assertFalse(
            any(issue.level is RecipeApplicationIssueLevel.BLOCKER for issue in issues)
        )

    def test_stale_override_for_missing_optional_exclusion_does_not_block(self) -> None:
        authoring = _selection("authoring")
        recipe = _recipe(authoring, _mapping(authoring))
        note_id = recipe["mapping"]["datasets"][0]["excluded_source_column_ids"][0]
        restored, bindings, candidates, issues = _restore(
            recipe, _selection("fresh", notes=()), {note_id: "authoring-column-3"}
        )
        self.assertEqual(restored.excluded_source_column_keys, ())
        self.assertNotIn(note_id, bindings)
        self.assertNotIn(note_id, candidates)
        self.assertFalse(
            any(issue.level is RecipeApplicationIssueLevel.BLOCKER for issue in issues)
        )

    def test_another_dependency_keeps_an_excluded_column_required(self) -> None:
        authoring = _selection("authoring")
        mapping = replace(
            _mapping(authoring), source_identity_column_keys=("authoring-column-3",)
        )
        recipe = _recipe(authoring, mapping)
        note = next(
            item
            for item in recipe["source_shape"]["datasets"][0]["columns"]
            if item["source_name"] == "Internal notes"
        )
        self.assertEqual(note["required_by"], ["exclusion", "mapping"])
        _bindings, candidates, issues = RecipeApplicationCompiler()._source_assessment(
            recipe, _selection("fresh", notes=()), {}
        )
        self.assertIn(note["logical_column_id"], candidates)
        self.assertIn("RECIPE_SOURCE_COLUMN_MISSING", {issue.code for issue in issues})

    def test_effective_bindings_keep_required_column_ambiguity_rejection(self) -> None:
        authoring = _selection("authoring")
        mapping = replace(
            _mapping(authoring), source_identity_column_keys=("authoring-column-3",)
        )
        recipe = _recipe(authoring, mapping)
        fresh = _selection("fresh", notes=("Internal notes", "Internal notes"))
        compiler = RecipeApplicationCompiler()
        bindings, _candidates, issues = compiler._source_assessment(recipe, fresh, {})
        self.assertIn(
            "RECIPE_SOURCE_COLUMN_AMBIGUOUS", {issue.code for issue in issues}
        )
        with self.assertRaisesRegex(RecipeApplicationError, "ambiguous reusable names"):
            compiler._effective_bindings(fresh, bindings, definition=recipe)

    def test_empty_exclusions_do_not_add_portable_fields_or_change_bytes(self) -> None:
        selection = _selection("authoring")
        mapping = _mapping(selection, excluded=False)
        implicit = _recipe(selection, mapping)
        explicit = _recipe(selection, replace(mapping, excluded_source_column_keys=()))
        self.assertEqual(canonical_json(implicit), canonical_json(explicit))
        self.assertNotIn(
            "excluded_source_column_ids", implicit["mapping"]["datasets"][0]
        )
        self.assertNotIn("Internal notes", canonical_json(implicit["source_shape"]))
        restored, _bindings, _candidates, _issues = _restore(
            implicit, _selection("fresh", notes=())
        )
        self.assertEqual(restored.excluded_source_column_keys, ())
