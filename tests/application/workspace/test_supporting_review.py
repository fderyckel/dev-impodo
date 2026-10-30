"""Business choices, exact supporting scopes, authorization and freshness."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from uuid import uuid4

from impodo.application.supporting_review_service import (
    SupportingReviewService,
    supporting_review_packet,
)
from impodo.domain.shared.access import Actor, ActorIdentity, Capability
from impodo.domain.workspace.contracts import SchemaOrigin
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.supporting_review import (
    SupportingChoice,
    SupportingChoiceAction,
    SupportingReview,
    supporting_read_request,
)
from impodo.domain.workspace.workbench import OdooConnectionMode, SourceMode
from tests.domain.workspace.test_supporting_plan import model, relation
from tests.domain.workspace.test_supporting_sources import accepted_selection


class SupportingReviewApplicationTests(unittest.TestCase):
    def setUp(self):
        self.actor = Actor(
            ActorIdentity("organization", "manager", "Manager"),
            frozenset({Capability.SCHEMA_GOVERN, Capability.PROJECT_VIEW}),
        )
        self.selection = accepted_selection()
        self.workspace = SimpleNamespace(
            workspace_id="workspace",
            source_mode=SourceMode.FILE,
            intended_models=("res.partner",),
            odoo_connection_mode=OdooConnectionMode.REMOTE,
            odoo_base_url="https://odoo.example.test",
            odoo_database="fictional",
        )
        self.schema = SimpleNamespace(
            origin=SchemaOrigin.LIVE_API,
            content_hash="schema",
            connection_target_hash="target",
            odoo_version="19.0",
            read_credential_binding_hash="credential",
            read_principal_hash="principal",
            read_permission_hash="permission",
            read_context_hash="context",
            models=(model("res.partner", relation("country_id", "res.country")),),
        )
        self.packet = supporting_review_packet(
            self.workspace, self.selection, self.schema, None, None
        )
        self.repository, self.authorization = Mock(), Mock()
        self.repository.get_supporting_review.return_value = None
        self.repository.save_supporting_review.side_effect = (
            lambda _workspace, review, **kwargs: review
        )
        self.service = SupportingReviewService(self.repository, self.authorization)

    def save(self, choices=None, **values):
        return self.service.save(
            self.packet,
            expected_binding_hash=values.get("binding", self.packet.binding_hash),
            expected_version=0,
            operation_id=str(uuid4()),
            choices=choices
            if choices is not None
            else (SupportingChoice("res.partner", "country_id", "REUSE"),),
            actor=self.actor,
        )

    def test_exact_reviewed_scope_round_trip_and_grouped_requirements(self):
        review = self.save()
        self.assertEqual(review, SupportingReview.from_json(review.to_json()))
        choice = review.choices[0]
        self.assertEqual(
            (choice.key_fields, choice.scope_fields, choice.display_field),
            (("code",), (), "name"),
        )
        request = supporting_read_request(self.schema, None, choice)
        self.assertFalse(request.write_use)
        self.assertFalse(request.all_fields)
        self.assertEqual(request.requested_fields, ("code", "name"))
        requirements = self.service.requirements(self.packet, review, actor=self.actor)
        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0].model_name, "res.country")
        self.assertEqual(self.workspace.intended_models, ("res.partner",))

    def test_required_optional_default_and_managed_choices_are_distinct(self):
        with self.assertRaisesRegex(WorkspaceError, "required supporting"):
            self.save(())
        for action in ("SKIP_OPTIONAL", "CHECKED_DEFAULT", "ODOO_MANAGED"):
            with self.subTest(action=action), self.assertRaises(WorkspaceError):
                self.save((SupportingChoice("res.partner", "country_id", action),))
        for flag, action in (
            ("create_default_present", "CHECKED_DEFAULT"),
            ("computed", "ODOO_MANAGED"),
        ):
            original = self.schema.models[0].fields[0]
            self.schema.models = (
                model("res.partner", replace(original, **{flag: True})),
            )
            packet = supporting_review_packet(
                self.workspace, self.selection, self.schema, None, None
            )
            normalized = self.service.normalize(
                packet, (SupportingChoice("res.partner", "country_id", action),)
            )
            self.assertEqual(normalized[0].action.value, action)

    def test_incoming_requires_accepted_data_and_explicit_write_scope(self):
        choice = SupportingChoice("res.partner", "country_id", "INCOMING", "products")
        with self.assertRaisesRegex(WorkspaceError, "write scope"):
            self.save((choice,))
        self.workspace.intended_models = ("res.partner", "res.country")
        packet = supporting_review_packet(
            self.workspace, self.selection, self.schema, None, None
        )
        self.assertEqual(
            self.service.normalize(packet, (choice,))[0].dataset_id, "products"
        )
        with self.assertRaisesRegex(WorkspaceError, "accepted incoming"):
            self.service.normalize(packet, (replace(choice, dataset_id="foreign"),))

    def test_forged_read_scope_and_foreign_edge_are_rejected(self):
        for choice in (
            SupportingChoice("res.partner", "country_id", "REUSE", key_fields=("id",)),
            SupportingChoice(
                "res.partner", "country_id", "REUSE", scope_fields=("secret",)
            ),
            SupportingChoice("res.partner", "password", "REUSE"),
        ):
            with self.subTest(choice=choice), self.assertRaises(WorkspaceError):
                self.save((choice,))
        self.repository.save_supporting_review.assert_not_called()

    def test_stale_source_schema_access_and_policy_bindings_block_confirmation(self):
        review = self.save()
        for name in (
            "content_hash",
            "read_principal_hash",
            "read_context_hash",
            "read_credential_binding_hash",
            "read_permission_hash",
        ):
            old = getattr(self.schema, name)
            setattr(self.schema, name, "changed")
            packet = supporting_review_packet(
                self.workspace, self.selection, self.schema, None, None
            )
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(WorkspaceError, "need refresh"),
            ):
                self.service.require_current(packet, actor=self.actor, review=review)
            setattr(self.schema, name, old)
        source = replace(self.selection, content_hash="new-source")
        packet = supporting_review_packet(
            self.workspace, source, self.schema, None, None
        )
        with self.assertRaisesRegex(WorkspaceError, "need refresh"):
            self.service.require_current(packet, actor=self.actor, review=review)
        with self.assertRaisesRegex(WorkspaceError, "data changed"):
            self.save(binding="stale")

    def test_denial_precedes_repository_access_and_packet_revision_is_not_mutated(self):
        self.authorization.require.side_effect = PermissionError("denied")
        with self.assertRaises(PermissionError):
            self.service.current("workspace", actor=self.actor)
        self.repository.get_supporting_review.assert_not_called()
        with self.assertRaises(PermissionError):
            self.save()
        self.repository.save_supporting_review.assert_not_called()

    def test_review_hash_rejects_tampering_and_conflicting_duplicate_edges(self):
        review = self.save()
        with self.assertRaisesRegex(ValueError, "hash"):
            SupportingReview.from_json(
                review.to_json().replace(
                    '"actor_subject":"manager"', '"actor_subject":"other"'
                )
            )
        with self.assertRaisesRegex(ValueError, "only once"):
            replace(review, choices=(*review.choices, *review.choices))
