"""Capture the documented multi-column hierarchy decisions from an isolated app.

Run this helper from the repository root with Playwright available. It creates
only fictional product data, serves the current authenticated application on
an ephemeral loopback port, and writes four 1440 by 1024 PNG files under
``docs/images/user``.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sys
from uuid import uuid4

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from impodo.application.data_version.inspection import (
    CATALOG_CONTRACT_VERSION,
    SourceColumnProfile,
    SourceFileCatalog,
    SourceTableCatalog,
)
from impodo.application.workspace.mapping.field_catalog import (
    MappingFieldCatalogSnapshot,
)
from impodo.domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
    SchemaGovernance,
)
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.workspace.contracts import (
    SchemaField,
    SchemaModel,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
    canonical_mapping_source_selection,
)
from impodo.domain.workspace.derived_entities import mapping_source_selection
from scripts.capture_match_data_recovery_screenshots import (
    VIEWPORT,
    _capture,
    _start_server,
    _stop_server,
)
from tests.support.browser_scenarios import (
    ProjectSetupBrowserTestCase,
    TargetRecord,
    _browser_model_catalog,
)


DEFAULT_OUTPUT_DIRECTORY = REPOSITORY_ROOT / "docs" / "images" / "user"


class _SourceEvidenceOverride:
    """Override one fictional workspace while delegating every other read."""

    def __init__(self, delegate, workspace_id, selection, catalogs) -> None:
        self._delegate = delegate
        self._workspace_id = workspace_id
        self._selection = selection
        self._catalogs = catalogs

    def get_source_selection(self, workspace_id):
        if workspace_id == self._workspace_id:
            return self._selection
        return self._delegate.get_source_selection(workspace_id)

    def get_source_catalogs(self, workspace_id):
        if workspace_id == self._workspace_id:
            return self._catalogs
        return self._delegate.get_source_catalogs(workspace_id)

    def __getattr__(self, name):
        return getattr(self._delegate, name)


class _MappingSourceOverride:
    """Project the fictional source and its current saved hierarchy plan."""

    def __init__(
        self,
        delegate,
        workspace_id,
        selection,
        catalogs,
        plan_reader,
    ) -> None:
        self._delegate = delegate
        self._workspace_id = workspace_id
        self._selection = selection
        self._catalogs = catalogs
        self._plan_reader = plan_reader

    def get_mapping_source_selection(self, workspace_id):
        if workspace_id != self._workspace_id:
            return self._delegate.get_mapping_source_selection(workspace_id)
        plan = self._plan_reader.get_derived_entity_plan(workspace_id)
        if plan is None:
            return self._selection
        return mapping_source_selection(self._selection, plan, self._catalogs)


class _DerivedPlanOverride:
    """Keep one isolated screenshot plan outside the fixture's frozen package."""

    def __init__(self, delegate, workspace_id) -> None:
        self._delegate = delegate
        self._workspace_id = workspace_id
        self._plan = None

    def get_derived_entity_plan(self, workspace_id):
        if workspace_id == self._workspace_id:
            return self._plan
        return self._delegate.get_derived_entity_plan(workspace_id)

    def save_derived_entity_plan(
        self,
        workspace_id,
        plan,
        *,
        expected_parent_version,
        actor,
    ) -> None:
        if workspace_id != self._workspace_id:
            self._delegate.save_derived_entity_plan(
                workspace_id,
                plan,
                expected_parent_version=expected_parent_version,
                actor=actor,
            )
            return
        current_version = self._plan.version if self._plan is not None else None
        if expected_parent_version != current_version:
            raise RuntimeError("The isolated hierarchy plan changed unexpectedly.")
        self._plan = plan


class _MappingFieldCatalogOverride:
    """Keep lazy field catalogues on the same fictional evidence."""

    def __init__(
        self,
        delegate,
        workspace_id,
        selection,
        catalogs,
        queries,
    ) -> None:
        self._delegate = delegate
        self._workspace_id = workspace_id
        self._selection = selection
        self._catalogs = catalogs
        self._queries = queries

    def get_mapping_field_catalog_snapshot(self, workspace_id):
        if workspace_id != self._workspace_id:
            return self._delegate.get_mapping_field_catalog_snapshot(workspace_id)
        revision = self._queries.get_mapping_revision(workspace_id)
        return MappingFieldCatalogSnapshot(
            physical_selection=self._selection,
            preparation_plan=self._queries.get_derived_entity_plan(workspace_id),
            source_catalogs=self._catalogs,
            schema=self._queries.get_odoo_schema_catalog(workspace_id),
            governance=self._queries.get_schema_governance(workspace_id),
            revision=revision,
            working_draft=self._queries.get_mapping_working_draft(workspace_id),
        )


