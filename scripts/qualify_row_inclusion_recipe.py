"""Qualify row-rule Recipe reuse and scoped Contact writes on remote Odoo 20.

Run with ``python -m scripts.qualify_row_inclusion_recipe`` from the checkout.
The runner publishes a Recipe in disposable local storage, applies it to fresh
fictional data, confirms the new population, and writes only two included
Contacts. It verifies exclusion and removes only this run's unique references.
The receipt excludes credentials, source values, company names, and Odoo IDs.
This adapter canary does not qualify the execution journal or a live transfer.
"""

import argparse
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from html import unescape
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from fastapi.testclient import TestClient

from impodo.adapters.odoo.connectors import Json2ReadConnector
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    RowInclusionCondition,
    RowInclusionMode,
    RowInclusionPolicy,
    ScalarFieldMapping,
    SelectionConditionOperator,
)
from impodo.domain.mapping.validation.evidence import mapping_issue_fingerprint
from impodo.domain.odoo.contracts import ConnectorError, RecordRequest
from impodo.domain.preparation.staging_contracts import StagingDisposition
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.shared.models import canonical_json_bytes
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.app import create_local_app
from impodo.web.composition.odoo_company import read_company_access
from impodo.web.composition.target_readers import _target_json2_config
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from tests.support.browser_scenarios import ProjectWorkspaceBuilder
from tests.support.identity import resolve_local_actor


def _require(condition, message):
    if not condition:
        raise RuntimeError(message)


def _field(response, name):
    match = re.search(r'name="' + re.escape(name) + r'" value="([^"]*)"', response.text)
    _require(match is not None, f"The local form is missing {name}")
    return unescape(match.group(1))


