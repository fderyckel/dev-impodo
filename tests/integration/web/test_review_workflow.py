"""Focused browser evidence for one Impodo capability."""

from __future__ import annotations

from tests.support.browser_scenarios import (
    ConnectorAuthenticationError,
    ConnectorTransportError,
    OdooReadFailureCode,
    OdooReadWorkflowError,
    POST_HEADERS,
    PreflightRequirementPlan,
    ProjectSetupBrowserTestCase,
    SecretStoreError,
    SimpleNamespace,
    patch,
)


class ReviewWorkflowBrowserTests(ProjectSetupBrowserTestCase):
    def test_summary_reconnects_missing_remote_key_without_losing_schema(
        self,
    ) -> None:
        context = self.app.state.context
        workspace_state, schema = self._registered_remote_schema_workspace()

        def compare_with_reader(_project_id, *, reader, actor):
            self.assertEqual(actor, context.actor)
            return reader(
                PreflightRequirementPlan((), (), (), source_record_count=0)
            )

        with patch.object(
            context.preflight,
            "compare",
            side_effect=compare_with_reader,
        ):
            blocked = self.client.post(
                f"/workspaces/{workspace_state.workspace_id}/summary/compare",
                data={"csrf_token": self.csrf},
                headers=POST_HEADERS,
            )

        self.assertEqual(blocked.status_code, 422)
        self.assertIn("Enter the Odoo read key", blocked.text)
        self.assertIn('name="read_api_key"', blocked.text)
        self.assertIn('name="read_api_key_storage"', blocked.text)
        self.assertIn("Save key and compare", blocked.text)
        self.assertIn("Nothing was changed in Odoo", blocked.text)
        self.assertNotIn("We could not complete that action", blocked.text)
        self.assertEqual(self.schema_calls, [])
        self.assertEqual(self.read_identity_calls, [])

        with patch.object(context.preflight, "compare") as compare:
            reconnected = self.client.post(
                f"/workspaces/{workspace_state.workspace_id}/summary/compare",
                data={
                    "csrf_token": self.csrf,
                    "read_api_key": "replacement-read-key",
                    "read_api_key_storage": "session",
                },
                headers=POST_HEADERS,
                follow_redirects=False,
            )

        self.assertEqual(reconnected.status_code, 303, reconnected.text)
        compare.assert_called_once()
        self.assertEqual(
            self.schema_calls,
            [(workspace_state.workspace_id, "replacement-read-key")],
        )
        self.assertEqual(
            self.read_identity_calls,
            [
                (
                    workspace_state.workspace_id,
                    "replacement-read-key",
                    ("res.partner",),
                )
            ],
        )
        rebound = context.queries.get_odoo_schema_catalog(workspace_state.workspace_id)
        self.assertEqual(rebound.content_hash, schema.content_hash)
        self.assertNotEqual(
            rebound.read_credential_binding_hash,
            schema.read_credential_binding_hash,
        )
        page = self.client.get(reconnected.headers["location"])
        self.assertNotIn("replacement-read-key", page.text)
        self.assertNotIn('id="comparison-recovery"', page.text)

    def test_remote_reference_failure_returns_to_matching_without_key_form(
        self,
    ) -> None:
        context = self.app.state.context
        workspace_state, schema = self._registered_remote_schema_workspace()
        available_key = SimpleNamespace(
            available=True,
            binding_hash=schema.read_credential_binding_hash,
        )

        with (
            patch.object(
                context.preflight,
                "compare",
                side_effect=OdooReadWorkflowError(
                    OdooReadFailureCode.REFERENCE_POLICY_MISMATCH,
                    "Internal wording must not select or leak into recovery",
                    support_reference="res.partner.country_id -> res.country",
                ),
            ),
            patch(
                "impodo.web.presenters.summary.get_target_credential_status",
                return_value=available_key,
            ),
        ):
            blocked = self.client.post(
                f"/workspaces/{workspace_state.workspace_id}/summary/compare",
                data={"csrf_token": self.csrf},
                headers=POST_HEADERS,
            )

        self.assertEqual(blocked.status_code, 422)
        self.assertIn('id="comparison-recovery"', blocked.text)
        self.assertIn("Review the linked field match", blocked.text)
        self.assertIn(
            f'href="/workspaces/{workspace_state.workspace_id}/mapping"',
            blocked.text,
        )
        self.assertIn('data-read-credential-dialog', blocked.text)
        self.assertIn('data-auto-open="false"', blocked.text)
        self.assertNotIn("Internal wording", blocked.text)

    def test_remote_compare_recovery_matches_the_classified_failure(self) -> None:
        context = self.app.state.context
        workspace_state, schema = self._registered_remote_schema_workspace()
        available_key = SimpleNamespace(
            available=True,
            binding_hash=schema.read_credential_binding_hash,
        )
        cases = (
            (
                ConnectorAuthenticationError("raw rejected key response"),
                "Replace the Odoo read key",
                'name="read_api_key"',
            ),
            (
                ConnectorTransportError("HTTP 503 raw upstream response"),
                "Try the comparison again",
                f'action="/workspaces/{workspace_state.workspace_id}/summary/compare"',
            ),
            (
                OdooReadWorkflowError(
                    OdooReadFailureCode.SCHEMA_EVIDENCE_STALE,
                    "raw schema mismatch",
                ),
                "Refresh the Odoo data structure",
                f'href="/workspaces/{workspace_state.workspace_id}/schema"',
            ),
            (
                OdooReadWorkflowError(
                    OdooReadFailureCode.PREPARED_EVIDENCE_STALE,
                    "raw prepared mismatch",
                ),
                "Prepare and approve the data again",
                f'href="/workspaces/{workspace_state.workspace_id}/prepare"',
            ),
            (
                SecretStoreError("raw Windows credential-store failure"),
                "Impodo could not save the comparison",
                "COMPARISON_STORAGE_FAILED",
            ),
        )

        for failure, title, expected_action in cases:
            with self.subTest(failure=type(failure).__name__):
                with (
                    patch.object(
                        context.preflight,
                        "compare",
                        side_effect=failure,
                    ),
                    patch(
                        "impodo.web.presenters.summary.get_target_credential_status",
                        return_value=available_key,
                    ),
                ):
                    blocked = self.client.post(
                        f"/workspaces/{workspace_state.workspace_id}/summary/compare",
                        data={"csrf_token": self.csrf},
                        headers=POST_HEADERS,
                    )

                self.assertEqual(blocked.status_code, 422)
                self.assertIn(title, blocked.text)
                self.assertIn(expected_action, blocked.text)
                if not isinstance(failure, ConnectorAuthenticationError):
                    self.assertIn('data-auto-open="false"', blocked.text)
                self.assertNotIn("raw ", blocked.text)