def _column_profile(
    ordinal: int,
    name: str,
    values: tuple[str | None, ...],
) -> SourceColumnProfile:
    populated = tuple(value for value in values if value is not None)
    lengths = tuple(len(value) for value in populated)
    return SourceColumnProfile(
        ordinal=ordinal,
        name=name,
        candidate_type="string",
        null_count=len(values) - len(populated),
        non_null_count=len(populated),
        distinct_count=len(set(populated)),
        distinct_count_is_exact=True,
        duplicate_count=len(populated) - len(set(populated)),
        minimum=min(populated) if populated else None,
        maximum=max(populated) if populated else None,
        minimum_length=min(lengths) if lengths else None,
        maximum_length=max(lengths) if lengths else None,
    )


def _fictional_source(base_selection: SourceSelection):
    rows = (
        ("PR-001", "Finished goods", "Model 100"),
        ("PR-002", None, "Model 200"),
        ("PR-003", "Components", None),
        ("PR-004", None, None),
    )
    names = ("Product reference", "Product family", "Model code")
    stable_keys = (
        "column:product_reference",
        "column:product_family",
        "column:model_code",
    )
    now = datetime.now(timezone.utc)
    file_id = "source:fictional-products"
    source_hash = "sha256:" + "8" * 64
    table_key = "products"
    catalog = SourceFileCatalog(
        contract_version=CATALOG_CONTRACT_VERSION,
        file_id=file_id,
        display_name="fictional-products.csv",
        source_sha256=source_hash,
        source_size_bytes=256,
        format="CSV",
        inspected_at=now,
        encoding="utf-8",
        delimiter=",",
        tables=(
            SourceTableCatalog(
                table_key=table_key,
                name="products",
                kind="CSV",
                hidden=False,
                header_row=1,
                row_count=len(rows),
                column_count=len(names),
                columns=tuple(
                    _column_profile(
                        ordinal,
                        name,
                        tuple(row[ordinal - 1] for row in rows),
                    )
                    for ordinal, name in enumerate(names, start=1)
                ),
                preview_rows=rows,
            ),
        ),
    )
    dataset = SourceDataset(
        dataset_id=base_selection.datasets[0].dataset_id,
        name="products",
        source=FileSourceBinding(
            file_id=file_id,
            table_key=table_key,
            source_sha256=source_hash,
            catalog_hash=catalog.content_hash,
            encoding="utf-8",
            delimiter=",",
            header_row=1,
        ),
        row_count=len(rows),
        columns=tuple(
            SourceDatasetColumn(
                ordinal=ordinal,
                source_name=name,
                stable_key=stable_key,
                candidate_type="string",
            )
            for ordinal, (name, stable_key) in enumerate(
                zip(names, stable_keys, strict=True),
                start=1,
            )
        ),
    )
    selection = canonical_mapping_source_selection(
        replace(
            base_selection,
            selection_id=str(uuid4()),
            version=base_selection.version + 1,
            created_at=now,
            datasets=(dataset,),
        )
    )
    return selection, (catalog,), dataset


