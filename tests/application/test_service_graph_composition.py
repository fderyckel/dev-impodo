"""Test deployment-neutral application service graph wiring."""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from impodo.web.composition.service_graph import (
    build_authoring_service_graph,
    build_foundation_service_graph,
    build_preparation_service_graph,
)


class FoundationServiceGraphTests(unittest.TestCase):
    def test_builder_reuses_injected_ports_across_foundation_services(self) -> None:
        repository = Mock()
        workspace_state_repository = Mock()
        authorization = Mock()

        graph = build_foundation_service_graph(
            repository,
            workspace_state_repository,
            authorization,
        )

        self.assertIs(graph.workspace_access.repository, repository)
        self.assertIs(graph.workspace_access.authorization, authorization)
        self.assertIs(graph.workspace_states.repository, workspace_state_repository)
        self.assertIs(graph.workspace_states.authorization, graph.workspace_access)
        self.assertIs(graph.project_authoring.projects, graph.migration_projects)
        self.assertIs(graph.project_authoring.data_versions, graph.data_versions)
        self.assertIs(graph.project_authoring.runs, graph.migration_runs)
        self.assertIs(
            graph.project_authoring.migration_workspaces,
            graph.migration_workspaces,
        )
        self.assertIs(graph.project_authoring.source_packages, graph.source_packages)
        self.assertIs(graph.project_authoring.workspace_states, graph.workspace_states)
        self.assertIs(graph.odoo_source_restarts.projects, graph.migration_projects)
        self.assertIs(
            graph.odoo_source_restarts.workspace_states, graph.workspace_states
        )


class AuthoringServiceGraphTests(unittest.TestCase):
    def test_builder_reuses_injected_ports_across_authoring_services(self) -> None:
        foundation = Mock()
        foundation.workspace_access = Mock()
        foundation.workspace_states = Mock()
        foundation.data_versions = Mock()
        foundation.migration_workspaces = Mock()
        foundation.source_packages = Mock()
        foundation_repository = Mock()
        authorization = Mock()
        workspaces = Mock()
        sources = Mock()
        mapping_sources = Mock()
        schemas = Mock()
        mappings = Mock()
        quality = Mock()
        derived_entities = Mock()
        references = Mock()
        recipe_parameters = Mock()
        supporting_lookups = Mock()
        downstream_invalidator = Mock()
        recipe_applications = Mock()
        row_inclusion = Mock()
        matching_order = Mock()
        transformation_impacts = Mock()
        recipes = Mock()
        artifacts = Mock()
        capture_filters = Mock()

        graph = build_authoring_service_graph(
            foundation=foundation,
            foundation_repository=foundation_repository,
            authorization=authorization,
            workspaces=workspaces,
            sources=sources,
            mapping_sources=mapping_sources,
            schemas=schemas,
            mappings=mappings,
            quality=quality,
            derived_entities=derived_entities,
            references=references,
            recipe_parameters=recipe_parameters,
            supporting_lookups=supporting_lookups,
            downstream_invalidator=downstream_invalidator,
            recipe_applications=recipe_applications,
            row_inclusion=row_inclusion,
            matching_order=matching_order,
            transformation_impacts=transformation_impacts,
            recipes=recipes,
            artifacts=artifacts,
            capture_filters=capture_filters,
        )

        self.assertIs(graph.recipe_compiler.sources, mapping_sources)
        self.assertIs(graph.recipe_compiler.mappings, mappings)
        self.assertIs(graph.recipe_publication.compiler, graph.recipe_compiler)
        self.assertIs(graph.recipes.repository, recipes)
        self.assertIs(graph.source_workspace.sources, sources)
        self.assertIs(
            graph.source_workspace.authorization,
            foundation.workspace_access,
        )
        self.assertIs(graph.source_projections.repository, foundation_repository)
        self.assertIs(
            graph.data_version_source_projection.projections,
            graph.source_projections,
        )
        self.assertIs(
            graph.data_version_source_projection.source_workspace,
            graph.source_workspace,
        )
        self.assertIs(
            graph.mapping_workspace.categorical_coverage,
            graph.categorical_coverage,
        )
        self.assertIs(
            graph.mapping_workspace.numeric_precision_coverage,
            graph.numeric_precision_coverage,
        )
        self.assertIs(
            graph.transformation_impacts.authorization,
            foundation.workspace_access,
        )
        self.assertIs(
            graph.row_inclusion_reviews.checked_mapping,
            graph.transformation_impacts,
        )
        self.assertIs(graph.matching_order._source_keys, graph.categorical_coverage)
        self.assertIs(graph.recipe_application.mappings, graph.mapping_workspace)
        self.assertIs(
            graph.recipe_application.row_inclusion_reviews,
            graph.row_inclusion_reviews,
        )


class PreparationServiceGraphTests(unittest.TestCase):
    def test_builder_reuses_injected_ports_across_preparation_services(self) -> None:
        workspaces = Mock()
        sources = Mock()
        derived_entities = Mock()
        mappings = Mock()
        staging = Mock()
        sessions = Mock()
        artifacts = Mock()
        authorization = Mock()
        quality_repository = Mock()
        normalization_repository = Mock()
        resolution_repository = Mock()
        preflight_repository = Mock()
        schemas = Mock()
        columnar_transformations = Mock()
        odoo_provenance = Mock()
        recipe_quality = Mock()

        graph = build_preparation_service_graph(
            workspaces=workspaces,
            sources=sources,
            derived_entities=derived_entities,
            mappings=mappings,
            staging=staging,
            sessions=sessions,
            artifacts=artifacts,
            authorization=authorization,
            quality_repository=quality_repository,
            normalization_repository=normalization_repository,
            resolution_repository=resolution_repository,
            preflight_repository=preflight_repository,
            schemas=schemas,
            columnar_transformations=columnar_transformations,
            odoo_provenance=odoo_provenance,
            recipe_quality=recipe_quality,
        )

        self.assertIs(graph.quality.mappings, mappings)
        self.assertIs(graph.quality.sources, sources)
        self.assertIs(graph.quality.quality, quality_repository)
        self.assertIs(graph.quality.recipe_quality, recipe_quality)
        self.assertIs(graph.normalization.repository, normalization_repository)
        self.assertIs(graph.normalization.authorization, authorization)
        self.assertIs(graph.resolution.repository, resolution_repository)
        self.assertIs(graph.resolution.staging, staging)
        self.assertIs(graph.preparation.quality, graph.quality)
        self.assertIs(graph.preparation.normalization, graph.normalization)
        self.assertIs(graph.preparation.resolution, graph.resolution)
        self.assertIs(
            graph.preparation.columnar_transformations,
            columnar_transformations,
        )
        self.assertIs(graph.preflight.preflight, preflight_repository)
        self.assertIs(graph.preflight.effective, resolution_repository)
        self.assertIs(graph.preflight.odoo_provenance, odoo_provenance)


if __name__ == "__main__":
    unittest.main()
