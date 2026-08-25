"""Verify the Project-owned workspace access boundary."""

from __future__ import annotations

from pathlib import Path
import asyncio
import shutil
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from impodo.access import (
    Actor,
    ActorIdentity,
    AuthorizationError,
    Capability,
    CapabilityAuthorizationPolicy,
    LOCAL_ACTOR,
)
from impodo.adapters.duckdb.migration_foundation_database import (
    MigrationFoundationDatabase,
)
from impodo.adapters.duckdb.migration_foundation_repository import (
    MigrationFoundationRepository,
)
from impodo.data_versions import DataVersionService
from impodo.migration_foundation import (
    MigrationFoundationError,
    MigrationIdentifierConfusionError,
    MigrationNotFoundError,
)
from impodo.migration_projects import MigrationProjectService
from impodo.migration_runs import MigrationRunService
from impodo.migration_workspaces import MigrationWorkspaceService
from impodo.workspace_access import (
    WorkspaceAccessContext,
    WorkspaceAccessService,
)
from impodo.workspace_state import WorkspaceStateService
from impodo.web.security import WorkspaceAccessMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, StreamingResponse


ROOT = Path(__file__).resolve().parents[1]


class ProjectMembershipPolicy:
    """Test hosted-style Project membership without changing local policy."""

    def __init__(self, allowed_project_ids: set[str]) -> None:
        self.allowed_project_ids = allowed_project_ids
        self.calls: list[tuple[Capability, str | None]] = []

    def require(
        self,
        actor: Actor,
        capability: Capability,
        *,
        project_id: str | None = None,
    ) -> None:
        capability = Capability(capability)
        self.calls.append((capability, project_id))
        if not actor.has(capability):
            raise AuthorizationError("Missing capability")
        if project_id is not None and project_id not in self.allowed_project_ids:
            raise AuthorizationError("Actor is not a member of this Project")


class WorkspaceAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.root = ROOT / ".tmp" / f"workspace-access-{uuid4()}"
        self.root.mkdir()
        self.database = MigrationFoundationDatabase(self.root)
        self.repository = MigrationFoundationRepository(self.database)
        authorization = CapabilityAuthorizationPolicy()
        self.projects = MigrationProjectService(self.repository, authorization)
        self.data_versions = DataVersionService(self.repository, authorization)
        self.runs = MigrationRunService(self.repository, authorization)
        self.workspaces = MigrationWorkspaceService(
            self.repository,
            authorization,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _create_workspace(self, label: str) -> WorkspaceAccessContext:
        project = self.projects.create(
            actor=LOCAL_ACTOR,
            display_name=f"{label} Project",
            migration_purpose="Test exact workspace ownership",
            source_system_identity=f"{label} ERP",
        )
        data_version = self.data_versions.create(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_workspace_revision=project.optimistic_revision,
            purpose="AUTHORING",
            label=f"{label} representative export",
        )
        project = self.projects.get(project.project_id, actor=LOCAL_ACTOR)
        run = self.runs.create(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_workspace_revision=project.optimistic_revision,
            data_version_id=data_version.data_version_id,
            purpose="AUTHORING",
            label=f"{label} authoring run",
        )
        project = self.projects.get(project.project_id, actor=LOCAL_ACTOR)
        workspace = self.workspaces.create(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_workspace_revision=project.optimistic_revision,
            data_version_id=data_version.data_version_id,
            migration_run_id=run.migration_run_id,
            display_name=f"{label} mapping workspace",
        )
        return WorkspaceAccessContext(
            project_id=project.project_id,
            workspace_id=workspace.workspace_id,
            data_version_id=data_version.data_version_id,
            migration_run_id=run.migration_run_id,
        )

    def test_two_projects_authorize_the_actual_workspace_owner(self) -> None:
        alpha = self._create_workspace("Alpha")
        beta = self._create_workspace("Beta")
        policy = ProjectMembershipPolicy({alpha.project_id})
        service = WorkspaceAccessService(self.repository, policy)
        actor = Actor(
            identity=ActorIdentity(
                issuer="urn:impodo:test",
                subject_id="alpha-data-manager",
                display_name="Alpha data manager",
            ),
            capabilities=frozenset(
                {Capability.PROJECT_VIEW, Capability.PROJECT_EDIT}
            ),
        )

        self.assertEqual(
            service.resolve(
                alpha.workspace_id,
                actor=actor,
                capability=Capability.PROJECT_VIEW,
            ),
            alpha,
        )
        with self.assertRaises(AuthorizationError):
            service.resolve(
                beta.workspace_id,
                actor=actor,
                capability=Capability.PROJECT_VIEW,
            )

        self.assertEqual(
            policy.calls,
            [
                (Capability.PROJECT_VIEW, None),
                (Capability.PROJECT_VIEW, alpha.project_id),
                (Capability.PROJECT_VIEW, None),
                (Capability.PROJECT_VIEW, beta.project_id),
            ],
        )

    def test_registry_resolver_is_one_read_and_opens_no_workspace_store(self) -> None:
        expected = self._create_workspace("Bounded")
        original_connect = self.database.connect
        with (
            patch.object(
                self.database,
                "connect",
                side_effect=original_connect,
            ) as connect,
            patch.object(
                self.database,
                "ensure_workspace_store",
                side_effect=AssertionError("workspace store must stay closed"),
            ) as ensure_workspace_store,
        ):
            actual = self.repository.resolve_workspace_access_context(
                expected.workspace_id
            )

        self.assertEqual(actual, expected)
        self.assertEqual(actual.run_purpose, "AUTHORING")
        connect.assert_called_once_with(self.database.registry_path)
        ensure_workspace_store.assert_not_called()

    def test_unknown_or_wrong_kind_identity_fails_closed(self) -> None:
        expected = self._create_workspace("Exact")

        with self.assertRaises(MigrationNotFoundError):
            self.repository.resolve_workspace_access_context(
                expected.project_id
            )
        with self.assertRaises(MigrationNotFoundError):
            self.repository.resolve_workspace_access_context(str(uuid4()))
        with self.assertRaises(MigrationFoundationError):
            self.repository.resolve_workspace_access_context("not-a-uuid")

    def test_unverified_recipe_application_linkage_fails_closed(self) -> None:
        expected = self._create_workspace("Application")
        with self.database.connect(self.database.registry_path) as connection:
            connection.execute(
                "UPDATE migration_workspace SET recipe_application_id = ? "
                "WHERE workspace_id = ?",
                [str(uuid4()), expected.workspace_id],
            )

        with self.assertRaises(MigrationNotFoundError):
            self.repository.resolve_workspace_access_context(
                expected.workspace_id
            )

    def test_capability_failure_precedes_registry_access(self) -> None:
        repository = Mock()
        service = WorkspaceAccessService(
            repository,
            ProjectMembershipPolicy(set()),
        )
        actor = Actor(
            identity=ActorIdentity(
                issuer="urn:impodo:test",
                subject_id="no-access",
                display_name="No access",
            ),
            capabilities=frozenset(),
        )

        with self.assertRaises(AuthorizationError):
            service.resolve(
                str(uuid4()),
                actor=actor,
                capability=Capability.PROJECT_VIEW,
            )

        repository.resolve_workspace_access_context.assert_not_called()

    def test_repository_cannot_substitute_another_workspace_identity(self) -> None:
        requested_workspace_id = str(uuid4())
        repository = Mock()
        repository.resolve_workspace_access_context.return_value = (
            WorkspaceAccessContext(
                project_id=str(uuid4()),
                workspace_id=str(uuid4()),
                data_version_id=str(uuid4()),
                migration_run_id=str(uuid4()),
            )
        )
        service = WorkspaceAccessService(
            repository,
            CapabilityAuthorizationPolicy(),
        )

        with self.assertRaises(MigrationIdentifierConfusionError):
            service.resolve(
                requested_workspace_id,
                actor=LOCAL_ACTOR,
                capability=Capability.PROJECT_VIEW,
            )

    def test_workspace_service_authorizes_parent_before_child_repository(self) -> None:
        alpha = self._create_workspace("Alpha service")
        beta = self._create_workspace("Beta service")
        policy = ProjectMembershipPolicy({alpha.project_id})
        access = WorkspaceAccessService(self.repository, policy)
        child_repository = Mock()
        service = WorkspaceStateService(
            child_repository,
            access,
        )
        actor = Actor(
            identity=ActorIdentity(
                issuer="urn:impodo:test",
                subject_id="alpha-editor",
                display_name="Alpha editor",
            ),
            capabilities=frozenset({Capability.PROJECT_REGISTER}),
        )

        with self.assertRaises(AuthorizationError):
            service.register(
                beta.workspace_id,
                actor=actor,
                expected_revision=1,
            )

        self.assertEqual(child_repository.method_calls, [])
        self.assertEqual(
            policy.calls,
            [
                (Capability.PROJECT_REGISTER, None),
                (Capability.PROJECT_REGISTER, beta.project_id),
            ],
        )

    def test_route_denials_are_opaque_and_precede_every_route_boundary(self) -> None:
        alpha = self._create_workspace("Alpha route")
        beta = self._create_workspace("Beta route")
        policy = ProjectMembershipPolicy({alpha.project_id})
        access = WorkspaceAccessService(self.repository, policy)
        actor = Actor(
            identity=ActorIdentity(
                issuer="urn:impodo:test",
                subject_id="alpha-browser",
                display_name="Alpha browser user",
            ),
            capabilities=frozenset(
                {Capability.PROJECT_VIEW, Capability.PROJECT_EDIT}
            ),
        )
        middleware = WorkspaceAccessMiddleware(
            _unused_asgi_app,
            access=access,
            actor=actor,
        )

        denial_responses = []
        for workspace_id in (
            beta.workspace_id,
            beta.project_id,
            str(uuid4()),
            "not-a-uuid",
        ):
            route_boundary = AsyncMock(
                side_effect=AssertionError(
                    "child store, vault, artifact, or Odoo boundary must not run"
                )
            )
            request = _workspace_request(workspace_id)
            with patch.object(
                self.database,
                "ensure_workspace_store",
                side_effect=AssertionError("workspace store must stay closed"),
            ):
                response = asyncio.run(
                    middleware.dispatch(request, route_boundary)
                )
            denial_responses.append((response.status_code, response.body))
            route_boundary.assert_not_awaited()

        self.assertEqual(
            denial_responses,
            [(404, b"Workspace not found")] * 4,
        )

        allowed_request = _workspace_request(alpha.workspace_id)

        async def allowed_boundary(_request):
            self.assertEqual(
                access.resolve(
                    alpha.workspace_id,
                    actor=actor,
                    capability=Capability.PROJECT_EDIT,
                ),
                alpha,
            )
            with self.assertRaises(MigrationIdentifierConfusionError):
                access.resolve(
                    beta.workspace_id,
                    actor=actor,
                    capability=Capability.PROJECT_VIEW,
                )
            return PlainTextResponse("ok")

        with patch.object(
            self.repository,
            "resolve_workspace_access_context",
            wraps=self.repository.resolve_workspace_access_context,
        ) as registry_resolver:
            response = asyncio.run(
                middleware.dispatch(allowed_request, allowed_boundary)
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            allowed_request.state.workspace_access_context,
            alpha,
        )
        registry_resolver.assert_called_once_with(alpha.workspace_id)

    def test_streaming_body_reuses_the_verified_request_context(self) -> None:
        alpha = self._create_workspace("Streaming")
        policy = ProjectMembershipPolicy({alpha.project_id})
        access = WorkspaceAccessService(self.repository, policy)
        actor = Actor(
            identity=ActorIdentity(
                issuer="urn:impodo:test",
                subject_id="stream-reader",
                display_name="Stream reader",
            ),
            capabilities=frozenset(
                {
                    Capability.PROJECT_VIEW,
                    Capability.PROTECTED_EVIDENCE_READ,
                }
            ),
        )
        middleware = WorkspaceAccessMiddleware(
            _unused_asgi_app,
            access=access,
            actor=actor,
        )

        async def stream_boundary(_request):
            async def body():
                self.assertEqual(
                    access.resolve(
                        alpha.workspace_id,
                        actor=actor,
                        capability=Capability.PROTECTED_EVIDENCE_READ,
                    ),
                    alpha,
                )
                yield b"protected evidence"

            return StreamingResponse(body())

        async def exercise_stream() -> tuple[int, bytes]:
            response = await middleware.dispatch(
                _workspace_request(alpha.workspace_id),
                stream_boundary,
            )
            chunks = [chunk async for chunk in response.body_iterator]
            return response.status_code, b"".join(chunks)

        with patch.object(
            self.repository,
            "resolve_workspace_access_context",
            wraps=self.repository.resolve_workspace_access_context,
        ) as registry_resolver:
            status_code, body = asyncio.run(exercise_stream())

        self.assertEqual(status_code, 200)
        self.assertEqual(body, b"protected evidence")
        registry_resolver.assert_called_once_with(alpha.workspace_id)

    def test_verified_job_packet_avoids_a_second_registry_read(self) -> None:
        alpha = self._create_workspace("Background job")
        beta = self._create_workspace("Wrong background job")
        policy = ProjectMembershipPolicy({alpha.project_id, beta.project_id})
        access = WorkspaceAccessService(self.repository, policy)
        actor = Actor(
            identity=ActorIdentity(
                issuer="urn:impodo:test",
                subject_id="job-reader",
                display_name="Job reader",
            ),
            capabilities=frozenset({Capability.PROJECT_VIEW}),
        )

        async def route_boundary(_request):
            return PlainTextResponse("ok")

        middleware = WorkspaceAccessMiddleware(
            _unused_asgi_app,
            access=access,
            actor=actor,
            trusted_context_resolver=lambda _path, _workspace_id: alpha,
        )
        with patch.object(
            self.repository,
            "resolve_workspace_access_context",
            side_effect=AssertionError("verified job packet must be reused"),
        ):
            response = asyncio.run(
                middleware.dispatch(
                    _workspace_request(alpha.workspace_id),
                    route_boundary,
                )
            )
        self.assertEqual(response.status_code, 200)

        confused = WorkspaceAccessMiddleware(
            _unused_asgi_app,
            access=access,
            actor=actor,
            trusted_context_resolver=lambda _path, _workspace_id: beta,
        )
        with patch.object(
            self.repository,
            "resolve_workspace_access_context",
            side_effect=AssertionError("mismatched packet must fail closed"),
        ):
            response = asyncio.run(
                confused.dispatch(
                    _workspace_request(alpha.workspace_id),
                    route_boundary,
                )
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.body, b"Workspace not found")


async def _unused_asgi_app(scope, receive, send) -> None:
    raise AssertionError("Base middleware app must not be called directly")


def _workspace_request(workspace_id: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": f"/workspaces/{workspace_id}/summary",
            "raw_path": f"/workspaces/{workspace_id}/summary".encode(),
            "query_string": b"",
            "headers": (),
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
            "session": {"authenticated": True},
        }
    )
