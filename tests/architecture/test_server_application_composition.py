"""Prove that the shared application graph can be assembled for a server."""

from __future__ import annotations

import ast
import inspect
import unittest
from importlib.util import resolve_name
from unittest.mock import Mock

from impodo.web.composition import application_graph as application_graph_module
from impodo.web.composition import odoo_source_graph as odoo_source_graph_module
from impodo.web.composition import server_active_work as server_active_work_module
from impodo.web.composition import server_app as server_app_module
from impodo.web.composition import server_cli as server_cli_module
from impodo.web.composition import server_config as server_config_module
from impodo.web.composition import server_control_app as server_control_app_module
from impodo.web.composition import (
    server_linux_migration as server_linux_migration_module,
)
from impodo.web.composition import server_linux_web as server_linux_web_module
from impodo.web.composition import server_linux_storage as server_linux_storage_module
from impodo.web.composition import server_lifecycle as server_lifecycle_module
from impodo.web.composition import server_linux_runtime as server_linux_runtime_module
from impodo.web.composition import server_runtime as server_runtime_module
from impodo.web.composition import server_runtime_app as server_runtime_app_module
from impodo.web.composition import server_storage as server_storage_module
from impodo.web.composition import server_uvicorn as server_uvicorn_module
from impodo.web.composition import service_graph as service_graph_module
from impodo.web.composition.application_graph import (
    BrowserQueryPorts,
    WebInfrastructureDependencies,
    WebJobRuntimes,
    WebRequestDependencies,
    WebTargetDependencies,
    WorkflowSupportPorts,
    build_application_service_graph,
    build_web_context,
    build_workflow_support_service_graph,
)
from impodo.web.composition.odoo_source_graph import (
    OdooSourcePorts,
    build_odoo_source_service_graph,
)
from impodo.web.composition.service_graph import (
    build_authoring_service_graph,
    build_correction_service_graph,
    build_cutover_service_graph,
    build_execution_service_graph,
    build_foundation_service_graph,
    build_preparation_service_graph,
    build_run_service_graph,
)


def _fake(name: str) -> Mock:
    return Mock(name=name)