def _post(client, path, data, *, files=None):
    response = client.post(
        path,
        data=data,
        files=files,
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    _require(
        response.status_code == 303,
        f"The local qualification action returned HTTP {response.status_code}",
    )


def _configure_target(context, workspace, args, key, actor):
    workspace = context.workspace_states.update_target(
        workspace.workspace_id,
        actor=actor,
        expected_revision=workspace.revision,
        odoo_connection_mode="REMOTE",
        odoo_base_url=args.base_url.rstrip("/"),
        odoo_database=args.database,
        intended_applications=(),
        intended_models=("res.partner",),
    )
    credential = store_target_credential(
        context.secret_store,
        workspace,
        TargetCredentialRole.READ,
        key,
        persistent=False,
    )
    return workspace, credential


def _capture_target(context, workspace, credential, key, actor, *, company=None):
    if company is None:
        access = read_company_access(workspace, key)
        choices = tuple(
            item for item in access.choices if item.name == access.default_company_name
        )
        _require(len(choices) == 1, "The API user's current company is ambiguous")
        company = choices[0]
    current = context.queries.get(workspace.workspace_id)
    workspace = context.workspace_states.select_odoo_company(
        workspace.workspace_id,
        actor=actor,
        expected_revision=current.revision,
        selection=company,
    )
    identity = context.read_identity_probe(workspace, key, ("res.partner",))
    metadata = context.schema_reader(workspace, key)
    schema = context.schema_workspace.capture(
        workspace.workspace_id,
        metadata,
        read_credential_binding_hash=credential.binding_hash,
        read_identity=identity,
        actor=actor,
    )
    _require(schema.odoo_version in {"20.0", "20.0+e"}, "Use a final Odoo 20 database")
    return workspace, schema, company, metadata.fingerprint.module_versions


def _confirm_and_submit(context, workspace_id, actor, expected_counts):
    mappings = context.mapping_workspace.mappings
    revision = mappings.get_mapping_revision(workspace_id)
    working = mappings.get_mapping_working_draft(workspace_id)
    validation = mappings.get_mapping_validation(workspace_id, revision.version)
    _require(
        validation.status.value != "INVALID", "The live target mapping remains invalid"
    )
    review, _ = context.row_inclusion_reviews.current(workspace_id, actor=actor)
    if review is None:
        review = context.row_inclusion_reviews.check_current(workspace_id, actor=actor)
    _require(
        (review.source_row_count, review.included_count, review.excluded_count)
        == expected_counts,
        "The checked source population does not match this qualification",
    )
    _require(review.cannot_evaluate_count == 0, "A source value could not be checked")
    context.row_inclusion_reviews.confirm_current(
        workspace_id,
        datasets=revision.definition.datasets,
        expected_revision_version=revision.version,
        expected_working_draft_version=working.version,
        actor=actor,
    )
    context.mapping_workspace.submit_current(
        workspace_id,
        datasets=revision.definition.datasets,
        expected_version=revision.version,
        expected_working_draft_version=working.version,
        actor=actor,
        warning_acknowledgements=tuple(
            mapping_issue_fingerprint(item)
            for item in validation.issues
            if item.severity == "warning"
        ),
    )


def _accept_fresh_data(client, url, content):
    page = client.get(url)
    _require(page.status_code == 200, "The fresh-data form is unavailable")
    _post(
        client,
        url + "/files",
        {
            "csrf_token": _field(page, "csrf_token"),
            "revision": _field(page, "revision"),
        },
        files={"source_file": ("contacts.csv", content, "text/csv")},
    )
    page = client.get(url)
    _post(
        client,
        url + "/register",
        {
            "csrf_token": _field(page, "csrf_token"),
            "revision": _field(page, "revision"),
        },
    )
    page = client.get(url)
    _post(
        client,
        url + "/accept",
        {
            "csrf_token": _field(page, "csrf_token"),
            "parameter_revision": "",
            "warnings_acknowledged": "1",
        },
    )


def _canary(workspace, key, canonical, marker):
    rows = tuple(
        item
        for item in canonical.rows
        if item.disposition is StagingDisposition.CANDIDATE
    )
    _require(
        len(rows) == 2 and all(len(item.target_identity) == 1 for item in rows),
        "Expected exactly two prepared Contacts",
    )
    _require(
        all(set(item.proposed_values) == {"name"} for item in rows),
        "The canary fields changed",
    )
    values = tuple(
        {**dict(item.proposed_values), "ref": item.target_identity[0]} for item in rows
    )
    references = (marker + "-F0", marker + "-F1", marker + "-F2")
    _require(
        {item["ref"] for item in values} == set(references[1:]),
        "An excluded Contact entered the canary",
    )
    config = _target_json2_config(workspace, key)
    config = replace(
        config,
        retries=0,
        context={
            **config.context,
            "mail_create_nosubscribe": True,
            "mail_notrack": True,
            "tracking_disable": True,
        },
    )
    reader = Json2ReadConnector(config)
    scope = OdooApiScope(
        preview_hash=canonical.content_hash,
        models=(
            OdooModelScope(
                "res.partner",
                write_fields=("name", "ref"),
                read_fields=("name", "ref"),
                lookup_fields=("ref",),
            ),
        ),
    )
    writer = Json2WriteExecutor(config, scope)
    _require(
        all(
            not writer.find_ids("res.partner", (("ref", "=", ref),))
            for ref in references
        ),
        "The unique qualification namespace is already present; no write was made",
    )
    expected = {item["ref"]: item["name"] for item in values}
    try:
        # Never retry this create blindly. Cleanup resolves only this exact namespace.
        identifiers = writer.create_rows("res.partner", values)
        _require(len(identifiers) == 2, "Odoo did not confirm two creates")
        readback = reader.get_records(
            (
                RecordRequest(
                    "res.partner",
                    ("name", "ref"),
                    domain=(("ref", "in", references),),
                    limit=4,
                ),
            )
        )
        actual = {
            item.values["ref"]: item.values["name"]
            for item in readback.records["res.partner"]
        }
        _require(actual == expected, "Live read-back or excluded-row absence failed")
    finally:
        own_ids = tuple(
            identifier
            for ref in references[1:]
            for identifier in writer.find_ids("res.partner", (("ref", "=", ref),))
        )
        if own_ids:
            status, result = writer.transport(
                config.base_url + "/json/2/res.partner/unlink",
                {
                    "Authorization": "bearer " + key,
                    "Content-Type": "application/json",
                    "X-Odoo-Database": config.database,
                },
                canonical_json_bytes(
                    {"ids": list(own_ids), "context": dict(config.context)}
                ),
                config.timeout_seconds,
                "POST",
            )
            _require(status == 200 and result is True, "Synthetic cleanup failed")
        _require(
            all(
                not writer.find_ids("res.partner", (("ref", "=", ref),))
                for ref in references
            ),
            "Cleanup absence verification failed",
        )
    print(
        "PASS: two included Contacts created and verified; excluded Contact absent; synthetic records removed.",
        flush=True,
    )


def qualify(args):
    _require(args.allow_write, "Use the explicit --allow-write flag for this canary")
    key = args.api_key_file.read_text(encoding="utf-8").strip()
    _require(
        key and not any(character.isspace() for character in key),
        "The private file must contain only the API key",
    )
    marker = "impodo-row-rule-" + uuid4().hex[:12]
    with TemporaryDirectory(prefix="impodo-row-recipe-") as root:
        token = uuid4().hex
        app = create_local_app(
            Path(root), secret_store=MemorySecretStore(), launch_token=token
        )
        with TestClient(app) as client:
            client.get("/launch?token=" + token, follow_redirects=False)
            context, actor = app.state.context, resolve_local_actor(app.state.context)
            workspace = ProjectWorkspaceBuilder(context).create(
                name="Row rule qualification", source_system="Fictional CSV"
            )
            content = f"Reference,Name,Status\n{marker}-A,Fictional included contact,KEEP\n{marker}-B,Fictional excluded contact,DROP\n".encode()
            source = context.intake.accept(
                workspace.workspace_id,
                actor=actor,
                expected_revision=workspace.revision,
                display_name="contacts.csv",
                stream=BytesIO(content),
            )
            workspace, credential = _configure_target(
                context, context.queries.get(workspace.workspace_id), args, key, actor
            )
            workspace = context.workspace_states.register(
                workspace.workspace_id,
                actor=actor,
                expected_revision=workspace.revision,
            )
            catalog = context.inspections.inspect_project(
                workspace.workspace_id, actor=actor
            )[0]
            table_key = catalog.tables[0].table_key
            context.sources.confirm_source(
                workspace.workspace_id,
                source.file_id,
                selected_table_keys=(table_key,),
                warnings_acknowledged=True,
                actor=actor,
            )
            context.data_version_source_projection.finalize_file_selection(
                workspace.workspace_id,
                dataset_names={(source.file_id, table_key): "contacts"},
                actor=actor,
            )
            selection = context.queries.get_source_selection(workspace.workspace_id)
            workspace, schema, company, modules = _capture_target(
                context, workspace, credential, key, actor
            )
            context.schema_workspace.govern(
                workspace.workspace_id,
                business_keys=(
                    BusinessKeyDefinition(
                        key_id="contacts-ref",
                        model="res.partner",
                        key_fields=("ref",),
                        status=BusinessKeyStatus.CONFIRMED,
                    ),
                ),
                actor=actor,
            )
            dataset = selection.datasets[0]
            keys = {item.source_name: item.stable_key for item in dataset.columns}
            mapping = DatasetMapping(
                dataset_id=dataset.dataset_id,
                target_model="res.partner",
                source_identity_column_keys=(keys["Reference"],),
                target_identity=(
                    IdentityComponentMapping(
                        source_column_keys=(keys["Reference"],), target_fields=("ref",)
                    ),
                ),
                fields=(
                    ScalarFieldMapping(
                        target_field="name", source_column_key=keys["Name"]
                    ),
                ),
                row_inclusion=RowInclusionPolicy(
                    mode=RowInclusionMode.MATCHING_ROWS,
                    conditions=(
                        RowInclusionCondition(
                            condition_id=str(uuid4()),
                            source_column_key=keys["Status"],
                            operator=SelectionConditionOperator.EQUALS,
                            comparison_value="KEEP",
                        ),
                    ),
                ),
            )
            revision, validation = context.mapping_workspace.check_definition(
                workspace.workspace_id,
                datasets=(mapping,),
                expected_parent_version=None,
                expected_working_draft_version=None,
                actor=actor,
            )
            if validation.status.value == "INVALID":
                working = context.mapping_workspace.mappings.get_mapping_working_draft(
                    workspace.workspace_id
                )
                working, _ = context.mapping_workspace.confirm_available_odoo_defaults(
                    workspace.workspace_id,
                    expected_version=working.version,
                    actor=actor,
                )
                context.mapping_workspace.check_definition(
                    workspace.workspace_id,
                    datasets=working.definition.datasets,
                    expected_parent_version=revision.version,
                    expected_working_draft_version=working.version,
                    actor=actor,
                )
            _confirm_and_submit(context, workspace.workspace_id, actor, (2, 1, 1))
            context.preparation.prepare(workspace.workspace_id, actor=actor)
            owner = context.migration_workspaces.get(
                workspace.workspace_id, actor=actor
            )
            publication = context.recipe_publication.publish(
                project_id=owner.project_id,
                data_version_id=owner.data_version_id,
                workspace_id=workspace.workspace_id,
                display_name="Row rule qualification",
                business_purpose="Verify excluded Contacts stay out of fresh-data loads",
                actor=actor,
            )
            print(
                "PASS: live Odoo 20 authoring and real Recipe publication.", flush=True
            )
            setup = context.test_runs.start_setup(
                owner.project_id,
                expected_workspace_revision=context.migration_projects.get(
                    owner.project_id, actor=actor
                ).optimistic_revision,
                recipe_revisions=(
                    (publication.recipe.recipe_id, publication.revision.version),
                ),
                dependencies=(),
                label="Fresh row-rule qualification",
                export_as_of=datetime.now(UTC).date().isoformat(),
                operation_id=str(uuid4()),
                actor=actor,
            )
            url = f"/projects/{owner.project_id}/test-runs/{setup.run.migration_run_id}/fresh-data"
            fresh_content = f"Reference,Name,Status\n{marker}-F0,Fictional excluded fresh contact,DROP\n{marker}-F1,Fictional first fresh contact,KEEP\n{marker}-F2,Fictional second fresh contact,KEEP\n".encode()
            _accept_fresh_data(client, url, fresh_content)
            fresh, credential = _configure_target(
                context,
                context.queries.get(setup.setup_workspace.workspace_id),
                args,
                key,
                actor,
            )
            fresh, fresh_schema, _, _ = _capture_target(
                context, fresh, credential, key, actor, company=company
            )
            activated = context.test_runs.activate(
                owner.project_id,
                setup.run.migration_run_id,
                expected_workspace_revision=context.migration_projects.get(
                    owner.project_id, actor=actor
                ).optimistic_revision,
                target_schema=fresh_schema,
                target_reference_bundle=None,
                credential_generation=fresh_schema.read_credential_binding_hash,
                operation_id=str(uuid4()),
                actor=actor,
            )
            application = activated.applications[0]
            issues = context.run_planning.repository.list_issues(
                application.application_id
            )
            _require(
                any(
                    item.code == "MAPPING_ROW_INCLUSION_CONFIRMATION_REQUIRED"
                    for item in issues
                ),
                "Fresh row counts did not require confirmation",
            )
            _confirm_and_submit(context, application.workspace_id, actor, (3, 2, 1))
            prepared = context.preparation.prepare(
                application.workspace_id, actor=actor
            )
            _require(
                prepared.eligible_record_count == 2,
                "Fresh preparation did not retain exactly two Contacts",
            )
            summary = context.preparation.staging.get_current_staging_summary(
                application.workspace_id
            )
            canonical = context.preparation.staging.get_canonical_staging_run(
                application.workspace_id,
                summary.run_id,
                expected_content_hash=summary.content_hash,
            )
            _canary(fresh, key, canonical, marker)
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "application_build_id": PROCESS_BUILD_CONTRACT.application_build_id,
        "odoo_version": schema.odoo_version,
        "module_versions": dict(modules),
        "authoring_rows": {"source": 2, "included": 1, "excluded": 1},
        "fresh_rows": {"source": 3, "included": 2, "excluded": 1},
        "recipe_publication": True,
        "fresh_confirmation_required": True,
        "scoped_canary_created": 2,
        "excluded_records_created": 0,
        "readback_verified": 2,
        "cleanup_verified": True,
        "scope": "Live schema, Recipe publication and reuse, canonical preparation, scoped JSON-2 canary writes and read-back. The execution journal and transfer between two live servers are separate qualifications.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-write", action="store_true", required=True)
    args = parser.parse_args()
    try:
        report = qualify(args)
    except (OSError, RuntimeError, ValueError, ConnectorError, WorkspaceError) as error:
        print(
            "Qualification failed: "
            + type(error).__name__
            + ". No successful receipt was written."
        )
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Sanitized receipt written to " + str(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
