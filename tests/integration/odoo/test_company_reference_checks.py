"""Qualify grouped company checks and generic preflight with shared and parent links."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path, PurePath

from impodo.adapters.odoo.company_checks import capture_company_checks
from impodo.adapters.odoo.connectors import (
    SnapshotConnector,
    write_metadata_snapshot,
    write_record_snapshot,
)
from impodo.domain.compiler.contracts import CompiledMigrationPlan
from impodo.domain.odoo.contracts import (
    MetadataRequest,
    MetadataSnapshot,
    RecordRequest,
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
    Classification,
    FieldMetadata,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
)

HASH = "sha256:" + "1" * 64
DOMAIN = "['|', ('company_id', '=', False), ('company_id', 'parent_of', [allowed_company_ids[0]])] + []"


def snapshots():
    fingerprint = TargetFingerprint(
        HASH, "REMOTE", "fictional", "20.0", "2026-10-01T00:00:00Z"
    )
    metadata = MetadataSnapshot(
        fingerprint,
        {
            "x.route": ModelMetadata(
                "x.route",
                None,
                {
                    "code": FieldMetadata("code", "char", label="Code"),
                    "x_zone_id": FieldMetadata(
                        "x_zone_id",
                        "many2one",
                        label="Zone",
                        relation="x.zone",
                        company_dependent=True,
                    ),
                },
            ),
            "x.zone": ModelMetadata(
                "x.zone", None, {"code": FieldMetadata("code", "char", label="Code")}
            ),
        },
    )
    records = RecordSnapshot(
        fingerprint,
        {
            "x.route": (),
            "x.zone": tuple(
                TargetRecord("x.zone", identifier, {"code": code})
                for identifier, code in enumerate(
                    ("SHARED", "PARENT", "SELECTED", "FOREIGN"), 1
                )
            ),
        },
        requested_fields={"x.route": ("code", "x_zone_id"), "x.zone": ("code",)},
    )
    return metadata, records


class CompanyReferenceTests(unittest.TestCase):
    def test_shared_parent_and_optional_blank_reach_preflight_but_foreign_company_blocks(
        self,
    ):
        metadata, records = snapshots()
        calls = []

        def post(model, method, payload):
            calls.append((model, method, payload))
            self.assertEqual(
                payload["context"], {"allowed_company_ids": [105], "active_test": False}
            )
            self.assertEqual(
                payload["domain"][1:],
                ["|", ["company_id", "=", False], ["company_id", "parent_of", [105]]],
            )
            return [{"id": value} for value in (1, 2, 3)]

        metadata = capture_company_checks(
            metadata, records, {("x.route", "x_zone_id"): DOMAIN}, 105, post
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(metadata.company_checks[0].incompatible_ids, (4,))
        plan = CompiledMigrationPlan(
            plan_id="company_rules",
            origin="profile_document",
            origin_hash=HASH,
            datasets=(
                DatasetSpec(
                    name="routes",
                    source=SourceSpec(file="routes.csv"),
                    target=TargetSpec(model="x.route", mode="upsert"),
                    source_identity=SourceIdentitySpec(fields=("code",)),
                    target_identity=TargetIdentitySpec(
                        components=(
                            IdentityComponent(
                                source_fields=("code",), target_fields=("code",)
                            ),
                        )
                    ),
                    fields={"code": FieldSpec(source="code", type="string")},
                    relations={
                        "x_zone_id": RelationSpec(
                            kind="many2one",
                            source_fields=("zone",),
                            resolve=ResolveSpec(
                                target_model="x.zone", target_fields=("code",)
                            ),
                        )
                    },
                ),
            ),
        )
        table = SourceTable(
            "routes",
            PurePath("routes.csv"),
            ("code", "zone"),
            tuple(
                SourceRow(number=index + 2, values={"code": f"R{index}", "zone": value})
                for index, value in enumerate(
                    ("SHARED", "PARENT", "SELECTED", "FOREIGN", None)
                )
            ),
            HASH,
        )
        prepared = prepare_source_tables(plan, (table,), source_hashes={"routes": HASH})
        metadata, records = bind_snapshot_hashes(metadata, records)
        result = PreflightEngine().run(plan, prepared, metadata, records)
        self.assertEqual(
            [item.classification for item in result.decisions],
            [
                Classification.CREATE,
                Classification.CREATE,
                Classification.CREATE,
                Classification.BLOCKED,
                Classification.CREATE,
            ],
        )
        self.assertIn(
            "REFERENCE_COMPANY_MISMATCH",
            {issue.code for issue in result.decisions[3].issues},
        )

    def test_compatibility_evidence_survives_snapshot_replay(self):
        metadata, records = snapshots()
        checked = capture_company_checks(
            metadata,
            records,
            {("x.route", "x_zone_id"): DOMAIN},
            105,
            lambda *_: [{"id": 1}, {"id": 2}, {"id": 3}],
        )
        with tempfile.TemporaryDirectory() as directory:
            metadata_path, records_path = (
                Path(directory) / "metadata.json",
                Path(directory) / "records.json",
            )
            write_metadata_snapshot(checked, metadata_path, profile_id="company_rules")
            write_record_snapshot(
                records, records_path, profile_id="company_rules", source_hashes={}
            )
            replay = SnapshotConnector(
                metadata_path=metadata_path, records_path=records_path
            )
            restored = replay.get_model_metadata(
                tuple(
                    MetadataRequest(model, tuple(value.fields))
                    for model, value in checked.models.items()
                )
            )
            self.assertEqual(restored.company_checks, checked.company_checks)
            self.assertEqual(
                bind_snapshot_hashes(restored, records)[0].content_hash,
                bind_snapshot_hashes(checked, records)[0].content_hash,
            )

    def test_shared_domain_reads_are_deduplicated_and_paged_by_distinct_target_ids(
        self,
    ):
        metadata, records = snapshots()
        owner = metadata.models["x.route"]
        metadata = replace(
            metadata,
            models={
                **metadata.models,
                "x.route": replace(
                    owner,
                    fields={
                        **owner.fields,
                        "x_other_zone_id": replace(
                            owner.fields["x_zone_id"], name="x_other_zone_id"
                        ),
                    },
                ),
            },
        )
        records = replace(
            records,
            records={
                **records.records,
                "x.zone": tuple(
                    TargetRecord("x.zone", value, {"code": str(value)})
                    for value in range(1, 1202)
                ),
            },
        )
        calls = []

        def post(_model, _method, payload):
            ids = payload["domain"][0][2]
            calls.append(ids)
            return [{"id": value} for value in ids]

        checked = capture_company_checks(
            metadata,
            records,
            {("x.route", "x_zone_id"): DOMAIN, ("x.route", "x_other_zone_id"): DOMAIN},
            105,
            post,
        )
        self.assertEqual([len(page) for page in calls], [500, 500, 201])
        self.assertEqual(len(checked.company_checks), 2)
        self.assertTrue(
            all(not item.incompatible_ids for item in checked.company_checks)
        )