def _configure_product_schema(fixture, workspace_id: str) -> None:
    context = fixture.app.state.context
    actor = context.actor
    workspace_state = context.queries.get(workspace_id)
    existing_schema = context.queries.get_odoo_schema_catalog(workspace_id)
    if existing_schema is None:
        raise RuntimeError("The isolated hierarchy workspace has no Odoo schema.")

    model_snapshot = _browser_model_catalog(workspace_state)
    category_record = TargetRecord(
        model="ir.model",
        odoo_id=40,
        values={
            "name": "Product Category",
            "model": "product.category",
            "abstract": False,
            "transient": False,
            "modules": "product, stock",
            "state": "base",
        },
    )
    model_snapshot = replace(
        model_snapshot,
        records={
            "ir.model": (*model_snapshot.records["ir.model"], category_record),
        },
    )
    context.schema_workspace.discover_models(
        workspace_id,
        model_snapshot,
        read_credential_binding_hash=(
            existing_schema.read_credential_binding_hash
        ),
        actor=actor,
    )

    product_category = SchemaModel(
        name="product.category",
        label="Product Category",
        fields=(
            SchemaField(
                name="name",
                label="Name",
                type="char",
                required=True,
                readonly=False,
                relation=None,
                relation_field=None,
                selection=(),
            ),
            SchemaField(
                name="parent_id",
                label="Parent Category",
                type="many2one",
                required=False,
                readonly=False,
                relation="product.category",
                relation_field=None,
                selection=(),
            ),
        ),
    )
    product_template = SchemaModel(
        name="product.template",
        label="Product",
        fields=(
            SchemaField(
                name="default_code",
                label="Internal Reference",
                type="char",
                required=True,
                readonly=False,
                relation=None,
                relation_field=None,
                selection=(),
            ),
            SchemaField(
                name="categ_id",
                label="Product Category",
                type="many2one",
                required=True,
                readonly=False,
                relation="product.category",
                relation_field=None,
                selection=(),
            ),
        ),
    )
    schema = replace(
        existing_schema,
        captured_at=datetime.now(timezone.utc),
        models=(product_category, product_template),
        content_hash="sha256:" + "7" * 64,
    )
    context.schema_workspace.schemas.save_odoo_schema_catalog(
        workspace_id,
        schema,
        actor=actor,
    )
    context.schema_workspace.schemas.save_schema_governance(
        workspace_id,
        SchemaGovernance(
            governance_id=str(uuid4()),
            version=1,
            workspace_id=workspace_id,
            catalog_hash=schema.content_hash,
            permitted_models=("product.category", "product.template"),
            business_keys=(
                BusinessKeyDefinition(
                    key_id="product.category:name-parent",
                    model="product.category",
                    key_fields=("name",),
                    scope_fields=("parent_id",),
                    description="Name within Parent Category",
                    status=BusinessKeyStatus.CONFIRMED,
                ),
                BusinessKeyDefinition(
                    key_id="product.template:default-code",
                    model="product.template",
                    key_fields=("default_code",),
                    description="Internal Reference",
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
            recorded_at=datetime.now(timezone.utc),
            recorded_by=actor.identity.display_name,
        ),
        actor=actor,
    )


def _install_fictional_evidence(
    fixture,
    workspace_id: str,
    selection: SourceSelection,
    catalogs: tuple[SourceFileCatalog, ...],
) -> None:
    context = fixture.app.state.context
    source_override = _SourceEvidenceOverride(
        context.queries._sources,
        workspace_id,
        selection,
        catalogs,
    )
    context.queries._sources = source_override
    context.derived_entities.sources = source_override
    plan_override = _DerivedPlanOverride(
        context.queries._derived_entities,
        workspace_id,
    )
    context.queries._derived_entities = plan_override
    context.derived_entities.derived_entities = plan_override
    context.queries._mapping_sources = _MappingSourceOverride(
        context.queries._mapping_sources,
        workspace_id,
        selection,
        catalogs,
        plan_override,
    )
    context.queries._mapping_field_catalogs = _MappingFieldCatalogOverride(
        context.queries._mapping_field_catalogs,
        workspace_id,
        selection,
        catalogs,
        context.queries,
    )


def _show_decision(page, locator, *, offset: int = 120) -> None:
    locator.scroll_into_view_if_needed()
    page.evaluate(f"window.scrollBy(0, -{offset})")


def capture(output_directory: Path, *, browser_channel: str) -> None:
    try:
        from playwright.sync_api import expect, sync_playwright
    except ModuleNotFoundError as error:  # pragma: no cover - operator guidance
        raise RuntimeError(
            "Playwright is required. Run with `uv run --with playwright`."
        ) from error

    output_directory.mkdir(parents=True, exist_ok=True)
    fixture = ProjectSetupBrowserTestCase(methodName="runTest")
    fixture.setUp()
    server = None
    thread = None
    try:
        workspace_id, _dataset, _key = fixture._mapping_ready_workspace(
            scalar_field_count=1,
            target_model="product.template",
        )
        base_selection = fixture.app.state.context.sources.sources.get_source_selection(
            workspace_id
        )
        if base_selection is None:
            raise RuntimeError("The isolated hierarchy source was not created.")
        selection, catalogs, dataset = _fictional_source(base_selection)
        _install_fictional_evidence(
            fixture,
            workspace_id,
            selection,
            catalogs,
        )
        _configure_product_schema(fixture, workspace_id)

        session_cookie = fixture.client.cookies.get("impodo_session")
        if not session_cookie:
            raise RuntimeError("The isolated setup did not create an Impodo session.")
        fixture.client.close()
        server, thread, port = _start_server(fixture.app)
        base_url = f"http://127.0.0.1:{port}"

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel=browser_channel,
                headless=True,
            )
            browser_context = browser.new_context(
                viewport=VIEWPORT,
                device_scale_factor=1,
                locale="en-GB",
            )
            browser_context.add_cookies(
                [
                    {
                        "name": "impodo_session",
                        "value": session_cookie,
                        "url": base_url,
                        "httpOnly": True,
                        "sameSite": "Strict",
                    }
                ]
            )
            page = browser_context.new_page()
            page.goto(
                f"{base_url}/workspaces/{workspace_id}/derived-entities",
                wait_until="networkidle",
            )
            page.locator('a[href="#hierarchy-extraction"]').click()
            hierarchy = page.locator("#hierarchy-extraction")
            expect(hierarchy).to_have_attribute("open", "")
            hierarchy.locator('select[name="hierarchy_level_1"]').select_option(
                dataset.columns[1].stable_key
            )
            hierarchy.locator('select[name="hierarchy_level_2"]').select_option(
                dataset.columns[2].stable_key
            )
            hierarchy.locator('input[name="output_dataset_name"]').fill(
                "product_categories"
            )
            hierarchy.locator('input[name="target_model"]').fill(
                "product.category"
            )
            hierarchy.locator('select[name="missing_parent_mode"]').select_option(
                "fixed"
            )
            hierarchy.locator('input[name="missing_parent_value"]').fill("Default")
            hierarchy.locator('select[name="missing_leaf"]').select_option(
                "use_deepest"
            )
            hierarchy.locator('select[name="all_blank_mode"]').select_option(
                "emit_null_reference"
            )
            _show_decision(page, hierarchy, offset=80)
            _capture(page, output_directory / "06a-hierarchy-setup.png")

            hierarchy.get_by_role(
                "button",
                name="Preview hierarchy records",
            ).click()
            preview = page.locator("#hierarchy-preview")
            expect(preview).to_be_visible()
            _show_decision(page, preview, offset=80)
            _capture(page, output_directory / "06b-hierarchy-preview.png")

            preview.get_by_role(
                "button",
                name="Create this hierarchy table",
            ).click()
            page.wait_for_load_state("networkidle")

            mapping_url = f"{base_url}/workspaces/{workspace_id}/mapping"
            plan = fixture.app.state.context.queries.get_derived_entity_plan(
                workspace_id
            )
            if plan is None:
                raise RuntimeError(
                    "The fictional hierarchy plan was not saved. "
                    f"URL={page.url!r}; body={page.locator('body').inner_text()[:2_000]!r}"
                )
            effective_selection = mapping_source_selection(
                selection,
                plan,
                catalogs,
            )
            generated_index = next(
                index
                for index, item in enumerate(effective_selection.datasets)
                if item.name == "product_categories"
            )
            consumer_index = next(
                index
                for index, item in enumerate(effective_selection.datasets)
                if item.name == "products"
            )
            generated_dataset_id = effective_selection.datasets[
                generated_index
            ].dataset_id
            page.goto(
                (
                    f"{mapping_url}?mapping_dataset={generated_index}"
                    f"&target_model_{consumer_index}=product.template"
                ),
                wait_until="networkidle",
            )
            identity = page.locator(
                f"#mapping-dataset-{generated_index} .identity-pair"
            )
            expect(identity).to_be_visible()
            expect(
                identity.locator(
                    f'select[name="identity_dataset_{generated_index}_1"]'
                )
            ).to_have_value(generated_dataset_id)
            expect(
                identity.locator(
                    f'select[name="identity_dataset_{generated_index}_1"]'
                )
            ).to_contain_text("product_categories (this generated table)")
            identity.scroll_into_view_if_needed()
            page.evaluate("window.scrollBy(0, 220)")
            _capture(page, output_directory / "10b-hierarchy-parent-mapping.png")

            page.goto(
                (
                    f"{mapping_url}?mapping_dataset={consumer_index}"
                    f"&target_model_{consumer_index}=product.template"
                ),
                wait_until="networkidle",
            )
            category = page.locator(
                '[data-relation-mapping-row][data-target-field="categ_id"]'
            )
            category.evaluate(
                """element => {
                  let current = element;
                  while (current) {
                    if (current instanceof HTMLDetailsElement) current.open = true;
                    current = current.parentElement;
                  }
                }"""
            )
            expect(category).to_be_visible(timeout=10_000)
            expect(
                category.locator(
                    f'select[name="relation_dataset_{consumer_index}_0"]'
                )
            ).to_contain_text("product_categories")
            _show_decision(page, category, offset=120)
            _capture(page, output_directory / "12a-hierarchy-product-link.png")

            browser_context.close()
            browser.close()
    finally:
        if server is not None and thread is not None:
            _stop_server(server, thread)
        fixture.tearDown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=DEFAULT_OUTPUT_DIRECTORY,
    )
    parser.add_argument(
        "--browser-channel",
        default="msedge",
        help="Installed Chromium channel for Playwright. Defaults to msedge.",
    )
    arguments = parser.parse_args()
    capture(
        arguments.output_directory.resolve(),
        browser_channel=arguments.browser_channel,
    )


if __name__ == "__main__":
    main()
