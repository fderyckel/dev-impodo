"""Test deployment-neutral application service graph wiring."""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from impodo.web.composition.service_graph import (
    build_authoring_service_graph,
    build_correction_service_graph,
    build_cutover_service_graph,
    build_execution_service_graph,
    build_foundation_service_graph,
    build_preparation_service_graph,
    build_run_service_graph,
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


class RunServiceGraphTests(unittest.TestCase):
    def test_builder_reuses_foundation_and_authoring_services_for_runs(self) -> None:
        foundation = Mock()
        foundation.migration_projects = Mock()
        foundation.data_versions = Mock()
        foundation.migration_runs = Mock()
        foundation.migration_workspaces = Mock()
        foundation.source_packages = Mock()
        foundation.workspace_states = Mock()
        authoring = Mock()
        authoring.recipes = Mock()
        authoring.source_projections = Mock()
        authoring.recipe_application = Mock()
        planning_repository = Mock()
        planning_repository.foundation = Mock()
        test_run_repository = Mock()
        production_run_repository = Mock()
        cutover_plan_repository = Mock()
        authorization = Mock()

        graph = build_run_service_graph(
            foundation=foundation,
            authoring=authoring,
            planning_repository=planning_repository,
            test_run_repository=test_run_repository,
            production_run_repository=production_run_repository,
            cutover_plan_repository=cutover_plan_repository,
            authorization=authorization,
        )

        self.assertIs(graph.run_planning.projects, foundation.migration_projects)
        self.assertIs(graph.run_planning.data_versions, foundation.data_versions)
        self.assertIs(graph.run_planning.recipes, authoring.recipes)
        self.assertIs(graph.run_planning.repository, planning_repository)
        self.assertIs(graph.run_planning.test_run_values, test_run_repository)
        self.assertIs(graph.run_planning.cutover_plans, cutover_plan_repository)
        self.assertIs(
            graph.run_planning.source_projections,
            authoring.source_projections,
        )
        self.assertIs(graph.run_planning.compiler, authoring.recipe_application)
        self.assertIs(graph.test_runs.run_planning, graph.run_planning)
        self.assertIs(graph.test_runs.test_runs, test_run_repository)
        self.assertIs(graph.production_runs.run_planning, graph.run_planning)
        self.assertIs(
            graph.production_runs.production_runs,
            production_run_repository,
        )
        self.assertIs(
            graph.production_runs.cutover_plans,
            cutover_plan_repository,
        )
        self.assertIs(graph.run_setups.test_runs, graph.test_runs)
        self.assertIs(graph.run_setups.production_runs, graph.production_runs)
        self.assertIs(graph.run_setups.authorization, authorization)


class ExecutionServiceGraphTests(unittest.TestCase):
    def test_builder_reuses_injected_ports_across_execution_services(self) -> None:
        workspaces = Mock()
        preflight = Mock()
        journal = Mock()
        reconciliation_results = Mock()
        sources = Mock()
        staging = Mock()
        schemas = Mock()
        artifacts = Mock()
        authorization = Mock()
        provenance = Mock()
        current_read_credential_binding = Mock()
        fallout_workbook_renderer = Mock()

        graph = build_execution_service_graph(
            workspaces=workspaces,
            preflight=preflight,
            journal=journal,
            reconciliation_results=reconciliation_results,
            sources=sources,
            staging=staging,
            schemas=schemas,
            artifacts=artifacts,
            authorization=authorization,
            provenance=provenance,
            current_read_credential_binding=current_read_credential_binding,
            fallout_workbook_renderer=fallout_workbook_renderer,
        )

        self.assertIs(graph.execution.workspaces, workspaces)
        self.assertIs(graph.execution.preflight, preflight)
        self.assertIs(graph.execution.journal, journal)
        self.assertIs(graph.execution.authorization, authorization)
        self.assertTrue(graph.execution.require_remote_read_identity)
        self.assertTrue(graph.execution.require_remote_write_identity)
        self.assertIs(
            graph.execution.current_read_credential_binding,
            current_read_credential_binding,
        )
        self.assertIs(graph.transfer_execution.sources, sources)
        self.assertIs(graph.transfer_execution.artifacts, artifacts)
        self.assertIs(graph.transfer_execution.provenance, provenance)
        self.assertIs(graph.transfer_execution.execution, graph.execution)
        self.assertIs(graph.reconciliation.preflight, preflight)
        self.assertIs(graph.reconciliation.execution, journal)
        self.assertIs(graph.reconciliation.results, reconciliation_results)
        self.assertIs(graph.reconciliation.authorization, authorization)
        self.assertIs(graph.reconciliation.schemas, schemas)
        evidence = graph.reconciliation.evidence
        self.assertIsNotNone(evidence)
        if evidence is not None:
            self.assertIs(evidence._authorization, authorization)
            self.assertIs(evidence._artifacts, artifacts)
        self.assertIs(graph.fallout_workbooks._reconciliation, graph.reconciliation)
        self.assertIs(graph.fallout_workbooks._preflight, preflight)
        self.assertIs(graph.fallout_workbooks._staging, staging)
        self.assertIs(graph.fallout_workbooks._sources, sources)
        self.assertIs(graph.fallout_workbooks._workspaces, workspaces)
        self.assertIs(graph.fallout_workbooks._artifacts, artifacts)
        self.assertIs(graph.fallout_workbooks._authorization, authorization)
        self.assertIs(
            graph.fallout_workbooks._workbook_renderer,
            fallout_workbook_renderer,
        )


class CutoverServiceGraphTests(unittest.TestCase):
    def test_builder_reuses_existing_graphs_and_injected_evidence_ports(self) -> None:
        foundation = Mock()
        foundation.migration_projects = Mock()
        foundation.data_versions = Mock()
        preparation = Mock()
        preparation.preflight = Mock()
        execution = Mock()
        execution.execution = Mock()
        execution.execution.journal = Mock()
        execution.reconciliation = Mock()
        run_planning = Mock()
        repository = Mock()
        mappings = Mock()
        staging = Mock()
        quality = Mock()
        authorization = Mock()

        graph = build_cutover_service_graph(
            foundation=foundation,
            preparation=preparation,
            execution=execution,
            run_planning=run_planning,
            repository=repository,
            mappings=mappings,
            staging=staging,
            quality=quality,
            authorization=authorization,
        )

        self.assertIs(graph.qualification_evidence.mappings, mappings)
        self.assertIs(graph.qualification_evidence.staging, staging)
        self.assertIs(graph.qualification_evidence.quality, quality)
        self.assertIs(
            graph.qualification_evidence.preflight,
            preparation.preflight,
        )
        self.assertIs(
            graph.qualification_evidence.execution,
            execution.execution.journal,
        )
        self.assertIs(
            graph.qualification_evidence.reconciliation,
            execution.reconciliation,
        )
        self.assertIs(graph.cutover_plans.projects, foundation.migration_projects)
        self.assertIs(graph.cutover_plans.data_versions, foundation.data_versions)
        self.assertIs(graph.cutover_plans.run_planning, run_planning)
        self.assertIs(graph.cutover_plans.repository, repository)
        self.assertIs(
            graph.cutover_plans.evidence_reader,
            graph.qualification_evidence,
        )
        self.assertIs(graph.cutover_plans.authorization, authorization)


class CorrectionServiceGraphTests(unittest.TestCase):
    def test_builder_keeps_target_and_pipeline_choices_in_composition(self) -> None:
        foundation = Mock()
        foundation.migration_projects = Mock()
        foundation.migration_runs = Mock()
        foundation.migration_workspaces = Mock()
        foundation.workspace_states = Mock()
        foundation.migration_run_target_setup = Mock()
        foundation.source_packages = Mock()
        foundation.source_packages.repository = Mock()
        authoring = Mock()
        authoring.mapping_workspace = Mock()
        authoring.schema_workspace = Mock()
        authoring.source_projections = Mock()
        preparation = Mock()
        preparation.preparation = Mock()
        preparation.quality = Mock()
        preparation.normalization = Mock()
        preparation.preflight = Mock()
        bindings = Mock()
        protected = Mock()
        mappings = Mock()
        sources = Mock()
        prepared = Mock()
        preflight_records = Mock()
        execution = Mock()
        reconciliations = Mock()
        artifacts = Mock()
        target = Mock()
        pipeline = Mock()
        pipeline_factory = Mock(return_value=pipeline)
        authorization = Mock()

        graph = build_correction_service_graph(
            foundation=foundation,
            authoring=authoring,
            preparation=preparation,
            bindings=bindings,
            protected=protected,
            mappings=mappings,
            sources=sources,
            prepared=prepared,
            preflight_records=preflight_records,
            execution=execution,
            reconciliations=reconciliations,
            artifacts=artifacts,
            target=target,
            review_pipeline_factory=pipeline_factory,
            authorization=authorization,
        )

        self.assertIs(graph.stages.mapping.mappings, authoring.mapping_workspace)
        self.assertIs(graph.stages.preparation.preparation, preparation.preparation)
        self.assertIs(graph.stages.preparation.sessions, prepared)
        self.assertIs(graph.stages.preparation.artifacts, artifacts)
        self.assertIs(graph.stages.quality.quality, preparation.quality)
        self.assertIs(graph.stages.quality.normalization, preparation.normalization)
        self.assertIs(graph.stages.target, target)
        pipeline_factory.assert_called_once_with(graph.stages)
        self.assertIs(graph.reviewer.pipeline, pipeline)
        self.assertIs(graph.successors.runs, foundation.migration_runs)
        self.assertIs(graph.successors.projects, foundation.migration_projects)
        self.assertIs(
            graph.successors.source_packages,
            foundation.source_packages.repository,
        )
        self.assertIs(graph.successors.workspaces, foundation.migration_workspaces)
        self.assertIs(graph.successors.source_projections, authoring.source_projections)
        self.assertIs(graph.executor.execution, execution)
        self.assertIs(graph.executor.reconciliations, reconciliations)
        self.assertIs(graph.workflow.preflight, preparation.preflight)
        self.assertIs(graph.workflow.preflight_repository, preflight_records)
        self.assertIs(graph.workflow.executor, graph.executor)


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
