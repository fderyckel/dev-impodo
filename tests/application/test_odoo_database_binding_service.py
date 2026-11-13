"""Require verified first review and Organization-wide reset authorization."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest import TestCase
from unittest.mock import MagicMock
from uuid import uuid4

from impodo.application.odoo_database_binding_ports import OdooDatabaseBindingRepository
from impodo.application.odoo_database_binding_service import OdooDatabaseBindingService
from impodo.application.workspace.access import (
    WorkspaceAccessContext,
    WorkspaceAccessService,
)
from impodo.domain.odoo_database_binding import (
    OdooDatabaseBinding,
    OdooDatabaseBindingError,
    OdooDatabaseBindingRole,
    OdooDatabaseGeneration,
    database_scope_hash,
)
from impodo.domain.shared.access import (
    LOCAL_ACTOR,
    Actor,
    AuthorizationError,
    Capability,
    CapabilityAuthorizationPolicy,
)
from impodo.domain.shared.tenancy import (
    OrganizationId,
    ProjectVisibilityScope,
)
from impodo.web.composition.local_identity import LocalRequestIdentityResolver


class OdooDatabaseBindingServiceTests(TestCase):
    def setUp(self):
        self.now = datetime.now(UTC)
        self.workspace_id, self.project_id = str(uuid4()), str(uuid4())
        self.lineage = WorkspaceAccessContext(
            self.project_id, self.workspace_id, str(uuid4()), str(uuid4())
        )
        self.access_repository = MagicMock()
        self.access_repository.resolve_workspace_access_context.return_value = (
            self.lineage
        )
        self.authorization = CapabilityAuthorizationPolicy()
        self.access = WorkspaceAccessService(self.access_repository, self.authorization)
        self.repository = MagicMock(spec=OdooDatabaseBindingRepository)
        self.identity = LocalRequestIdentityResolver(clock=lambda: self.now).resolve(
            {"authenticated": True}
        )
        self.scope_hash = database_scope_hash("https://odoo.example.test", "demo")
        self.current = OdooDatabaseGeneration(
            str(self.identity.organization_scope.organization_id), self.scope_hash, 0
        )
        self.repository.current.return_value = self.current
        self.repository.get.return_value = None
        self.repository.bind.side_effect = lambda binding, **kwargs: binding
        self.repository.declare.side_effect = lambda generation, **kwargs: generation
        self.review_admission = MagicMock()
        self.service = OdooDatabaseBindingService(
            self.access,
            self.authorization,
            self.repository,
            review_admission=self.review_admission,
            clock=lambda: self.now,
        )
        self.arguments = {
            "base_url": "https://odoo.example.test",
            "database": "demo",
            "actor": LOCAL_ACTOR,
        }
        self.request = {
            "scope": self.identity.organization_scope,
            "authorization_receipt": self.identity.authorization,
        }

    def test_first_review_and_reset_use_explicit_verified_receipts(self):
        current = self.service.current(
            self.workspace_id,
            capability=Capability.SOURCE_CONFIGURE,
            **self.arguments,
            **self.request,
        )
        self.assertEqual(current.generation, 0)
        binding = self.service.bind(
            self.workspace_id,
            "SOURCE",
            expected_generation_hash=current.generation_hash,
            operation_id=str(uuid4()),
            capability=Capability.SOURCE_CONFIGURE,
            **self.arguments,
            **self.request,
        )
        self.assertEqual(binding.project_id, self.project_id)
        self.review_admission.assert_called_once_with(
            self.lineage, OdooDatabaseBindingRole.SOURCE, self.scope_hash
        )
        self.assertEqual(
            binding.organization_id,
            str(self.identity.organization_scope.organization_id),
        )
        generation = self.service.declare_recreated(
            self.workspace_id,
            expected_generation_hash=current.generation_hash,
            operation_id=str(uuid4()),
            reason="The database was recreated",
            **self.arguments,
            **self.request,
        )
        self.assertEqual(generation.generation, 1)
        self.assertEqual(generation.actor, LOCAL_ACTOR.identity)

    def test_first_review_admission_denial_cannot_seed_old_workspace_authority(self):
        self.review_admission.side_effect = OdooDatabaseBindingError(
            "Earlier load journal requires a fresh workspace"
        )
        with self.assertRaisesRegex(OdooDatabaseBindingError, "fresh workspace"):
            self.service.bind(
                self.workspace_id,
                "DESTINATION",
                expected_generation_hash=self.current.generation_hash,
                operation_id=str(uuid4()),
                capability=Capability.EXPORT_PLAN_APPROVE,
                **self.arguments,
                **self.request,
            )
        self.review_admission.assert_called_once_with(
            self.lineage, OdooDatabaseBindingRole.DESTINATION, self.scope_hash
        )
        self.repository.bind.assert_not_called()
        self.repository.declare.assert_not_called()

    def test_existing_proof_reuse_does_not_admit_the_workspace_again(self):
        proof = OdooDatabaseBinding(
            self.workspace_id,
            self.project_id,
            "impodo-local",
            OdooDatabaseBindingRole.DESTINATION,
            self.scope_hash,
            self.current.generation_hash,
            LOCAL_ACTOR.identity,
            self.now,
            str(uuid4()),
        )
        self.repository.get.return_value = proof
        self.repository.bind.return_value = proof
        self.repository.bind.side_effect = None
        self.assertEqual(
            self.service.bind(
                self.workspace_id,
                "DESTINATION",
                expected_generation_hash=self.current.generation_hash,
                operation_id=str(uuid4()),
                capability=Capability.EXPORT_PLAN_APPROVE,
                **self.arguments,
                **self.request,
            ),
            proof,
        )
        self.review_admission.assert_not_called()
        self.repository.bind.assert_called_once()

    def test_denied_project_capability_never_opens_generation_registry(self):
        denied = Actor(LOCAL_ACTOR.identity, frozenset({Capability.PROJECT_VIEW}))
        with self.assertRaises(AuthorizationError):
            self.service.require_current(
                self.workspace_id,
                "DESTINATION",
                capability=Capability.EXPORT_PLAN_EXECUTE,
                **{**self.arguments, "actor": denied},
            )
        self.repository.assert_current.assert_not_called()
        self.access_repository.resolve_workspace_access_context.assert_not_called()

    def test_scope_actor_visibility_and_expiry_mismatches_fail_before_ledger_access(
        self,
    ):
        foreign = OrganizationId("foreign")
        bad_receipts = (
            replace(self.identity.authorization, principal_id="someone-else"),
            replace(
                self.identity.authorization,
                organization_id=foreign,
                project_visibility=ProjectVisibilityScope(
                    foreign, organization_wide=True
                ),
            ),
            replace(
                self.identity.authorization,
                project_visibility=ProjectVisibilityScope(
                    self.identity.organization_scope.organization_id,
                    frozenset({str(uuid4())}),
                ),
            ),
            replace(
                self.identity.authorization,
                issued_at=self.now - timedelta(hours=2),
                expires_at=self.now - timedelta(hours=1),
            ),
            replace(
                self.identity.authorization,
                actor_identity=replace(LOCAL_ACTOR.identity, subject_id="someone-else"),
            ),
        )
        for receipt in bad_receipts:
            with (
                self.subTest(receipt=receipt.receipt_id),
                self.assertRaises(AuthorizationError),
            ):
                self.service.current(
                    self.workspace_id,
                    capability=Capability.SOURCE_CONFIGURE,
                    **self.arguments,
                    scope=self.identity.organization_scope,
                    authorization_receipt=receipt,
                )
        self.repository.current.assert_not_called()

    def test_project_admin_without_organization_wide_receipt_cannot_declare_reset(self):
        receipt = replace(
            self.identity.authorization,
            project_visibility=ProjectVisibilityScope(
                self.identity.organization_scope.organization_id,
                frozenset({self.project_id}),
            ),
        )
        with self.assertRaises(AuthorizationError):
            self.service.declare_recreated(
                self.workspace_id,
                expected_generation_hash=self.current.generation_hash,
                operation_id=str(uuid4()),
                reason="Reset",
                **self.arguments,
                scope=self.identity.organization_scope,
                authorization_receipt=receipt,
            )
        self.repository.current.assert_not_called()
        self.repository.declare.assert_not_called()

    def test_worker_checks_stored_authority_without_minting_or_request_receipt(self):
        proof = OdooDatabaseBinding(
            self.workspace_id,
            self.project_id,
            "impodo-local",
            OdooDatabaseBindingRole.DESTINATION,
            self.scope_hash,
            self.current.generation_hash,
            LOCAL_ACTOR.identity,
            self.now,
            str(uuid4()),
        )
        self.repository.assert_current.return_value = proof
        result = self.service.require_current(
            self.workspace_id,
            "DESTINATION",
            capability=Capability.EXPORT_PLAN_EXECUTE,
            **self.arguments,
        )
        self.assertEqual(result, proof)
        self.repository.assert_current.assert_called_once_with(
            self.workspace_id,
            OdooDatabaseBindingRole.DESTINATION,
            self.scope_hash,
            project_id=self.project_id,
        )
        self.repository.bind.assert_not_called()
        self.repository.current.assert_not_called()
