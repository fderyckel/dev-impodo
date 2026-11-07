"""Independent source and target cases for nested reference regressions."""

from __future__ import annotations

from pathlib import PurePath
from types import SimpleNamespace
from uuid import uuid4

from impodo.domain.compiler.contracts import CompiledMigrationPlan
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
    FieldSpec,
    IdentityComponent,
    RelationSpec,
    ResolveSpec,
    SourceIdentitySpec,
    SourceSpec,
    TargetIdentitySpec,
    TargetSpec,
)
from impodo.domain.shared.models import (
    BusinessReference,
    FieldMetadata,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
)

HASH = "sha256:" + "1" * 64
TARGET_HASH = "sha256:" + "2" * 64


def _frozen(plan, prepared):
    return SimpleNamespace(
        workspace_id=str(uuid4()),
        prepared=prepared,
        plan=plan,
        revision=SimpleNamespace(
            mapping_id=str(uuid4()),
            version=1,
            definition=SimpleNamespace(content_hash=HASH),
        ),
        staging=SimpleNamespace(run_id=str(uuid4()), content_hash=HASH),
        quality=SimpleNamespace(run_id=str(uuid4()), content_hash=HASH),
        normalization=SimpleNamespace(
            run_id=str(uuid4()),
            content_hash=HASH,
            lifecycle_version=1,
            eligible_dataset_hash=HASH,
        ),
        content_hash=HASH,
    )


def _table(name, rows):
    return SourceTable(
        name,
        PurePath(name + ".csv"),
        tuple(rows[0]),
        tuple(SourceRow(index, values) for index, values in enumerate(rows, 2)),
        HASH,
    )


