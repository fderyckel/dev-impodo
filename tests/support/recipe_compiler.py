"""Shared compiler-only fixtures for portable Recipe shape tests."""

from __future__ import annotations

from uuid import uuid4

from impodo.domain.recipe_parameters import RecipeParameterDefinitions


class Evidence:
    def __init__(
        self,
        *,
        selection,
        revision,
        submission,
        schema,
        governance,
        ruleset,
        parameter_definitions=RecipeParameterDefinitions(),
        base_selection=None,
        preparation=None,
        reference_bundle=None,
    ) -> None:
        self.selection = selection
        self.revision = revision
        self.submission = submission
        self.schema = schema
        self.governance = governance
        self.ruleset = ruleset
        self.parameter_definitions = parameter_definitions
        self.base_selection = base_selection or selection
        self.preparation = preparation
        self.reference_bundle = reference_bundle

    def get_mapping_source_selection(self, workspace_id):
        del workspace_id
        return self.selection

    def get_source_selection(self, workspace_id):
        del workspace_id
        return self.base_selection

    def get_mapping_revision(self, workspace_id, version=None):
        del workspace_id, version
        return self.revision

    def get_mapping_submission(self, workspace_id, version=None):
        del workspace_id, version
        return self.submission

    def get_odoo_schema_catalog(self, workspace_id):
        del workspace_id
        return self.schema

    def get_schema_governance(self, workspace_id):
        del workspace_id
        return self.governance

    def get_current_quality_ruleset(self, workspace_id):
        del workspace_id
        return self.ruleset

    def get_derived_entity_plan(self, workspace_id):
        del workspace_id
        return self.preparation

    def get_reference_bundle(self, workspace_id):
        del workspace_id
        return self.reference_bundle

    def get_parameter_definitions(self, workspace_id):
        del workspace_id
        return self.parameter_definitions

    def save_parameter_definitions(self, workspace_id, definitions, *, actor):
        del workspace_id, actor
        self.parameter_definitions = definitions


def file_binding(marker: str):
    from impodo.domain.source_binding import FileSourceBinding

    return FileSourceBinding(
        file_id=f"physical-file-{uuid4()}",
        table_key="Customers",
        source_sha256="sha256:" + marker * 64,
        catalog_hash="sha256:" + marker * 64,
        encoding="utf-8",
        delimiter=",",
        header_row=1,
    )

