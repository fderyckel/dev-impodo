"""Verify the shared top-level application and web-context assembly."""

from __future__ import annotations

import ast
import inspect
import unittest
from dataclasses import replace
from types import SimpleNamespace
from typing import get_type_hints
from unittest.mock import Mock

from impodo.domain.workspace.workbench import WorkspaceStateError
from impodo.web.composition import application_graph as application_graph_module
from impodo.web.composition.application_graph import (
    WebInfrastructureDependencies,
    WebJobRuntimes,
    WebRequestDependencies,
    WebTargetDependencies,
    WorkflowSupportServiceGraph,
    build_application_service_graph,
    build_web_context,
)
from impodo.web.context import CorrectionRouteContext, LocalTargetRuntime
from impodo.web.routers.corrections import build_corrections_router


class ApplicationGraphCompositionTests(unittest.TestCase):
    def test_top_level_graph_does_not_import_concrete_adapters(self) -> None:
        tree = ast.parse(inspect.getsource(application_graph_module))
        adapter_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported = (node.module or "",)
            else:
                continue
            adapter_imports.extend(
                name for name in imported if name.startswith("impodo.adapters")
            )

        self.assertEqual(adapter_imports, [])

    def test_top_level_graph_preserves_responsibility_named_groups(self) -> None:
        groups = {
            name: Mock(name=name)
            for name in (
                "foundation",
                "authoring",
                "runs",
                "preparation",
                "execution",
                "cutover",
                "corrections",
                "support",
            )
        }

        graph = build_application_service_graph(**groups)

        for name, group in groups.items():
            self.assertIs(getattr(graph, name), group)

    def test_web_context_maps_services_and_runtime_adapters_once(self) -> None:
        foundation = SimpleNamespace(
            workspace_access=Mock(),
            workspace_states=Mock(),
            migration_projects=Mock(),
            data_versions=Mock(),
            migration_runs=Mock(),
            migration_run_target_setup=Mock(),
            migration_workspaces=Mock(),
            project_authoring=Mock(),
            odoo_source_restarts=Mock(),
        )
        authoring = SimpleNamespace(
            recipes=Mock(),
            recipe_publication=Mock(),
            source_workspace=Mock(),
            data_version_source_projection=Mock(),
            categorical_coverage=Mock(),
            schema_workspace=Mock(),
            mapping_workspace=Mock(),
            matching_order=Mock(),
            transformation_impacts=Mock(),
            row_inclusion_reviews=Mock(),
        )
        runs = SimpleNamespace(
            run_planning=Mock(),
            test_runs=Mock(),
            production_runs=Mock(),
            run_setups=Mock(),
        )
        preparation = SimpleNamespace(
            preparation=Mock(),
            quality=Mock(),
            resolution=Mock(),
            normalization=Mock(),
            preflight=Mock(),
        )
        execution = SimpleNamespace(
            execution=Mock(),
            transfer_execution=Mock(),
            reconciliation=Mock(),
            fallout_workbooks=Mock(),
        )
        cutover = SimpleNamespace(cutover_plans=Mock())
        corrections = SimpleNamespace(workflow=Mock())
        support = WorkflowSupportServiceGraph(
            queries=Mock(),
            workspace_views=Mock(),
            navigation=Mock(),
            intake=Mock(),
            inspections=Mock(),
            derived_entities=Mock(),
            supporting_lookups=Mock(),
            recipe_target_matches=Mock(),
            preparation_recovery=Mock(),
            odoo_source_capture=Mock(),
            odoo_capture_publication=Mock(),
            odoo_provenance=Mock(),
            odoo_connection_tests=Mock(),
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
        request = WebRequestDependencies(
            project_queries=Mock(),
            identity_resolver=Mock(),
            unavailable_projects=(),
        )
        jobs = WebJobRuntimes(
            recipe_runs=Mock(),
            preparation=Mock(),
            preflight=Mock(),
            load=Mock(),
            corrections=Mock(),
            odoo_capture=Mock(),
        )
        infrastructure = WebInfrastructureDependencies(
            destination_create_fields=Mock(),
            artifacts=Mock(),
            authorization=Mock(),
            jobs=Mock(),
            secret_store=Mock(),
        )
        target = WebTargetDependencies(
            connection_tester=Mock(),
            read_identity_probe=Mock(),
            write_identity_probe=Mock(),
            schema_reader=Mock(),
            schema_read_session_factory=Mock(),
            model_catalog_reader=Mock(),
            readiness_reader=Mock(),
            destination_match_reader=Mock(),
            source_capture_factory=Mock(),
            write_executor_factory=Mock(),
            readback_reader_factory=Mock(),
            local_target=LocalTargetRuntime(
                stack=Mock(),
                metadata_reader=Mock(),
            ),
            remote_connections=Mock(),
        )

        context = build_web_context(
            services,
            request=request,
            jobs=jobs,
            infrastructure=infrastructure,
            target=target,
        )

        self.assertIs(context.workspace_access, foundation.workspace_access)
        self.assertIs(context.mapping_workspace, authoring.mapping_workspace)
        self.assertIs(context.run_planning, runs.run_planning)
        self.assertIs(context.preflight, preparation.preflight)
        self.assertIs(context.execution, execution.execution)
        self.assertIs(context.cutover_plans, cutover.cutover_plans)
        self.assertIs(context.corrections, corrections.workflow)
        self.assertIs(context.queries, support.queries)
        self.assertIs(context.project_queries, request.project_queries)
        self.assertIs(context.identity_resolver, request.identity_resolver)
        self.assertIs(context.preparation_jobs, jobs.preparation)
        self.assertIs(context.authorization, infrastructure.authorization)
        self.assertIs(context.secret_store, infrastructure.secret_store)
        self.assertIs(context.read_identity_probe, target.read_identity_probe)
        self.assertIsNotNone(target.local_target)
        if target.local_target is not None:
            self.assertIs(context.local_stack, target.local_target.stack)
        self.assertFalse(hasattr(context, "launch_token"))

        server_context = build_web_context(
            services,
            request=request,
            jobs=jobs,
            infrastructure=infrastructure,
            target=replace(target, local_target=None),
        )
        self.assertIsNone(server_context.local_target)
        with self.assertRaisesRegex(WorkspaceStateError, "local Odoo controls"):
            _ = server_context.local_stack

    def test_correction_router_declares_its_narrow_context(self) -> None:
        annotation = get_type_hints(build_corrections_router)["context"]

        self.assertIs(annotation, CorrectionRouteContext)


if __name__ == "__main__":
    unittest.main()
