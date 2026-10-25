"""Focused browser evidence for one Impodo capability."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock, patch

from impodo.domain.shared.access import Capability
from impodo.domain.shared.tenancy import (
    OrganizationId,
    ProjectVisibilityScope,
    RequestOrganizationScope,
)
from tests.support.browser_scenarios import (
    LocalBrowserSecurityTestCase,
    _csrf,
    uuid4,
)
from tests.support.identity import resolve_local_actor


class LocalBrowserSecurityTests(LocalBrowserSecurityTestCase):
    def test_workspace_middleware_and_command_share_one_request_actor(self) -> None:
        """Resolve once and pass the request actor into the Workspace command."""

        self.client.get("/launch?token=launch-secret", follow_redirects=False)
        context = self.app.state.context
        workspace = self.workspaces.create(
            name="Request identity customer",
            source_system="Fictional ERP",
        )
        form = self.client.get(f"/workspaces/{workspace.workspace_id}/files")
        self.assertEqual(form.status_code, 200)

        resolver = context.identity_resolver
        local_identity = resolver.resolve({"authenticated": True})

        with (
            patch.object(
                resolver,
                "resolve",
                wraps=resolver.resolve,
            ) as resolve,
            patch.object(
                context.workspace_states,
                "register",
                wraps=context.workspace_states.register,
            ) as register,
        ):
            response = self.client.post(
                f"/workspaces/{workspace.workspace_id}/register",
                data={
                    "csrf_token": _csrf(form.text),
                    "revision": str(workspace.revision),
                },
                headers={"Origin": "http://testserver"},
            )

        self.assertEqual(response.status_code, 422)
        resolve.assert_called_once()
        self.assertEqual(register.call_args.kwargs["actor"], local_identity.actor)

    def test_project_denial_is_opaque_and_opens_no_project_or_child_store(
        self,
    ) -> None:
        launched = self.client.get(
            "/launch?token=launch-secret",
            follow_redirects=False,
        )
        self.assertEqual(launched.status_code, 303)
        context = self.app.state.context
        project = context.migration_projects.create(
            actor=resolve_local_actor(context),
            display_name="Hidden customer migration",
            migration_purpose="Verify opaque Project access",
            source_system_identity="Fictional ERP",
        )
        missing = self.client.get(f"/projects/{uuid4()}")

        local_identity = context.identity_resolver.resolve(
            {"authenticated": True}
        )
        foreign_organization = OrganizationId("foreign-organization")
        foreign_principal = replace(
            local_identity.principal,
            organization_id=foreign_organization,
        )
        foreign_identity = replace(
            local_identity,
            principal=foreign_principal,
            organization_scope=RequestOrganizationScope.for_principal(
                foreign_principal
            ),
            authorization=replace(
                local_identity.authorization,
                receipt_id="foreign-authorization",
                organization_id=foreign_organization,
                project_visibility=ProjectVisibilityScope(
                    organization_id=foreign_organization,
                    organization_wide=True,
                ),
            ),
        )
        resolver = Mock()
        resolver.resolve.return_value = foreign_identity
        context.identity_resolver = resolver

        with (
            patch.object(
                context.migration_projects.repository,
                "get_project",
                side_effect=AssertionError("Project registry must stay closed"),
            ) as project_reader,
            patch.object(
                context.data_versions,
                "list",
                side_effect=AssertionError("Data versions must stay closed"),
            ),
            patch.object(
                context.migration_workspaces,
                "list_for_project",
                side_effect=AssertionError("Workspaces must stay closed"),
            ),
        ):
            denied = self.client.get(f"/projects/{project.project_id}")

        self.assertEqual(denied.status_code, 404)
        self.assertEqual(
            (denied.status_code, denied.content),
            (missing.status_code, missing.content),
        )
        resolver.resolve.assert_called_once()
        project_reader.assert_not_called()

    def test_project_mutations_require_exact_capability_before_child_work(
        self,
    ) -> None:
        self.client.get(
            "/launch?token=launch-secret",
            follow_redirects=False,
        )
        context = self.app.state.context
        project = context.migration_projects.create(
            actor=resolve_local_actor(context),
            display_name="Read-only customer migration",
            migration_purpose="Verify mutation authorization order",
            source_system_identity="Fictional ERP",
        )
        overview = self.client.get(f"/projects/{project.project_id}")
        self.assertEqual(overview.status_code, 200)

        local_identity = context.identity_resolver.resolve(
            {"authenticated": True}
        )
        view_only_capabilities = frozenset({Capability.PROJECT_VIEW})
        resolver = Mock()
        resolver.resolve.return_value = replace(
            local_identity,
            actor=replace(
                local_identity.actor,
                capabilities=view_only_capabilities,
            ),
            authorization=replace(
                local_identity.authorization,
                capabilities=view_only_capabilities,
            ),
        )
        context.identity_resolver = resolver

        with (
            patch.object(
                context.migration_projects.repository,
                "get_project",
                side_effect=AssertionError("Project registry must stay closed"),
            ) as project_reader,
            patch.object(
                context.migration_workspaces,
                "list_for_project",
                side_effect=AssertionError("Workspace list must stay closed"),
            ) as workspace_reader,
            patch.object(
                context.recipe_publication,
                "publish",
                side_effect=AssertionError("Publication must not start"),
            ) as publication,
        ):
            deleted = self.client.post(
                f"/projects/{project.project_id}/delete",
                data={
                    "csrf_token": _csrf(overview.text),
                    "revision": str(project.optimistic_revision),
                },
                headers={"Origin": "http://testserver"},
            )
            published = self.client.post(
                f"/projects/{project.project_id}/recipes",
                data={"csrf_token": _csrf(overview.text)},
                headers={"Origin": "http://testserver"},
            )

        self.assertEqual(deleted.status_code, 404)
        self.assertEqual(published.status_code, 404)
        self.assertEqual(resolver.resolve.call_count, 2)
        project_reader.assert_not_called()
        workspace_reader.assert_not_called()
        publication.assert_not_called()

    def test_browser_session_remains_valid_for_the_launcher_lifetime(self) -> None:
        launched_at = 1_000_000
        with patch("itsdangerous.timed.time.time", return_value=launched_at):
            launched = self.client.get(
                "/launch?token=launch-secret",
                follow_redirects=False,
            )

        self.assertEqual(launched.status_code, 303)
        cookie = launched.headers["set-cookie"].casefold()
        self.assertNotIn("max-age", cookie)
        self.assertNotIn("expires", cookie)

        with patch(
            "itsdangerous.timed.time.time",
            return_value=launched_at + (12 * 60 * 60),
        ):
            health = self.client.get("/health")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})

    def test_launch_session_host_and_origin_controls(self) -> None:
        unauthenticated = self.client.get("/projects")
        self.assertEqual(unauthenticated.status_code, 401)

        wrong_host = self.client.get(
            "/launch?token=launch-secret",
            headers={"Host": "attacker.example"},
        )
        self.assertEqual(wrong_host.status_code, 400)

        launched = self.client.get(
            "/launch?token=launch-secret",
            follow_redirects=False,
        )
        self.assertEqual(launched.status_code, 303)
        cookie = launched.headers["set-cookie"].casefold()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        reused = self.client.get(
            "/launch?token=launch-secret",
            follow_redirects=False,
        )
        self.assertEqual(reused.status_code, 401)

        projects = self.client.get("/projects")
        self.assertEqual(projects.status_code, 200)
        self.assertIn(
            '<span class="brand-tagline">Prepare clean data for Odoo</span>',
            projects.text,
        )
        self.assertIn('id="app-sidebar"', projects.text)
        self.assertIn("data-sidebar-toggle", projects.text)
        self.assertIn('aria-label="Impodo workflow"', projects.text)
        self.assertIn("bootstrap-icons.svg#folder", projects.text)
        self.assertIn("Data remains on this computer.", projects.text)
        self.assertNotIn("Customer data remains on this computer.", projects.text)
        self.assertIn('class="creator-credit"', projects.text)
        self.assertIn("Made in", projects.text)
        self.assertIn("flag-luxembourg.svg", projects.text)
        self.assertEqual(projects.headers["x-frame-options"], "DENY")
        self.assertIn("frame-ancestors 'none'", projects.headers["content-security-policy"])
        self.assertEqual(self.client.get("/projects").status_code, 200)
        self.assertEqual(self.client.get("/projects/new").status_code, 200)

        csrf = _csrf(projects.text)
        missing_origin = self.client.post(
            "/projects/new",
            data={
                "csrf_token": csrf,
                "display_name": "Blocked",
                "source_mode": "FILE",
                "source_system_identity": "Other",
            },
        )
        self.assertEqual(missing_origin.status_code, 403)

        origin_fallback = self.client.post(
            "/projects/new",
            data={
                "csrf_token": csrf,
                "creation_request_id": str(uuid4()),
                "display_name": "Origin fallback",
                "source_mode": "FILE",
                "source_system_identity": "Other",
            },
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        self.assertEqual(origin_fallback.status_code, 303)

        referer_fallback = self.client.post(
            "/projects/new",
            data={
                "csrf_token": csrf,
                "creation_request_id": str(uuid4()),
                "display_name": "Referer fallback",
                "source_mode": "FILE",
                "source_system_identity": "Other",
            },
            headers={"Referer": "http://testserver/projects/new"},
            follow_redirects=False,
        )
        self.assertEqual(referer_fallback.status_code, 303)

        cross_site = self.client.post(
            "/projects/new",
            data={
                "csrf_token": csrf,
                "display_name": "Cross-site",
                "source_mode": "FILE",
                "source_system_identity": "Other",
            },
            headers={
                "Origin": "http://testserver",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        self.assertEqual(cross_site.status_code, 403)

        hostile_referer = self.client.post(
            "/projects/new",
            data={
                "csrf_token": csrf,
                "display_name": "Hostile",
                "source_mode": "FILE",
                "source_system_identity": "Other",
            },
            headers={"Referer": "http://testserver.attacker.example/projects/new"},
        )
        self.assertEqual(hostile_referer.status_code, 403)
