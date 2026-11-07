"""Keep incoming relationship dependencies through comparison and loading."""

import unittest
from pathlib import PurePath
from types import SimpleNamespace
from uuid import uuid4

from impodo.application.workspace.execution.service import execution_api_scope
from impodo.domain.compiler.contracts import CompiledMigrationPlan
from impodo.domain.execution.models import ExecutionRunStatus
from impodo.domain.execution_snapshot import ExecutionSnapshot, build_execution_snapshot
from impodo.domain.odoo.contracts import (
    MetadataSnapshot,
    RecordSnapshot,
    bind_snapshot_hashes,
)
from impodo.domain.preparation.preflight import PreflightEngine
from impodo.domain.preparation.source import (
    SourceRow,
    SourceTable,
    prepare_source_tables,
)
from impodo.domain.recipe.profile import (
    DatasetSpec,
    IdentityComponent,
    RelationSpec,
    ResolveSpec,
    SourceIdentitySpec,
    SourceSpec,
    TargetIdentitySpec,
    TargetSpec,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.shared.models import (
    FieldMetadata,
    LogicalReference,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
)
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.application.workspace.execution import test_service as support


def _comparison_evidence(version, mode, model, kind, hybrid, scoped):
    scope = (
        (IdentityComponent(source_fields=("scope",), target_fields=("scope_code",)),)
        if scoped
        else ()
    )
    terms = DatasetSpec(
        name="terms",
        source=SourceSpec(file="terms.csv"),
        target=TargetSpec(model=model, mode="upsert"),
        source_identity=SourceIdentitySpec(fields=("term_code",)),
        target_identity=TargetIdentitySpec(
            components=(
                IdentityComponent(
                    source_fields=("term_name",), target_fields=("name",)
                ),
            ),
            scope=scope,
        ),
    )
    resolve = ResolveSpec(
        dataset="terms",
        target_source_fields=("term_code",),
        target_model=model if hybrid else None,
        target_fields=("name",) if hybrid else (),
    )
    field = "property_payment_term_id" if kind == "many2one" else "x_term_ids"
    customers = DatasetSpec(
        name="customers",
        source=SourceSpec(file="customers.csv"),
        target=TargetSpec(model="res.partner", mode="upsert"),
        source_identity=SourceIdentitySpec(fields=("ref",)),
        target_identity=TargetIdentitySpec(
            components=(
                IdentityComponent(source_fields=("ref",), target_fields=("ref",)),
            ),
        ),
        relations={
            field: RelationSpec(
                kind=kind, source_fields=("term_code",), resolve=resolve
            )
        },
    )
    plan = CompiledMigrationPlan(
        plan_id="incoming_updates",
        origin="profile_document",
        origin_hash=support.HASH,
        datasets=(terms, customers),
    )
    prepared = prepare_source_tables(
        plan,
        (
            SourceTable(
                "terms",
                PurePath("terms.csv"),
                ("term_code", "term_name", "scope"),
                (
                    SourceRow(
                        2,
                        {"term_code": "30jjnt", "term_name": "30JJNT", "scope": "SITE"},
                    ),
                ),
                support.HASH,
            ),
            SourceTable(
                "customers",
                PurePath("customers.csv"),
                ("ref", "term_code", "term_name"),
                (
                    SourceRow(
                        2,
                        {
                            "ref": "EXISTING",
                            "term_code": "30jjnt",
                            "term_name": "30JJNT",
                        },
                    ),
                    SourceRow(
                        3, {"ref": "NEW", "term_code": "30jjnt", "term_name": "30JJNT"}
                    ),
                ),
                support.HASH,
            ),
        ),
        source_hashes={"terms": support.HASH, "customers": support.HASH},
    )
    fingerprint = TargetFingerprint(
        target_hash=support.TARGET_HASH,
        connection_mode=mode.value,
        database="disposable",
        odoo_version=version,
        module_versions={"base": version},
        snapshot_timestamp="2026-11-06T08:00:00Z",
    )
    metadata = MetadataSnapshot(
        fingerprint,
        {
            model: ModelMetadata(
                model,
                "Terms",
                {
                    "name": FieldMetadata("name", "char", required=True),
                    "scope_code": FieldMetadata("scope_code", "char"),
                },
            ),
            "res.partner": ModelMetadata(
                "res.partner",
                "Customers",
                {
                    "ref": FieldMetadata("ref", "char"),
                    field: FieldMetadata(field, kind, relation=model),
                },
            ),
        },
    )
    target = RecordSnapshot(
        fingerprint,
        records={
            model: (),
            "res.partner": (
                TargetRecord("res.partner", 50, {"ref": "EXISTING", field: False}),
            ),
        },
        requested_fields={model: ("name", "scope_code"), "res.partner": ("ref", field)},
    )
    metadata, target = bind_snapshot_hashes(metadata, target)
    result = PreflightEngine().run(plan, prepared, metadata, target)
    frozen = SimpleNamespace(
        workspace_id=str(uuid4()),
        prepared=prepared,
        plan=plan,
        revision=SimpleNamespace(
            mapping_id=str(uuid4()),
            version=1,
            definition=SimpleNamespace(content_hash=support.HASH),
        ),
        staging=SimpleNamespace(run_id=str(uuid4()), content_hash=support.HASH),
        quality=SimpleNamespace(run_id=str(uuid4()), content_hash=support.HASH),
        normalization=SimpleNamespace(
            run_id=str(uuid4()),
            content_hash=support.HASH,
            lifecycle_version=1,
            eligible_dataset_hash=support.HASH,
        ),
        content_hash=support.HASH,
    )
    return frozen, result, field


def _comparison(version, mode, model, kind, hybrid, scoped):
    frozen, result, field = _comparison_evidence(version, mode, model, kind, hybrid, scoped)
    snapshot = build_execution_snapshot(
        preflight_run_id=str(uuid4()), frozen=frozen, result=result
    )
    return ExecutionSnapshot.from_json(snapshot.to_json()), field


class IncomingUpdateExecutionTests(unittest.TestCase):
    def test_comparison_keeps_incoming_terms_for_existing_and_new_customers(self):
        shapes = (
            ("account.payment.term", "many2one", False, False),
            ("account.payment.term", "many2one", True, False),
            ("x.shipping.term", "many2many", False, True),
            ("x.shipping.term", "many2many", True, True),
        )
        for version in ("19.0", "20.0"):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                for model, kind, hybrid, scoped in shapes:
                    with self.subTest(
                        version=version,
                        mode=mode,
                        model=model,
                        kind=kind,
                        hybrid=hybrid,
                    ):
                        snapshot, field = _comparison(
                            version, mode, model, kind, hybrid, scoped
                        )
                        self.assertEqual(snapshot.counts["CREATE"], 2)
                        self.assertEqual(snapshot.counts["UPDATE"], 1)
                        term = next(
                            row for row in snapshot.rows if row.dataset == "terms"
                        )
                        for owner in (
                            row for row in snapshot.rows if row.dataset == "customers"
                        ):
                            intent = next(
                                item for item in owner.fields if item.field == field
                            )
                            values = (
                                intent.value
                                if isinstance(intent.value, tuple)
                                else (intent.value,)
                            )
                            self.assertEqual(
                                values,
                                (
                                    LogicalReference(
                                        "incoming", ("30jjnt",), dataset="terms"
                                    ),
                                ),
                            )
                            self.assertEqual(intent.dependency_row_ids, (term.row_id,))
                            self.assertLess(
                                term.schedule_ordinal, owner.schedule_ordinal
                            )

                        service, journal = support.ExecutionServiceTests()._service(
                            snapshot, mode=mode
                        )
                        executor = support._Executor(
                            execution_api_scope(snapshot).semantic_hash,
                            lookup_results={(model, (("name", "=", "30JJNT"),)): ()},
                        )
                        run = service.execute(
                            snapshot.workspace_id,
                            expected_snapshot_hash=snapshot.semantic_hash,
                            executor=executor,
                            actor=LOCAL_ACTOR,
                        )

                        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED)
                        self.assertEqual(journal.run.committed_count, 3)
                        self.assertFalse(
                            any(
                                lookup_model == model
                                for lookup_model, _ in executor.lookups
                            )
                        )
                        model_name, identifier, values = executor.updates[-1]
                        self.assertEqual((model_name, identifier), ("res.partner", 50))
                        self.assertEqual(
                            values[field], 10 if kind == "many2one" else [[6, 0, [10]]]
                        )
                        creates = (
                            executor.loads
                            if mode is OdooConnectionMode.REMOTE
                            else executor.creates
                        )
                        self.assertEqual(creates[0][0], model)


if __name__ == "__main__":
    unittest.main()
