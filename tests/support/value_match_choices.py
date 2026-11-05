"""Real extracted payment-term tables with fictional Odoo evidence."""

from dataclasses import replace
from asyncio import get_running_loop

from impodo.adapters.duckdb.unit_of_work import _read_databases

from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    IdentityComponentMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
    ValueMapping,
)
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.shared.models import TargetRecord
from impodo.domain.workspace.derived_entities import derived_dataset_links
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.mapping_journey import MappingJourneyFixture


class ValueMatchFixture(MappingJourneyFixture):
    """Keep source, derivation, mapping, and saved aliases on their real paths."""

    codes = tuple(f"TERM{index:02d}" for index in range(1, 19))
    related_model = "account.payment.term"

    def freeze_files(self, _files):
        files = {
            "products": (
                "Reference,Product title,Category\n"
                + "\n".join(
                    f"P{index},Vendor {index},{code}"
                    for index, code in enumerate(self.codes, 1)
                )
                + "\n"
            ).encode(),
            "spare_parts": (
                "Reference,Product title,Category\n"
                + "\n".join(
                    f"S{index},Term {index},{code}"
                    for index, code in enumerate(self.codes, 1)
                )
                + "\n"
            ).encode(),
        }
        return super().freeze_files(files)

    def configure_matches(self, major, mode):
        workspace_id, physical, schema = self.configure(major, mode)
        models = dict(self.metadata.models)
        category = models.pop("x.category")
        models[self.related_model] = replace(
            category, model=self.related_model, description="Payment Terms"
        )
        item = models["x.item"]
        models["x.item"] = replace(
            item,
            fields={
                **item.fields,
                "category_id": replace(
                    item.fields["category_id"],
                    relation=self.related_model,
                    label="Vendor Payment Terms",
                ),
            },
        )
        self.metadata = replace(self.metadata, models=models)
        state = self.context.queries.get(workspace_id)
        self.context.workspace_states.update_target(
            workspace_id,
            actor=self.actor,
            expected_revision=state.revision,
            odoo_connection_mode=mode.value,
            odoo_base_url=state.odoo_base_url,
            odoo_database=state.odoo_database,
            intended_applications=(),
            intended_models=tuple(models),
        )
        schema = self.context.schema_workspace.capture(
            workspace_id,
            self.metadata,
            actor=self.actor,
            read_credential_binding_hash=schema.read_credential_binding_hash,
            read_identity=self.read_identity(
                self.context.queries.get(workspace_id),
                "fictional-read-key",
                tuple(models),
            )
            if mode.value == "REMOTE"
            else None,
        )
        self.context.schema_workspace.govern(
            workspace_id,
            actor=self.actor,
            business_keys=(
                BusinessKeyDefinition(
                    "item:reference",
                    "x.item",
                    ("ref",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
                BusinessKeyDefinition(
                    "term:name",
                    self.related_model,
                    ("name",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
        )
        products = next(item for item in physical.datasets if item.name == "products")
        source = next(item for item in physical.datasets if item.name == "spare_parts")
        term_column = next(
            item.stable_key for item in source.columns if item.source_name == "Category"
        )
        plan, _ = run_local_operation_sync(
            self.context.derived_entities.save_rule,
            workspace_id,
            output_dataset_name="payment_terms",
            source_dataset_id=source.dataset_id,
            source_column_key=term_column,
            target_model=self.related_model,
            target_name_field="name",
            external_id_namespace="payment_terms",
            parent_separator=None,
            blank_policy="block",
            expected_parent_version=None,
            actor=self.actor,
        )
        self.link = derived_dataset_links(plan)[0]
        self.selection = self.context.queries.get_mapping_source_selection(workspace_id)
        self.parent = next(
            item
            for item in self.selection.datasets
            if item.dataset_id == self.link.derived_dataset_id
        )
        self.owner = products
        columns = {item.source_name: item.stable_key for item in products.columns}
        self.source_key = columns["Category"]
        self.mappings = (
            DatasetMapping(
                dataset_id=self.parent.dataset_id,
                target_model=self.related_model,
                source_identity_column_keys=(self.link.canonical_key_column_key,),
                target_identity=(
                    IdentityComponentMapping((self.link.name_column_key,), ("name",)),
                ),
            ),
            DatasetMapping(
                dataset_id=products.dataset_id,
                target_model="x.item",
                source_identity_column_keys=(columns["Reference"],),
                target_identity=(
                    IdentityComponentMapping((columns["Reference"],), ("ref",)),
                ),
                fields=(
                    ScalarFieldMapping(
                        target_field="name", source_column_key=columns["Product title"]
                    ),
                ),
                relationships=(
                    RelationshipMapping(
                        target_field="category_id",
                        kind="many2one",
                        source_column_keys=(self.source_key,),
                        categorical_policy=CategoricalCoveragePolicy.EXPLICIT_KEY_MATCH,
                        resolver=RelationshipResolver(
                            ResolverOrigin.TARGET_THEN_DATASET,
                            dataset_id=self.parent.dataset_id,
                            model=self.related_model,
                            key_mappings=(
                                ReferenceKeyMapping(self.source_key, "name"),
                            ),
                            value_mappings=tuple(
                                ValueMapping(code, code) for code in self.codes[:11]
                            ),
                        ),
                    ),
                ),
            ),
        )
        self.save_mappings()
        return workspace_id

    def save_mappings(self):
        draft = self.context.queries.get_mapping_working_draft(self.workspace_id)
        return run_local_operation_sync(
            self.context.mapping_workspace.save_working_draft,
            self.workspace_id,
            datasets=self.mappings,
            actor=self.actor,
            expected_version=draft.version if draft else None,
        )

    def snapshots(self, state, metadata_requests, record_requests):
        self.assertIsNone(getattr(_read_databases, "connections", None))
        with self.assertRaises(RuntimeError):
            get_running_loop()
        metadata, records = super().snapshots(state, metadata_requests, record_requests)
        if any(request.model == self.related_model for request in record_requests):
            records = replace(
                records,
                records={
                    **records.records,
                    self.related_model: tuple(
                        TargetRecord(self.related_model, index + 100, {"name": code})
                        for index, code in enumerate(self.codes[:11])
                    ),
                },
            )
        return metadata, records

    def choices_data(self):
        return {
            "csrf_token": self.csrf,
            "kind": "relationship",
            "dataset_id": self.owner.dataset_id,
            "source_column_key": self.source_key,
            "target_model": "x.item",
            "target_field": "category_id",
            "business_key_id": "term:name",
            "resolver_origin": "target_then_dataset",
            "incoming_dataset_id": self.parent.dataset_id,
        }
