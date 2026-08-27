"""Focused browser evidence for one Impodo capability."""

from __future__ import annotations

from tests.support.browser_scenarios import (
    CanonicalControlTotal,
    DatasetMapping,
    IdentityComponentMapping,
    MagicMock,
    MappingTargetMode,
    MappingValidationStatus,
    POST_HEADERS,
    Path,
    PreparationJobStatus,
    PreparationWorkspace,
    ProjectSetupBrowserTestCase,
    QualityOutcomePolicy,
    QualityOwnerRole,
    QualityRuleFamily,
    ScalarFieldMapping,
    ScalarValueSource,
    SimpleNamespace,
    TransformationImpactReport,
    TransformationImpactRow,
    _spawned_duckdb_locks,
    _wait_for_preparation,
    datetime,
    json,
    patch,
    re,
    time,
    timezone,
    unescape,
    uuid4,
)


class PreparationWorkflowBrowserTests(ProjectSetupBrowserTestCase):
    def test_saved_prepared_data_has_plain_recovery_ui(self) -> None:
        workspace_id, _dataset, _business_key = self._mapping_ready_workspace(
            scalar_field_count=0,
        )
        context = self.app.state.context
        staging = MagicMock(
            total_rows=12,
            mapping_version=3,
            run_id="f0cd6d32-80d9-4e31-9bcb-d316d83cf0b8",
            content_hash="sha256:" + "7" * 64,
            datasets=(),
        )

        with (
            patch.object(
                context.preflight,
                "current_staging",
                return_value=staging,
            ),
            patch.object(
                context.preflight,
                "current_report",
                return_value=None,
            ),
        ):
            page = self.client.get(f"/workspaces/{workspace_id}/summary")

        self.assertEqual(page.status_code, 200)
        self.assertIn("Your prepared data is safe", page.text)
        self.assertIn("12 prepared rows", page.text)
        self.assertIn("Prepare data for review", page.text)
        self.assertIn("Prepared data is stored locally", page.text)
        self.assertIn("<details", page.text)
        self.assertIn("<summary>Support details</summary>", page.text)
        self.assertNotIn("<details open", page.text)
        self.assertNotIn("canonical_staging", page.text)

    def test_prepare_rejects_bad_source_hash_before_publication_and_redirects(
        self,
    ) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=0,
        )
        context = self.app.state.context
        source_identity = dataset.columns[0]
        mapping = DatasetMapping(
            dataset_id=dataset.dataset_id,
            target_model="res.partner",
            mode=MappingTargetMode.UPSERT,
            source_identity_column_keys=(source_identity.stable_key,),
            target_identity=(
                IdentityComponentMapping(
                    source_column_keys=(source_identity.stable_key,),
                    target_fields=business_key.key_fields,
                ),
            ),
        )
        revision, validation = context.mapping_workspace.check_definition(
            workspace_id,
            datasets=(mapping,),
            expected_parent_version=None,
            expected_working_draft_version=None,
            actor=context.actor,
        )
        submission = context.mapping_workspace.submit_current(
            workspace_id,
            datasets=(mapping,),
            expected_version=revision.version,
            expected_working_draft_version=1,
            actor=context.actor,
        )
        self.assertNotEqual(validation.status, MappingValidationStatus.INVALID)
        self.assertIsNotNone(submission)
        selection = context.sources.sources.get_source_selection(workspace_id)
        assert selection is not None
        corrupted = json.loads(selection.to_json())
        corrupted["datasets"][0]["source"]["source_sha256"] = (
            "sha256:not-a-digest"
        )
        database_path = (
            context.workspace_states.repository.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        with context.workspace_states.repository._connect(database_path) as connection:
            connection.execute(
                "UPDATE source_selection SET selection_json = ? "
                "WHERE singleton_id = 1",
                [json.dumps(corrupted)],
            )

        failed = self.client.post(
            f"/workspaces/{workspace_id}/summary/check",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(failed.status_code, 303)
        self.assertIn(
            f"/workspaces/{workspace_id}/preparation/",
            failed.headers["location"],
        )
        completed_job = _wait_for_preparation(
            self.client,
            failed.headers["location"],
        )
        self.assertEqual(completed_job["status"], "FAILED")
        self.assertTrue(completed_job["failure_code"])
        self.assertIsNone(context.preflight.current_staging(workspace_id))
        self.assertIsNone(context.quality.current_summary(workspace_id))
        self.assertIsNone(
            context.normalization.current_summary(workspace_id)
        )
        self.assertEqual(self.readiness_calls, [])

        recovery = self.client.get(failed.headers["location"])
        self.assertEqual(recovery.status_code, 200)
        self.assertIn("Stored source selection is invalid", recovery.text)
        self.assertIn("data-preparation-failure-code", recovery.text)
        self.assertIn(str(completed_job["failure_code"]), recovery.text)
        retried = self.client.post(
            f"{failed.headers['location']}/retry",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(retried.status_code, 303)
        self.assertNotEqual(retried.headers["location"], failed.headers["location"])
        retried_job = _wait_for_preparation(
            self.client,
            retried.headers["location"],
        )
        self.assertEqual(retried_job["status"], "FAILED")

    def test_data_manager_can_save_an_optional_named_total(self) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=1,
            numeric_field=True,
        )
        source_identity, source_value = dataset.columns

        page = self.client.get(f"/workspaces/{workspace_id}/mapping")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Check a known total (optional)", page.text)
        self.assertIn("Allowed difference", page.text)
        self.assertNotIn("control_totals_json", page.text)

        saved = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            data={
                "csrf_token": self.csrf,
                "action": "save_progress",
                "expected_parent_version": "",
                "expected_working_draft_version": "",
                "target_model_0": "res.partner",
                "mode_0": "upsert",
                "source_identity_0": source_identity.stable_key,
                "business_key_0": business_key.key_id,
                "identity_source_0_0": source_identity.stable_key,
                "scalar_value_source_0_1": "source",
                "scalar_source_0_1": source_value.stable_key,
                "scalar_type_0_1": "decimal",
                "scalar_compare_0_1": "1",
                "scalar_null_0_1": "distinct",
                "control_name_0_0": "Opening balance",
                "control_target_0_0": "field_0000",
                "control_expected_0_0": "1234.50",
                "control_unit_0_0": "EUR",
                "control_tolerance_0_0": "0.01",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(saved.status_code, 303)
        working = (
            self.app.state.context.mapping_workspace.mappings.get_mapping_working_draft(
                workspace_id
            )
        )
        control = working.definition.datasets[0].effective_control_totals[0]
        self.assertEqual(control.name, "Opening balance")
        self.assertEqual(control.target_field, "field_0000")
        self.assertEqual(control.expected_total, "1234.50")
        self.assertEqual(control.unit, "EUR")
        self.assertEqual(control.tolerance, "0.01")

    def test_active_preparation_blocks_mapping_autosave_before_database_read(
        self,
    ) -> None:
        workspace_id, _dataset, _business_key = self._mapping_ready_workspace(
            scalar_field_count=1,
        )
        context = self.app.state.context
        manager = context.preparation_jobs
        assert manager is not None
        active = SimpleNamespace(job_id=str(uuid4()))

        with patch.object(manager, "active", return_value=active):
            mapping_page = self.client.get(
                f"/workspaces/{workspace_id}/mapping",
                follow_redirects=False,
            )
            autosave = self.client.post(
                f"/workspaces/{workspace_id}/mapping/save",
                json={"entries": []},
                headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
            )

        progress_url = f"/workspaces/{workspace_id}/preparation/{active.job_id}"
        self.assertEqual(mapping_page.status_code, 303)
        self.assertEqual(mapping_page.headers["location"], progress_url)
        self.assertEqual(autosave.status_code, 409)
        self.assertEqual(autosave.json()["redirect_url"], progress_url)
        self.assertIn("Wait for it to finish", autosave.json()["detail"])

    def test_data_manager_can_save_a_guided_business_data_check(self) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=2,
        )
        source_identity, source_value = dataset.columns
        context = self.app.state.context
        _revision, validation = (
            context.mapping_workspace.check_definition(
                workspace_id,
                datasets=(
                    DatasetMapping(
                        dataset_id=dataset.dataset_id,
                        target_model="res.partner",
                        mode=MappingTargetMode.UPSERT,
                        source_identity_column_keys=(
                            source_identity.stable_key,
                        ),
                        target_identity=(
                            IdentityComponentMapping(
                                source_column_keys=(
                                    source_identity.stable_key,
                                ),
                                target_fields=business_key.key_fields,
                            ),
                        ),
                        fields=(
                            ScalarFieldMapping(
                                target_field="field_0000",
                                source_column_key=source_value.stable_key,
                                value_source=ScalarValueSource.SOURCE,
                            ),
                            ScalarFieldMapping(
                                target_field="field_0001",
                                source_column_key=source_value.stable_key,
                                value_source=ScalarValueSource.SOURCE,
                            ),
                        ),
                    ),
                ),
                expected_parent_version=None,
                expected_working_draft_version=None,
                actor=context.actor,
            )
        )
        self.assertNotEqual(
            validation.status,
            MappingValidationStatus.INVALID,
        )

        page = self.client.get(f"/workspaces/{workspace_id}/mapping")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Data checks", page.text)
        self.assertIn("Recommended checks are already on", page.text)
        self.assertIn("Add business check 1", page.text)
        self.assertNotIn("ruleset_json", page.text)

        saved = self.client.post(
            f"/workspaces/{workspace_id}/mapping/quality",
            data={
                "csrf_token": self.csrf,
                "quality_dataset_id": dataset.dataset_id,
                "quality_name_0": "Opening before closing",
                "quality_family_0": "ORDERED_COMPARISON",
                "quality_field_a_0": "field_0000",
                "quality_field_b_0": "field_0001",
                "quality_equals_0": "",
                "quality_outcome_0": "QUARANTINE",
                "quality_owner_0": "FUNCTIONAL_OWNER",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(saved.status_code, 303)
        ruleset = context.quality.quality.get_current_quality_ruleset(workspace_id)
        self.assertIsNotNone(ruleset)
        self.assertEqual(len(ruleset.manager_rules), 1)
        rule = ruleset.manager_rules[0]
        self.assertEqual(rule.name, "Opening before closing")
        self.assertEqual(rule.family, QualityRuleFamily.ORDERED_COMPARISON)
        self.assertEqual(rule.outcome, QualityOutcomePolicy.QUARANTINE)
        self.assertEqual(rule.owner_role, QualityOwnerRole.FUNCTIONAL_OWNER)
        restored = self.client.get(saved.headers["location"])
        self.assertIn("Opening before closing", restored.text)
        self.assertIn("Functional owner", restored.text)

    def test_failed_named_total_has_plain_review_ui_and_blocks_package(self) -> None:
        workspace_id, _dataset, _business_key = self._mapping_ready_workspace(
            scalar_field_count=0,
        )
        context = self.app.state.context
        total = CanonicalControlTotal(
            control_id="sha256:" + "d" * 64,
            name="Opening balance",
            dataset="contacts",
            target_field="credit_limit",
            expected_total="1000",
            actual_total="900",
            tolerance="0",
            unit="EUR",
            included_rows=12,
            empty_rows=0,
        )
        staging = MagicMock(
            total_rows=12,
            mapping_version=3,
            run_id="f0cd6d32-80d9-4e31-9bcb-d316d83cf0b8",
            content_hash="sha256:" + "7" * 64,
            datasets=(),
            control_totals=(total,),
            failed_control_total_count=1,
            control_totals_passed=False,
        )
        report = MagicMock(
            status="READY",
            blocked_count=0,
            needs_review_count=0,
            ready_count=0,
            total_count=0,
            datasets=(),
            rows=(),
            checked_at=datetime.now(timezone.utc),
            run_id=str(uuid4()),
        )

        with (
            patch.object(
                context.preflight,
                "current_staging",
                return_value=staging,
            ),
            patch.object(
                context.preflight,
                "current_report",
                return_value=report,
            ),
        ):
            page = self.client.get(f"/workspaces/{workspace_id}/summary")
            package = self.client.post(
                f"/workspaces/{workspace_id}/summary/package",
                data={"csrf_token": self.csrf},
                headers=POST_HEADERS,
            )

        self.assertEqual(page.status_code, 200)
        self.assertIn("Some totals need attention", page.text)
        self.assertIn("Opening balance", page.text)
        self.assertIn("Difference -100 EUR", page.text)
        self.assertIn("<summary>Support details</summary>", page.text)
        self.assertNotIn("Save these rules as a Recipe", page.text)
        self.assertEqual(package.status_code, 422)
        self.assertIn("Resolve the named totals", package.text)

    def test_transformation_impact_uses_server_filters_and_100_row_pages(
        self,
    ) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=1,
        )
        source_identity, source_value = dataset.columns
        context = self.app.state.context
        revision, validation = (
            context.mapping_workspace.check_definition(
                workspace_id,
                datasets=(
                    DatasetMapping(
                        dataset_id=dataset.dataset_id,
                        target_model="res.partner",
                        mode=MappingTargetMode.UPSERT,
                        source_identity_column_keys=(
                            source_identity.stable_key,
                        ),
                        target_identity=(
                            IdentityComponentMapping(
                                source_column_keys=(
                                    source_identity.stable_key,
                                ),
                                target_fields=business_key.key_fields,
                            ),
                        ),
                        fields=(
                            ScalarFieldMapping(
                                target_field="field_0000",
                                source_column_key=source_value.stable_key,
                                value_source=ScalarValueSource.SOURCE,
                            ),
                        ),
                    ),
                ),
                expected_parent_version=None,
                expected_working_draft_version=None,
                actor=context.actor,
            )
        )
        self.assertNotEqual(validation.status, MappingValidationStatus.INVALID)
        impact_rows = tuple(
            TransformationImpactRow(
                dataset=dataset.name,
                source_row=index + 2,
                source_column=source_value.source_name,
                target_field="field_0000",
                raw_value=f" raw {index} ",
                proposed_value=f"Raw {index}",
                rules="Trim",
                outcome="invalid" if index % 2 else "changed",
                message="Needs review" if index % 2 else "",
            )
            for index in range(205)
        )

        def fake_stage(*_args, **kwargs):
            sink = kwargs["transformation_impact_sink"]
            for row in impact_rows:
                sink(row)
            return MagicMock(
                transformation_impact=TransformationImpactReport(
                    mapping_content_hash=revision.definition.content_hash,
                    evaluated_count=205,
                    changed_count=103,
                    fallback_count=0,
                    null_count=0,
                    invalid_count=102,
                    provided_count=0,
                    unchanged_count=0,
                    rows=(),
                    detail_limit=0,
                )
            )

        impact_url = f"/workspaces/{workspace_id}/mapping/transformation-impact"
        first_visit = self.client.get(impact_url)
        self.assertIn("Prepare the comparison", first_visit.text)
        self.assertIn("data-transformation-impact-prepare", first_visit.text)
        self.assertIn("data-transformation-impact-status", first_visit.text)
        self.assertIn('aria-live="polite"', first_visit.text)
        impact_script = self.client.get("/static/transformation-impact.js")
        self.assertIn("[data-transformation-impact-prepare]", impact_script.text)
        self.assertIn("Preparing the comparison…", impact_script.text)
        with patch(
            "impodo.application.workspace.mapping.transformation_impact.stage_browser_mapping",
            side_effect=fake_stage,
        ) as staged:
            prepared = self.client.post(
                f"{impact_url}/prepare",
                data={"csrf_token": self.csrf},
                headers=POST_HEADERS,
                follow_redirects=False,
            )
        self.assertEqual(prepared.status_code, 303)
        staged.assert_called_once()

        first_page = self.client.get(impact_url)
        self.assertEqual(first_page.text.count('class="impact-row'), 100)
        self.assertIn(
            "Contains 1 space before the value and 1 space after the value.",
            first_page.text,
        )
        self.assertIn(
            "Removed 1 space before the value and 1 space after the value.",
            first_page.text,
        )
        self.assertIn("Showing 1–100 of 205", first_page.text)
        self.assertIn("Next 100", first_page.text)
        next_match = re.search(
            r'href="([^"]+after=[^"]+)"[^>]*>Next 100</a>',
            first_page.text,
        )
        self.assertIsNotNone(next_match)
        second_page = self.client.get(unescape(next_match.group(1)))
        self.assertEqual(second_page.text.count('class="impact-row'), 100)
        self.assertIn("Showing 101–200 of 205", second_page.text)
        self.assertIn("Previous 100", second_page.text)

        invalid_page = self.client.get(f"{impact_url}?outcome=invalid")
        self.assertEqual(invalid_page.text.count('class="impact-row'), 100)
        self.assertIn("Showing 1–100 of 102", invalid_page.text)
        invalid_csv = self.client.post(
            f"{impact_url}.csv",
            data={"csrf_token": self.csrf, "outcome": "invalid"},
            headers=POST_HEADERS,
        )
        self.assertEqual(invalid_csv.status_code, 200)
        self.assertEqual(len(invalid_csv.text.splitlines()), 103)

    def test_schema_governance_keeps_duplicate_fields_out_of_one_rule(
        self,
    ) -> None:
        workspace_id, _dataset, original_key = self._mapping_ready_workspace(
            scalar_field_count=1,
        )
        context = self.app.state.context
        original_governance = (
            context.schema_workspace.schemas.get_schema_governance(workspace_id)
        )
        self.assertIsNotNone(original_governance)

        duplicate_simple = self.client.post(
            f"/workspaces/{workspace_id}/schema/govern",
            data={
                "csrf_token": self.csrf,
                "primary_key_field_0": "ref",
                "primary_scope_field_0": "ref",
                "key_fields_0": "ref",
                "scope_fields_0": "ref",
                "key_description_0": "Reference within reference",
            },
            headers=POST_HEADERS,
        )

        self.assertEqual(duplicate_simple.status_code, 422)
        self.assertIn(
            "Review the highlighted matching rule, then confirm it again.",
            duplicate_simple.text,
        )
        self.assertIn(
            (
                "For res.partner, choose each field only once. The matching fields "
                "and Within fields must be different."
            ),
            duplicate_simple.text,
        )
        self.assertRegex(
            duplicate_simple.text,
            (
                r'(?s)name="primary_key_field_0".*?'
                r'<option\s+value="ref"\s+selected'
            ),
        )
        self.assertRegex(
            duplicate_simple.text,
            (
                r'(?s)name="primary_scope_field_0".*?'
                r'<option\s+value="ref"\s+selected'
            ),
        )
        unchanged_governance = (
            context.schema_workspace.schemas.get_schema_governance(workspace_id)
        )
        self.assertIsNotNone(unchanged_governance)
        self.assertEqual(
            unchanged_governance.content_hash,
            original_governance.content_hash,
        )
        self.assertEqual(unchanged_governance.business_keys, (original_key,))

        duplicate_combined = self.client.post(
            f"/workspaces/{workspace_id}/schema/govern",
            data={
                "csrf_token": self.csrf,
                "primary_key_field_0": "",
                "primary_scope_field_0": "",
                "key_fields_0": "ref, ref",
                "scope_fields_0": "",
                "key_description_0": "Repeated combined reference",
            },
            headers=POST_HEADERS,
        )

        self.assertEqual(duplicate_combined.status_code, 422)
        self.assertIn('value="ref, ref"', duplicate_combined.text)
        self.assertIn("Repeated combined reference", duplicate_combined.text)

        valid = self.client.post(
            f"/workspaces/{workspace_id}/schema/govern",
            data={
                "csrf_token": self.csrf,
                "primary_key_field_0": "field_0000",
                "primary_scope_field_0": "ref",
                "key_fields_0": "field_0000",
                "scope_fields_0": "ref",
                "key_description_0": "Field within reference",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(valid.status_code, 303)
        saved_governance = (
            context.schema_workspace.schemas.get_schema_governance(workspace_id)
        )
        self.assertIsNotNone(saved_governance)
        self.assertEqual(
            saved_governance.business_keys[0].key_fields,
            ("field_0000",),
        )
        self.assertEqual(
            saved_governance.business_keys[0].scope_fields,
            ("ref",),
        )

        schema_script = self.client.get("/static/schema.js")
        self.assertIn("updateKeyFieldConflicts", schema_script.text)
        self.assertIn(
            "Matching fields and Within fields must be different.",
            schema_script.text,
        )

    def test_preparation_worker_and_progress_page_do_not_open_locked_databases(
        self,
    ) -> None:
        workspace_id, _dataset, _business_key = self._mapping_ready_workspace(
            scalar_field_count=1,
        )
        context = self.app.state.context
        workspace_state = context.queries.get(workspace_id)
        selection = context.queries.get_source_selection(workspace_id)
        assert selection is not None
        migration_workspace = context.migration_workspaces.get(
            workspace_id,
            actor=context.actor,
        )
        workspace = PreparationWorkspace.from_context(
            migration_workspace,
            context.data_versions.get(
                migration_workspace.data_version_id,
                actor=context.actor,
            ),
            context.migration_runs.get(
                migration_workspace.migration_run_id,
                actor=context.actor,
            ),
        )
        self.assertEqual(workspace.migration_run_purpose.value, "AUTHORING")
        manager = context.preparation_jobs
        assert manager is not None
        root = Path(self.temporary.name)
        registry_path = root / "registry.duckdb"
        project_path = (
            context.workspace_states.repository.workspace_directory(workspace_id)
            / "workspace-engine.duckdb"
        )

        with _spawned_duckdb_locks(registry_path):
            job = manager.enqueue(
                workspace_id,
                workspace_state.name,
                sum(item.row_count for item in selection.datasets),
                actor=context.actor,
                workspace=workspace,
            )
            progress_url = (
                f"/workspaces/{workspace_id}/preparation/{job.job_id}"
            )
            completed = _wait_for_preparation(
                self.client,
                progress_url,
                timeout=30,
            )

        self.assertEqual(completed["status"], PreparationJobStatus.FAILED.value)
        self.assertEqual(completed["failure_code"], "ReadinessError", completed)
        self.assertIn("Submit the mapping", str(completed["failure_message"]))
        worker_deadline = time.monotonic() + 2.0
        while manager.worker_alive(job.job_id) and time.monotonic() < worker_deadline:
            time.sleep(0.01)
        self.assertFalse(manager.worker_alive(job.job_id))

        with _spawned_duckdb_locks(registry_path, project_path):
            progress_page = self.client.get(progress_url)

        self.assertEqual(progress_page.status_code, 200, progress_page.text)
        self.assertIn("Stage 4 of 6", progress_page.text)
        self.assertIn("Preparation progress", progress_page.text)
        self.assertIn(
            f'href="/projects/{workspace.project_id}"',
            progress_page.text,
        )
        self.assertIn('aria-current="page"', progress_page.text)
