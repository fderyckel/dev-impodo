"""Test request identity resolution across local and server compositions."""

from __future__ import annotations

from datetime import datetime, timezone
import unittest

from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.shared.tenancy import AuthenticationMethod, PrincipalKind
from impodo.web.composition.local_identity import (
    LOCAL_ORGANIZATION_ID,
    LocalRequestIdentityResolver,
)
from impodo.web.composition.server_app import (
    ServerCompositionError,
    create_server_app,
)


NOW = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)


class RequestIdentityCompositionTests(unittest.TestCase):
    def test_local_identity_requires_the_launch_session(self) -> None:
        with self.assertRaisesRegex(PermissionError, "not authenticated"):
            LocalRequestIdentityResolver(clock=lambda: NOW).resolve({})

    def test_local_identity_uses_implicit_organization_and_operator(self) -> None:
        resolved = LocalRequestIdentityResolver(
            clock=lambda: NOW,
        ).resolve({"authenticated": True})

        self.assertEqual(resolved.actor, LOCAL_ACTOR)
        self.assertEqual(resolved.principal.kind, PrincipalKind.LOCAL)
        self.assertEqual(
            resolved.principal.authentication_method,
            AuthenticationMethod.LOCAL_LAUNCH,
        )
        self.assertEqual(
            resolved.organization_scope.organization_id,
            LOCAL_ORGANIZATION_ID,
        )
        self.assertTrue(resolved.authorization.project_visibility.organization_wide)

    def test_server_composition_fails_without_local_fallbacks(self) -> None:
        with self.assertRaisesRegex(
            ServerCompositionError,
            "PostgreSQL control-plane",
        ):
            create_server_app()


if __name__ == "__main__":
    unittest.main()
