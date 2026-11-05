"""Revalidate verified key renewal without accepting unrelated stale decisions."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from impodo.application.supporting_review_service import supporting_review_packet
from impodo.domain.workspace.derived_entities import DerivedEntityPlan
from impodo.domain.workspace.supporting_review import SupportingChoice
from tests.application.workspace import test_supporting_review as fixtures


class SupportingAccessRevalidationTests(unittest.TestCase):
    def scenario(self, major):
        fixture = fixtures.SupportingReviewApplicationTests()
        fixture.setUp()
        previous = SimpleNamespace(
            **{
                **vars(fixture.schema),
                "odoo_version": f"{major}.0",
                "pending_refresh": None,
                "policy_hash": "policy",
                "connection_mode": "REMOTE",
                "database": "fictional",
            }
        )
        current = SimpleNamespace(
            **{**vars(previous), "read_credential_binding_hash": "new-key"}
        )
        old_packet = supporting_review_packet(
            fixture.workspace, fixture.selection, previous, None, None
        )
        review = fixture.service.save(
            old_packet,
            expected_binding_hash=old_packet.binding_hash,
            expected_version=0,
            operation_id=str(uuid4()),
            choices=(SupportingChoice("res.partner", "country_id", "REUSE"),),
            actor=fixture.actor,
        )
        fixture.repository.rebind_supporting_review.side_effect = (
            lambda _id, saved, **_kw: saved
        )
        packet = supporting_review_packet(
            fixture.workspace, fixture.selection, current, None, None
        )
        return fixture, previous, packet, review

    def test_key_only_change_revalidates_both_majors_with_immutable_history(self):
        for major in (19, 20):
            with self.subTest(major=major):
                fixture, previous, packet, review = self.scenario(major)
                serialized = review.to_json()
                saved = fixture.service.revalidate_access(
                    packet, previous, review, actor=fixture.actor
                )
                self.assertEqual(saved.version, review.version + 1)
                self.assertEqual(saved.binding_hash, packet.binding_hash)
                self.assertEqual(saved.choices, review.choices)
                self.assertEqual(review.to_json(), serialized)
                self.assertNotEqual(saved.operation_id, review.operation_id)
                self.assertEqual(fixture.service.validate_review(packet, saved), saved)

    def test_real_access_fields_defaults_and_existing_staleness_are_not_reapproved(
        self,
    ):
        for major in (19, 20):
            for changed in (
                "read_context_hash",
                "read_principal_hash",
                "read_permission_hash",
                "content_hash",
                "policy_hash",
                "connection_target_hash",
                "odoo_version",
                "models",
                "source",
                "preparation",
                "scope",
                "saved_binding",
                "matching_keys",
            ):
                with self.subTest(major=major, changed=changed):
                    fixture, previous, packet, review = self.scenario(major)
                    if changed == "saved_binding":
                        review = replace(
                            review, binding_hash="stale-before-key-renewal"
                        )
                    elif changed == "matching_keys":
                        review = replace(
                            review,
                            choices=(replace(review.choices[0], key_fields=("name",)),),
                        )
                    elif changed in {"source", "preparation", "scope"}:
                        packet = replace(packet, binding_hash="changed-inputs")
                        if changed == "source":
                            packet = replace(
                                packet,
                                selection=replace(
                                    packet.selection, content_hash="changed-source"
                                ),
                            )
                        elif changed == "preparation":
                            packet = replace(
                                packet,
                                preparation=DerivedEntityPlan(
                                    "plan",
                                    1,
                                    "workspace",
                                    packet.selection.content_hash,
                                    (),
                                    datetime.now(UTC),
                                    "Manager",
                                ),
                            )
                        else:
                            packet.workspace.intended_models = (
                                "res.partner",
                                "res.country",
                            )
                    elif changed == "models":
                        original = packet.schema.models[0].fields[0]
                        packet.schema.models = (
                            replace(
                                packet.schema.models[0],
                                fields=(
                                    replace(original, create_default_present=True),
                                ),
                            ),
                        )
                    else:
                        setattr(packet.schema, changed, "changed")
                    result = fixture.service.revalidate_access(
                        packet, previous, review, actor=fixture.actor
                    )
                    self.assertEqual(result, review)
                    fixture.repository.rebind_supporting_review.assert_not_called()

    def test_revalidation_authorizes_before_any_write(self):
        fixture, previous, packet, review = self.scenario(20)
        fixture.authorization.require.side_effect = PermissionError("denied")
        with self.assertRaises(PermissionError):
            fixture.service.revalidate_access(
                packet, previous, review, actor=fixture.actor
            )
        fixture.repository.rebind_supporting_review.assert_not_called()
