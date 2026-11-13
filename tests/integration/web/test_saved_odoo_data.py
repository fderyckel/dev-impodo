"""Exercise saved-data browser authorization, bulk choices, jobs, and package I/O."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.duckdb.odoo_saved_source_company import (
    OdooSavedSourceCompanyRepository,
)
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.odoo_saved_data_jobs import SavedOdooActionError, SavedOdooJob
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo_source_policy import odoo_source_policy_hash
from impodo.domain.project.foundation import MigrationNotFoundError
from impodo.domain.workspace.contracts import SchemaField
from impodo.web.composition.saved_odoo_restore import SavedRestoreResult
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    _browser_schema,
    _BrowserOdooCaptureGateway,
)
from tests.support.identity import resolve_local_actor


class _MajorGateway(_BrowserOdooCaptureGateway):
    def probe_schema(self, request, context, *, cancellation=None):
        snapshot = _browser_schema(self.workspace_state)
        return replace(
            snapshot,
            fingerprint=replace(
                snapshot.fingerprint, odoo_version=self.schema.odoo_version
            ),
        )


class _QueuedActions:
    def __init__(self):
        self.work = None
        self.job = None

    def enqueue(self, project_id, saved_data_id, *, kind, work, operation_id=""):
        self.work = work
        self.job = SavedOdooJob(
            str(uuid4()),
            project_id,
            saved_data_id,
            kind,
            "QUEUED",
            0,
            "Waiting to start",
            "",
            datetime.now(UTC),
            operation_id=operation_id,
        )
        return self.job

    def get(self, project_id, saved_data_id, job_id):
        if self.job is None or (project_id, saved_data_id, job_id) != (
            self.job.project_id,
            self.job.saved_data_id,
            self.job.job_id,
        ):
            raise LookupError("Saved Odoo data action not found")
        return self.job

    def shutdown(self):
        pass


class SavedOdooDataBrowserTests(ProjectSetupBrowserTestCase):
    def setUp(self):
        super().setUp()
        self.context = self.app.state.context
        self.actor = resolve_local_actor(self.context)

    def _capture(self, major=19):
        state, schema = self._registered_remote_schema_workspace()
        company = OdooCompanySelection(
            schema.connection_target_hash, 1, "Source company"
        )
        OdooSavedSourceCompanyRepository(
            self.context.sources.sources._database
        ).set_source_company(
            state.workspace_id,
            company,
            actor=self.actor,
            expected_revision=self.context.queries.get(state.workspace_id).revision,
        )
        state = self.context.queries.get(state.workspace_id)
        schema = replace(
            schema,
            odoo_version=f"{major}.0",
            policy_hash=odoo_source_policy_hash(major),
            read_context_hash=company.context_hash,
        )
        self.context.schema_workspace.schemas.save_odoo_schema_catalog(
            state.workspace_id, schema, actor=self.actor
        )
        response = self._post(
            f"/workspaces/{state.workspace_id}/sources/odoo-selection",
            {
                "csrf_token": self.csrf,
                "dataset_name": "clean_contacts",
                "model": "res.partner",
                "field_names": "name",
                "include_archived": "",
                "page_size": "100",
            },
        )
        self.assertEqual(response.status_code, 303, response.text)
        publication = self.context.odoo_capture_publication.publish(
            state.workspace_id, _MajorGateway(state, schema), actor=self.actor
        )
        self.context.data_version_source_projection.accept_odoo_capture(
            state.workspace_id,
            publication.source_selection,
            publication.source_snapshots,
            publication.manifests,
            actor=self.actor,
        )
        saved = self.context.saved_odoo_data
        self.assertIsNotNone(saved)
        definition = saved.create(
            state.workspace_id, actor=self.actor, label=f"Clean Odoo {major}"
        )
        definition, version = saved.retain_current(
            definition.project_id,
            definition.saved_data_id,
            actor=self.actor,
            expected_revision=definition.revision,
            operation_id=str(uuid4()),
        )
        return definition, version, company

    @staticmethod
    def _url(definition):
        return f"/projects/{definition.project_id}/saved-odoo-data"

    def _restore_form(self, *, action="check", **extra):
        return {
            "csrf_token": self.csrf,
            "operation_id": str(uuid4()),
            "version_number": "1",
            "profile_id": "",
            "odoo_connection_mode": "REMOTE",
            "odoo_base_url": "https://fresh.example.test",
            "odoo_database": "fresh_test",
            "destination_company_name": "Destination company",
            "api_key": "fictional-destination-secret",
            "policy_default": "use_saved",
            "policy__res.partner__name": "keep_destination",
            "keys__res.partner": "name",
            "action": action,
            **extra,
        }

    def _checked_result(self, *, issues=(), default_rows=()):
        def check(context, project_id, saved_data_id, version_number, **arguments):
            profile = arguments["profile"]
            repository = context.saved_odoo_data.repository
            try:
                old = repository.get_restore_profile(
                    project_id, saved_data_id, profile.profile_id
                )
            except MigrationNotFoundError:
                old = None
            stored = repository.save_restore_profile(
                profile, expected_revision=old.revision if old else None
            )
            return SavedRestoreResult(
                str(uuid4()),
                stored.profile_id,
                f"{self._url(self.definition)}?selected={saved_data_id}&profile={stored.profile_id}",
                False,
                issues=issues,
                default_rows=default_rows,
            )

        return check

    def test_saved_page_shows_two_levels_and_stays_offline_for_both_majors(self):
        for major in (19, 20):
            with self.subTest(major=major):
                definition, _version, _company = self._capture(major)
                with (
                    patch(
                        "impodo.web.routers.saved_odoo_data.saved_source_companies",
                        side_effect=AssertionError("source must stay offline"),
                    ),
                    collect_duckdb_request_timings() as timings,
                ):
                    page = self.client.get(
                        f"{self._url(definition)}?selected={definition.saved_data_id}"
                    )
                self.assertEqual(page.status_code, 200, page.text)
                self.assertIn("Level 1 · What to preserve", page.text)
                self.assertIn("Level 2 · The preserved records", page.text)
                self.assertIn("Update saved data", page.text)
                self.assertIn("Restore all", page.text)
                self.assertIn('name="saved_data_id"', page.text)
                self.assertLessEqual(timings.connection_count, 8)
                schema = self.context.queries.get_odoo_schema_catalog(
                    _version.workspace_id
                )
                extra = SchemaField(
                    "x_company_setting",
                    "Company-specific setting",
                    "char",
                    False,
                    False,
                    None,
                    None,
                    (),
                    stored=True,
                    company_dependent=True,
                    exportable=True,
                )
                expanded = replace(
                    schema,
                    models=(
                        replace(
                            schema.models[0], fields=(*schema.models[0].fields, extra)
                        ),
                    ),
                )
                with patch.object(
                    type(self.context.queries),
                    "get_odoo_schema_catalog",
                    return_value=expanded,
                ):
                    coverage = self.client.get(
                        f"{self._url(definition)}?selected={definition.saved_data_id}"
                    )
                self.assertIn("Fields outside this saved scope", coverage.text)
                self.assertIn("Company-specific setting", coverage.text)
                self.assertIn("Company-dependent values are outside", coverage.text)

    def test_configure_uses_fetched_company_choices_and_rejects_unknown_ids(self):
        definition, version, company = self._capture()
        form = {
            "csrf_token": self.csrf,
            "workspace_id": version.workspace_id,
            "label": "Clean configuration",
            "source_company_id": "999",
            "all_fields": "yes",
            "operation_id": str(uuid4()),
        }
        with (
            patch(
                "impodo.web.routers.saved_odoo_data.saved_source_companies",
                return_value=(company,),
            ),
            patch.object(
                self.context.saved_odoo_data, "configure", return_value=definition
            ) as configure,
        ):
            rejected = self._post(f"{self._url(definition)}/configure", form)
            self.assertEqual(rejected.status_code, 422, rejected.text)
            configure.assert_not_called()
            del form["all_fields"]
            rejected = self._post(f"{self._url(definition)}/configure", form)
            checkbox = re.search(
                r'<input\b[^>]*name="all_fields"[^>]*>', rejected.text
            ).group()
            self.assertNotIn("checked", checkbox)
            form["all_fields"] = "yes"
            form["source_company_id"] = str(company.company_id)
            accepted = self._post(f"{self._url(definition)}/configure", form)
            self.assertEqual(accepted.status_code, 303, accepted.text)
            self.assertEqual(configure.call_args.kwargs["source_company"], company)
            self.assertTrue(configure.call_args.kwargs["all_fields"])

    def test_configuration_revision_keeps_saved_data_immutable(self):
        definition, version, company = self._capture()
        with patch(
            "impodo.web.routers.saved_odoo_data.saved_source_companies",
            return_value=(company,),
        ):
            response = self._post(
                f"{self._url(definition)}/configure",
                {
                    "csrf_token": self.csrf,
                    "workspace_id": version.workspace_id,
                    "saved_data_id": definition.saved_data_id,
                    "revision": str(definition.revision),
                    "label": "Revised configuration",
                    "source_company_id": str(company.company_id),
                    "all_fields": "yes",
                    "operation_id": str(uuid4()),
                },
            )
        self.assertEqual(response.status_code, 303, response.text)
        current = self.context.saved_odoo_data.get(
            definition.project_id, definition.saved_data_id, actor=self.actor
        )
        self.assertEqual(current.configuration_revision, 2)
        self.assertEqual(current.latest_version, 1)
        retained = self.context.saved_odoo_data.repository.get_version(
            definition.project_id, definition.saved_data_id, 1
        )
        self.assertEqual(retained, version)

    def test_update_checks_csrf_and_queues_only_the_whole_configuration(self):
        definition, version, _company = self._capture()
        actions = _QueuedActions()
        self.context.saved_odoo_jobs = actions
        endpoint = f"{self._url(definition)}/{definition.saved_data_id}/update"
        rejected = self.client.post(
            endpoint,
            data={
                "csrf_token": "wrong",
                "revision": str(definition.revision),
                "operation_id": str(uuid4()),
            },
            headers=POST_HEADERS,
        )
        self.assertEqual(rejected.status_code, 403)
        self.assertIsNone(actions.work)
        with patch.object(
            self.context.saved_odoo_data, "refresh", return_value=version
        ) as refresh:
            response = self._post(
                endpoint,
                {
                    "csrf_token": self.csrf,
                    "revision": str(definition.revision),
                    "operation_id": str(uuid4()),
                },
            )
            self.assertEqual(response.status_code, 303, response.text)
            self.assertEqual(actions.job.kind, "UPDATE")
            refresh.assert_not_called()
            actions.work(lambda _percent, _message: None)
            self.assertEqual(
                refresh.call_args.args[:2],
                (definition.project_id, definition.saved_data_id),
            )
            operation_id = refresh.call_args.kwargs["operation_id"]
            actions.job = replace(actions.job, status="FAILED")
            retry_page = self.client.get(
                f"{self._url(definition)}?selected={definition.saved_data_id}&job={actions.job.job_id}"
            )
            self.assertIn(
                f'name="operation_id" value="{operation_id}"', retry_page.text
            )

    def test_check_is_read_only_and_remembers_field_choices_without_reflecting_key(
        self,
    ):
        self.definition, _version, _company = self._capture()
        endpoint = (
            f"{self._url(self.definition)}/{self.definition.saved_data_id}/restore"
        )
        with patch(
            "impodo.web.routers.saved_odoo_data.restore_saved_odoo_data",
            side_effect=self._checked_result(),
        ) as check:
            response = self._post(endpoint, self._restore_form())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(check.call_args.kwargs["check_only"])
        self.assertNotIn("fictional-destination-secret", response.text)
        profiles = self.context.saved_odoo_data.repository.list_restore_profiles(
            self.definition.project_id, self.definition.saved_data_id
        )
        rule = json.loads(profiles[0].settings_json)["rules"]["res.partner"][0]
        self.assertEqual(rule["policy"], "use_saved")
        page = self.client.get(
            f"{self._url(self.definition)}?selected={self.definition.saved_data_id}&profile={profiles[0].profile_id}"
        )
        self.assertIn("Used to find matching records", page.text)
        self.assertRegex(page.text, r'name="policy__res.partner__name"\s+disabled')
        self.assertIn('value="fresh_test"', page.text)
        unchanged = self.client.get(
            f"{self._url(self.definition)}?selected={self.definition.saved_data_id}&unchanged=yes"
        )
        self.assertIn("No records needed changing.", unchanged.text)

    def test_checked_required_defaults_accept_bulk_value_only_for_checked_fields(self):
        self.definition, _version, _company = self._capture(20)
        endpoint = (
            f"{self._url(self.definition)}/{self.definition.saved_data_id}/restore"
        )
        rows = (
            {
                "model": "res.partner",
                "name": "x_required",
                "label": "New required value",
                "type": "integer",
                "has_odoo_default": False,
                "value": None,
            },
        )
        with patch(
            "impodo.web.routers.saved_odoo_data.restore_saved_odoo_data",
            side_effect=self._checked_result(
                issues=("A required value is missing",), default_rows=rows
            ),
        ):
            response = self._post(endpoint, self._restore_form())
        self.assertEqual(response.status_code, 422, response.text)
        profile = self.context.saved_odoo_data.repository.list_restore_profiles(
            self.definition.project_id, self.definition.saved_data_id
        )[0]
        self.assertIn('name="default_value__res.partner__x_required"', response.text)
        self.assertNotRegex(
            response.text, r'name="default_value__res.partner__x_required" value="None"'
        )
        values = self._restore_form(profile_id=profile.profile_id)
        values["default_provider__res.partner__x_required"] = "fixed_value"
        values["default_value__res.partner__x_required"] = "0"
        with patch(
            "impodo.web.routers.saved_odoo_data.restore_saved_odoo_data",
            side_effect=self._checked_result(),
        ) as check:
            accepted = self._post(endpoint, values)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        submitted = json.loads(check.call_args.kwargs["profile"].settings_json)
        self.assertEqual(submitted["defaults"]["res.partner"]["x_required"]["value"], 0)

    def test_unresolved_background_restore_exposes_failed_action_recovery(self):
        self.definition, _version, _company = self._capture()
        actions = _QueuedActions()
        self.context.saved_odoo_jobs = actions
        endpoint = (
            f"{self._url(self.definition)}/{self.definition.saved_data_id}/restore"
        )
        with patch(
            "impodo.web.routers.saved_odoo_data.restore_saved_odoo_data",
            side_effect=self._checked_result(issues=("Review the removed field",)),
        ):
            response = self._post(
                endpoint, self._restore_form(action="restore", confirm_restore="yes")
            )
            self.assertEqual(response.status_code, 303, response.text)
            with self.assertRaises(SavedOdooActionError) as caught:
                actions.work(lambda _percent, _message: None)
        self.assertIn("profile=", caught.exception.redirect_url)
        review = self.client.get(caught.exception.redirect_url)
        self.assertIn("Review the removed field", review.text)

    def test_restore_requires_confirmation_and_rejects_unadmitted_field_names(self):
        definition, _version, _company = self._capture()
        endpoint = f"{self._url(definition)}/{definition.saved_data_id}/restore"
        actions = _QueuedActions()
        self.context.saved_odoo_jobs = actions
        with patch(
            "impodo.web.routers.saved_odoo_data.restore_saved_odoo_data"
        ) as restore:
            response = self._post(endpoint, self._restore_form(action="restore"))
            self.assertEqual(response.status_code, 422)
            bad = self._restore_form(action="restore", confirm_restore="yes")
            bad["policy__res.partner__invented"] = "use_saved"
            response = self._post(endpoint, bad)
            self.assertEqual(response.status_code, 422)
            restore.assert_not_called()
            self.assertIsNone(actions.work)
            accepted = self._post(
                endpoint, self._restore_form(action="restore", confirm_restore="yes")
            )
            self.assertEqual(accepted.status_code, 303, accepted.text)
            self.assertEqual(actions.job.kind, "RESTORE")
            restore.assert_not_called()

    def test_download_cleans_temporary_package_and_import_admits_a_bounded_file(self):
        definition, _version, _company = self._capture()
        archive = self.context.saved_odoo_archive
        paths = []

        def export(_project, _saved, _version, *, destination, password, actor):
            self.assertEqual(password, "fictional-package-password")
            destination.write_bytes(b"encrypted package")
            paths.append(destination)
            return destination

        def ingest(_project, path, *, password, actor):
            self.assertEqual(path.read_bytes(), b"encrypted package")
            self.assertEqual(password, "fictional-package-password")
            paths.append(path)
            return definition

        with patch.object(archive, "export_version", side_effect=export):
            response = self._post(
                f"{self._url(definition)}/{definition.saved_data_id}/versions/1/download",
                {
                    "csrf_token": self.csrf,
                    "package_password": "fictional-package-password",
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, b"encrypted package")
        self.assertFalse(paths[-1].exists())
        with patch.object(archive, "import_version", side_effect=ingest):
            response = self.client.post(
                f"{self._url(definition)}/import",
                data={
                    "csrf_token": self.csrf,
                    "package_password": "fictional-package-password",
                },
                files={
                    "package_file": (
                        "saved.impodo",
                        b"encrypted package",
                        "application/octet-stream",
                    )
                },
                headers=POST_HEADERS,
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303, response.text)
        self.assertFalse(paths[-1].exists())

    def test_import_rejects_oversize_and_reports_unavailable_archive(self):
        definition, _version, _company = self._capture()
        with (
            patch("impodo.web.routers.saved_odoo_data._UPLOAD_LIMIT", 4),
            patch.object(self.context.saved_odoo_archive, "import_version") as ingest,
        ):
            response = self.client.post(
                f"{self._url(definition)}/import",
                data={
                    "csrf_token": self.csrf,
                    "package_password": "fictional-password",
                },
                files={
                    "package_file": (
                        "saved.impodo",
                        b"too large",
                        "application/octet-stream",
                    )
                },
                headers=POST_HEADERS,
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 413)
            ingest.assert_not_called()
        self.context.saved_odoo_archive = None
        response = self._post(
            f"{self._url(definition)}/{definition.saved_data_id}/versions/1/download",
            {"csrf_token": self.csrf, "package_password": "fictional-password"},
        )
        self.assertEqual(response.status_code, 503)