def compared_reference_case(
    major=19, mode="LOCAL", *, reference_parent=False, schedule=False, company_matches=1
):
    """Prepare real rows and compare against an independent three-model target."""
    company = ResolveSpec(target_model="res.company", target_fields=("name",))
    term = DatasetSpec(
        name="terms",
        source=SourceSpec(file="terms.csv"),
        target=TargetSpec(
            model="account.payment.term",
            mode="reference" if reference_parent else "upsert",
        ),
        source_identity=SourceIdentitySpec(fields=("term_key",)),
        target_identity=TargetIdentitySpec(
            components=(
                IdentityComponent(
                    source_fields=("term_name",), target_fields=("name",)
                ),
            ),
            scope=(
                IdentityComponent(
                    source_fields=("company_name",),
                    target_fields=("company_id",),
                    resolve=company,
                ),
            ),
        ),
    )
    parent = ResolveSpec(dataset="terms", target_source_fields=("term_key",))
    if schedule:
        owner = DatasetSpec(
            name="lines",
            source=SourceSpec(file="lines.csv"),
            target=TargetSpec(model="account.payment.term.line", mode="upsert"),
            source_identity=SourceIdentitySpec(fields=("line_key",)),
            target_identity=TargetIdentitySpec(
                components=(
                    IdentityComponent(
                        source_fields=("term_key",),
                        target_fields=("payment_id",),
                        resolve=parent,
                    ),
                )
            ),
            fields={"nb_days": FieldSpec(source="days", type="integer")},
        )
        owner_rows = ({"line_key": "L1", "term_key": "T1", "days": "45"},)
        owner_records = (
            TargetRecord(
                "account.payment.term.line", 200, {"payment_id": 10, "nb_days": 0}
            ),
        )
    else:
        owner = DatasetSpec(
            name="contacts",
            source=SourceSpec(file="contacts.csv"),
            target=TargetSpec(model="res.partner", mode="upsert"),
            source_identity=SourceIdentitySpec(fields=("ref",)),
            target_identity=TargetIdentitySpec(
                components=(
                    IdentityComponent(
                        source_fields=("ref",),
                        target_fields=("ref",),
                    ),
                )
            ),
            fields={
                "name": FieldSpec(source="name", type="string", required_on_create=True)
            },
            relations={
                "property_payment_term_id": RelationSpec(
                    kind="many2one", source_fields=("term_key",), resolve=parent
                )
            },
        )
        owner_rows = (
            {"ref": "EXISTING", "name": "Existing contact", "term_key": "T1"},
        )
        if not reference_parent:
            owner_rows += ({"ref": "NEW", "name": "New contact", "term_key": "T1"},)
        owner_records = (
            TargetRecord(
                "res.partner",
                50,
                {
                    "ref": "EXISTING",
                    "name": "Existing contact",
                    "property_payment_term_id": False,
                },
            ),
        )
    plan = CompiledMigrationPlan(
        plan_id="nested-reference-regression",
        origin="profile_document",
        origin_hash=HASH,
        datasets=(term, owner),
    )
    prepared = prepare_source_tables(
        plan,
        (
            _table(
                "terms",
                (
                    {
                        "term_key": "T1",
                        "term_name": "Immediate",
                        "company_name": "North",
                    },
                ),
            ),
            _table(owner.name, owner_rows),
        ),
        source_hashes={"terms.csv": HASH, owner.name + ".csv": HASH},
    )
    fingerprint = TargetFingerprint(
        target_hash=TARGET_HASH,
        connection_mode=mode,
        database="disposable",
        odoo_version=f"{major}.0",
        module_versions={"base": f"{major}.0"},
        snapshot_timestamp="2026-11-06T00:00:00Z",
    )
    models = {
        "res.company": ModelMetadata(
            "res.company", "Company", {"name": FieldMetadata("name", "char")}
        ),
        "account.payment.term": ModelMetadata(
            "account.payment.term",
            "Term",
            {
                "name": FieldMetadata("name", "char"),
                "company_id": FieldMetadata(
                    "company_id", "many2one", relation="res.company"
                ),
            },
        ),
        "res.partner": ModelMetadata(
            "res.partner",
            "Contact",
            {
                "ref": FieldMetadata("ref", "char"),
                "name": FieldMetadata("name", "char"),
                "property_payment_term_id": FieldMetadata(
                    "property_payment_term_id",
                    "many2one",
                    relation="account.payment.term",
                ),
            },
        ),
        "account.payment.term.line": ModelMetadata(
            "account.payment.term.line",
            "Line",
            {
                "payment_id": FieldMetadata(
                    "payment_id", "many2one", relation="account.payment.term"
                ),
                "nb_days": FieldMetadata("nb_days", "integer"),
            },
        ),
    }
    existing_term = (
        (
            TargetRecord(
                "account.payment.term", 10, {"name": "Immediate", "company_id": 9}
            ),
        )
        if reference_parent
        else ()
    )
    records = {
        "res.company": tuple(
            TargetRecord("res.company", 9 + index, {"name": "North"})
            for index in range(company_matches)
        ),
        "account.payment.term": existing_term,
        owner.target.model: owner_records,
    }
    requested = {model: tuple(models[model].fields) for model in records}
    metadata, target = bind_snapshot_hashes(
        MetadataSnapshot(fingerprint, {model: models[model] for model in records}),
        RecordSnapshot(fingerprint, records, requested),
    )
    result = PreflightEngine().run(plan, prepared, metadata, target)
    return _frozen(plan, prepared), result


def legacy_reference(value):
    if isinstance(value, BusinessReference):
        return BusinessReference(
            value.model,
            tuple(legacy_reference(item) for item in value.key),
            tuple(legacy_reference(item) for item in value.scope),
        )
    if isinstance(value, tuple):
        return tuple(legacy_reference(item) for item in value)
    return value


def saved_reference_snapshot(frozen, result):
    snapshot = build_execution_snapshot(
        preflight_run_id=str(uuid4()), frozen=frozen, result=result
    )
    return ExecutionSnapshot.from_json(snapshot.to_json())
