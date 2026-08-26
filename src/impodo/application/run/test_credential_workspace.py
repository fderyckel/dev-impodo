"""Resolve the one Test workspace that owns target credentials."""

from __future__ import annotations

from impodo.domain.shared.access import Actor, AuthorizationPolicy, Capability
from impodo.domain.project.foundation import require_uuid


class TestRunCredentialWorkspaceUseCase:
    """Own credential-owner resolution without opening unrelated workspaces."""

    def __init__(
        self,
        *,
        test_runs,
        workspace_states,
        authorization: AuthorizationPolicy,
    ) -> None:
        self._test_runs = test_runs
        self._workspace_states = workspace_states
        self._authorization = authorization

    def workspace(self, workspace_id: str, *, actor: Actor):
        """Return the shared Test setup workspace that owns credentials."""

        return self._workspace_states.repository.get(
            self.workspace_id(workspace_id, actor=actor)
        )

    def workspace_id(self, workspace_id: str, *, actor: Actor) -> str:
        """Return the credential owner without opening another workspace store."""

        workspace_id = require_uuid(workspace_id, "workspace_id")
        binding = self._test_runs.for_workspace(workspace_id)
        if binding is None:
            return workspace_id
        self._authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=binding.project_id,
        )
        return binding.setup_workspace_id