class ServerApplicationCompositionTests(unittest.TestCase):
    def test_shared_server_graph_has_no_local_implementation_imports(self) -> None:
        modules = (
            application_graph_module,
            odoo_source_graph_module,
            server_active_work_module,
            server_app_module,
            server_cli_module,
            server_config_module,
            server_control_app_module,
            server_linux_web_module,
            server_linux_storage_module,
            server_lifecycle_module,
            server_runtime_module,
            server_runtime_app_module,
            server_storage_module,
            server_uvicorn_module,
            service_graph_module,
        )
        forbidden_modules = (
            "duckdb",
            "impodo.adapters",
            "impodo.application.correction_jobs",
            "impodo.application.odoo_capture_job_service",
            "impodo.application.preflight_jobs",
            "impodo.application.run.recipe_run_jobs",
            "impodo.application.workspace.execution.load_jobs",
            "impodo.web.composition.incompatible_project_storage",
            "impodo.web.composition.local_app",
            "impodo.web.composition.local_identity",
            "impodo.web.composition.preparation_job_manager",
        )
        forbidden_symbols = {
            "CorrectionJobManager",
            "InlineJobDispatcher",
            "LoadJobManager",
            "LocalArtifactStore",
            "LocalRequestIdentityResolver",
            "OdooCaptureJobManager",
            "PreflightJobManager",
            "PreparationJobManager",
            "RecipeRunJobManager",
        }

        imported_modules: set[str] = set()
        imported_symbols: set[str] = set()
        for module in modules:
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_modules.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imported_name = node.module or ""
                    if node.level:
                        relative_name = f"{'.' * node.level}{imported_name}"
                        imported_name = resolve_name(
                            relative_name,
                            module.__package__ or "",
                        )
                    imported_modules.add(imported_name)
                    imported_symbols.update(alias.name for alias in node.names)

        self.assertFalse(
            any(
                imported == forbidden or imported.startswith(f"{forbidden}.")
                for imported in imported_modules
                for forbidden in forbidden_modules
            ),
            imported_modules,
        )
        self.assertTrue(forbidden_symbols.isdisjoint(imported_symbols))

    def test_linux_runtime_imports_only_approved_server_adapters(self) -> None:
        tree = ast.parse(inspect.getsource(server_linux_runtime_module))
        adapter_imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.startswith("impodo.adapters")
        }

        self.assertEqual(
            adapter_imports,
            {
                "impodo.adapters.postgresql.startup",
                "impodo.adapters.secrets.linux_files",
            },
        )

    def test_linux_migration_imports_only_approved_server_adapters(self) -> None:
        tree = ast.parse(inspect.getsource(server_linux_migration_module))
        adapter_imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.startswith("impodo.adapters")
        }

        self.assertEqual(
            adapter_imports,
            {
                "impodo.adapters.postgresql.migrations",
                "impodo.adapters.postgresql.startup",
                "impodo.adapters.secrets.linux_files",
            },
        )

    def test_fake_server_ports_assemble_the_complete_application_graph(self) -> None:
        authorization = _fake("authorization")
        foundation_repository = _fake("foundation_repository")
        workspace_states = _fake("workspace_states")
        artifacts = _fake("artifacts")
        source_repository = _fake("source_repository")
        schema_repository = _fake("schema_repository")
        mapping_repository = _fake("mapping_repository")
        quality_repository = _fake("quality_repository")
        staging_repository = _fake("staging_repository")
        run_planning_repository = _fake("run_planning_repository")
        cutover_plan_repository = _fake("cutover_plan_repository")

        foundation = build_foundation_service_graph(
            foundation_repository,
            workspace_states,
            authorization,
        )
        odoo_sources = build_odoo_source_service_graph(
            workspace_access=foundation.workspace_access,
            ports=OdooSourcePorts(
                capture_workspaces=workspace_states,
                capture_selections=source_repository,
                capture_schemas=schema_repository,
                capture_filters=_fake("capture_filters"),
                relationship_scopes=source_repository,
                provenance_workspaces=workspace_states,
                provenance_selections=source_repository,
                provenance_store=_fake("provenance_store"),
                publication_selections=source_repository,
                publication_store=_fake("publication_store"),
                source_artifacts=artifacts,
                secrets=_fake("secrets"),
                provenance_codec=_fake("provenance_codec"),
                comparison_codec=_fake("comparison_codec"),
            ),
        )
        authoring = build_authoring_service_graph(
            foundation=foundation,
            foundation_repository=foundation_repository,
            authorization=authorization,
            workspaces=workspace_states,
            sources=source_repository,
            mapping_sources=_fake("mapping_sources"),
            schemas=schema_repository,
            mappings=mapping_repository,
            quality=quality_repository,
            derived_entities=_fake("derived_entities"),
            references=_fake("references"),
            recipe_parameters=_fake("recipe_parameters"),
            supporting_lookups=_fake("supporting_lookups"),
            business_intents=_fake("business_intents"),
            downstream_invalidator=_fake("downstream_invalidator"),
            recipe_applications=_fake("recipe_applications"),
            row_inclusion=_fake("row_inclusion"),
            matching_order=_fake("matching_order"),
            transformation_impacts=_fake("transformation_impacts"),
            recipes=_fake("recipes"),
            artifacts=artifacts,
            capture_filters=_fake("authoring_capture_filters"),
        )
        runs = build_run_service_graph(
            foundation=foundation,
            authoring=authoring,
            planning_repository=run_planning_repository,
            test_run_repository=_fake("test_runs"),
            production_run_repository=_fake("production_runs"),
            cutover_plan_repository=cutover_plan_repository,
            authorization=authorization,
        )
        preparation = build_preparation_service_graph(
            workspaces=workspace_states,
            sources=source_repository,
            derived_entities=_fake("preparation_derived_entities"),
            mappings=mapping_repository,
            staging=staging_repository,
            sessions=_fake("preparation_sessions"),
            artifacts=artifacts,
            authorization=foundation.workspace_access,
            quality_repository=quality_repository,
            normalization_repository=_fake("normalization_repository"),
            resolution_repository=_fake("resolution_repository"),
            preflight_repository=_fake("preflight_repository"),
            schemas=schema_repository,
            columnar_transformations=_fake("columnar_transformations"),
            odoo_provenance=odoo_sources.provenance,
            recipe_quality=_fake("recipe_quality"),
        )
        execution = build_execution_service_graph(
            workspaces=workspace_states,
            preflight=preparation.preflight,
            journal=_fake("execution_journal"),
            reconciliation_results=_fake("reconciliation_results"),
            sources=source_repository,
            staging=staging_repository,
            schemas=schema_repository,
            artifacts=artifacts,
            authorization=foundation.workspace_access,
            provenance=odoo_sources.provenance,
            current_read_credential_binding=_fake("credential_binding"),
            fallout_workbook_renderer=_fake("fallout_workbook_renderer"),
        )
        cutover = build_cutover_service_graph(
            foundation=foundation,
            preparation=preparation,
            execution=execution,
            run_planning=run_planning_repository,
            repository=cutover_plan_repository,
            mappings=mapping_repository,
            staging=staging_repository,
            quality=quality_repository,
            authorization=authorization,
        )
        corrections = build_correction_service_graph(
            foundation=foundation,
            authoring=authoring,
            preparation=preparation,
            bindings=_fake("correction_bindings"),
            protected=_fake("protected_corrections"),
            mappings=mapping_repository,
            sources=source_repository,
            prepared=_fake("prepared_evidence"),
            preflight_records=_fake("preflight_records"),
            execution=_fake("correction_execution"),
            reconciliations=_fake("correction_reconciliations"),
            artifacts=artifacts,
            target=_fake("correction_target"),
            review_pipeline_factory=_fake("review_pipeline_factory"),
            authorization=authorization,
        )
        job_runtimes = WebJobRuntimes(
            recipe_runs=_fake("recipe_jobs"),
            preparation=_fake("preparation_jobs"),
            preflight=_fake("preflight_jobs"),
            load=_fake("load_jobs"),
            corrections=_fake("correction_jobs"),
            odoo_capture=_fake("capture_jobs"),
        )
        support = build_workflow_support_service_graph(
            foundation=foundation,
            authoring=authoring,
            runs=runs,
            execution=execution,
            ports=WorkflowSupportPorts(
                queries=BrowserQueryPorts(
                    workspace_states=workspace_states,
                    sources=source_repository,
                    derived_entities=_fake("derived_queries"),
                    schemas=schema_repository,
                    mappings=mapping_repository,
                    mapping_field_catalogs=_fake("mapping_fields"),
                    quality=quality_repository,
                    transformation_impacts=_fake("impact_queries"),
                    mapping_sources=_fake("mapping_source_queries"),
                ),
                workspace_owners=foundation_repository,
                navigation=_fake("navigation"),
                workspace_catalogs=workspace_states,
                source_catalogs=source_repository,
                derived_sources=source_repository,
                derived_entities=_fake("support_derived_entities"),
                supporting_lookups=_fake("support_lookups"),
                preparation_recovery=_fake("preparation_recovery"),
                recovery_workspace_states=workspace_states,
            ),
            artifacts=artifacts,
            jobs=job_runtimes,
            odoo_sources=odoo_sources,
            connection_tester=_fake("connection_tester"),
            read_identity_probe=_fake("read_identity_probe"),
        )
        services = build_application_service_graph(
            foundation=foundation,
            authoring=authoring,
            runs=runs,
            preparation=preparation,
            execution=execution,
            cutover=cutover,
            corrections=corrections,
            support=support,
        )
        context = build_web_context(
            services,
            request=WebRequestDependencies(
                project_queries=_fake("project_queries"),
                identity_resolver=_fake("identity_resolver"),
                unavailable_projects=(),
            ),
            jobs=job_runtimes,
            infrastructure=WebInfrastructureDependencies(
                destination_create_fields=_fake("destination_create_fields"),
                artifacts=artifacts,
                authorization=authorization,
                jobs=_fake("job_dispatcher"),
                secret_store=_fake("secret_store"),
            ),
            target=WebTargetDependencies(
                connection_tester=_fake("connection_tester"),
                read_identity_probe=_fake("read_identity_probe"),
                write_identity_probe=_fake("write_identity_probe"),
                schema_reader=_fake("schema_reader"),
                schema_read_session_factory=_fake("schema_session_factory"),
                model_catalog_reader=_fake("model_catalog_reader"),
                readiness_reader=_fake("readiness_reader"),
                destination_match_reader=_fake("destination_match_reader"),
                source_capture_factory=_fake("source_capture_factory"),
                write_executor_factory=_fake("write_executor_factory"),
                readback_reader_factory=_fake("readback_reader_factory"),
                local_target=None,
                remote_connections=_fake("remote_connections"),
            ),
        )

        self.assertIs(context.workspace_access, foundation.workspace_access)
        self.assertIs(context.odoo_source_capture, odoo_sources.capture)
        self.assertIs(context.odoo_capture_publication, odoo_sources.publication)
        self.assertIs(context.odoo_provenance, odoo_sources.provenance)
        self.assertIsNone(context.local_target)


if __name__ == "__main__":
    unittest.main()
