"""Qualify canonical reference bytes and immutable native replay evidence."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest

import duckdb

from impodo.adapters.duckdb.native_prepared_projection import (
    projected_encoded_rows_sql,
    supports_clean_native_projection,
)
from impodo.adapters.duckdb.preparation_session_repository import PreparationSessionRepository
from impodo.adapters.duckdb.staging_repository import StagingRepository
from impodo.domain.compiler.columnar_transformation import (
    ColumnarSupport,
    compile_columnar_transformation_program,
)
from impodo.domain.mapping.canonicalization import canonicalize_mapping_definition
from impodo.domain.mapping.contracts import (
    ConstantBusinessReference,
    ConstantReferenceComponent,
    IdentityComponentMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    RelationshipValueSource,
    ResolverOrigin,
    ValueMapping,
)
from impodo.domain.preparation.staging_contracts import CanonicalRow, StagingDatasetRole
from impodo.domain.prepared_snapshot import PreparedSnapshot
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.shared.models import canonical_json_bytes
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.staging.canonical_projection import canonical_prepared_session_row
from impodo.domain.staging.preparation_session import PreparedCanonicalProjection
from impodo.domain.staging.transformation_impact import _TransformationImpactCollector
from tests.integration.columnar import test_polars_transformation as fixtures
from tests.integration.duckdb import test_preparation_session as session_fixtures
from tests.support.paths import REPOSITORY_ROOT


LEGACY_FIXTURE = (
    REPOSITORY_ROOT / "tests/fixtures/preparation/native-projection-v3.json"
)


class NativeReferenceSerializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT / ".tmp")
        self.root = Path(self.temporary.name)
        self.case_number = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _prepare(self, program, rows):
        self.case_number += 1
        root = self.root / str(self.case_number)
        root.mkdir()
        selection = fixtures._selection()
        selection = replace(
            selection,
            datasets=(replace(selection.datasets[0], row_count=len(rows)),),
        )
        path, snapshot = fixtures._write_snapshot(root, selection, rows)
        prepared_path, prepared = fixtures._write_prepared_snapshot(
            root, path, snapshot, program,
        )
        return selection, snapshot, prepared_path, prepared

    def _project(self, projection, path, *, batch_size=17):
        rows = []
        with duckdb.connect(config={"threads": "1", "memory_limit": "192MB"}) as connection:
            for offset in range(0, projection.row_count, batch_size):
                rows.extend(connection.execute(
                    projected_encoded_rows_sql(projection),
                    [str(path), offset, min(offset + batch_size, projection.row_count)],
                ).fetchall())
        return tuple(rows)

    def test_historical_projection_keeps_exact_captured_bytes_and_hashes(self):
        cases = json.loads(LEGACY_FIXTURE.read_text(encoding="utf-8"))["cases"]
        for case in cases:
            with self.subTest(origin=case["origin"]):
                projection = PreparedCanonicalProjection.from_portable_dict(case["projection"])
                self.assertEqual(projection.contract_version, 3)
                self.assertEqual(projection.to_portable_dict(), case["projection"])
                _, _, path, _ = self._prepare(projection.program, (fixtures._rows()[0],))
                encoded = self._project(projection, path)[0][-1]
                self.assertEqual(encoded, case["encoded_row"])
                self.assertEqual(
                    "sha256:" + sha256(encoded.encode("utf-8")).hexdigest(),
                    case["encoded_row_sha256"],
                )

                current = replace(projection, contract_version=4)
                current_encoded = self._project(current, path)[0][-1]
                canonical = CanonicalRow.from_dict(json.loads(case["encoded_row"]))
                self.assertEqual(
                    current_encoded.encode("utf-8"),
                    canonical_json_bytes(canonical.to_portable_dict()),
                )
                self.assertNotEqual(current_encoded, encoded)
                self.assertEqual(current.program.content_hash, projection.program.content_hash)

    def test_unknown_projection_versions_and_changed_programs_are_rejected(self):
        payload = json.loads(LEGACY_FIXTURE.read_text(encoding="utf-8"))["cases"][0]["projection"]
        for version in (0, 2, 5, 99, None, False, 3.5, "3", "4"):
            with self.subTest(version=version):
                changed = {**payload, "contract_version": version}
                with self.assertRaisesRegex(ValueError, "projection is invalid"):
                    PreparedCanonicalProjection.from_portable_dict(changed)
        changed = deepcopy(payload)
        changed["program"]["relationships"][0]["related_model"] = "x_other.category"
        with self.assertRaisesRegex(ValueError, "program changed"):
            PreparedCanonicalProjection.from_portable_dict(changed)

    def test_saved_historical_run_reopens_and_verifies_its_original_hash(self):
        harness = session_fixtures.PreparationSessionRepositoryTests()
        harness.setUp()
        self.addCleanup(harness.tearDown)
        workspace_id = harness.workspace_state.workspace_id
        case = json.loads(LEGACY_FIXTURE.read_text(encoding="utf-8"))["cases"][1]
        projection = PreparedCanonicalProjection.from_portable_dict(case["projection"])
        _, source, path, original = self._prepare(projection.program, (fixtures._rows()[0],))
        snapshot = PreparedSnapshot.create(
            workspace_id=workspace_id, dataset_id=original.dataset_id,
            dataset_name=original.dataset_name, source_snapshot_hash=source.content_hash,
            mapping_hash=original.mapping_hash, schema_hash=original.schema_hash,
            transformation_program_hash=original.transformation_program_hash,
            row_count=original.row_count, physical_schema_hash=original.physical_schema_hash,
            parquet_sha256=original.parquet_sha256, created_at=original.created_at,
            writer_contract_version=original.writer_contract_version,
        )
        artifacts = harness.repository._artifacts
        with artifacts.prepare_prepared_snapshot(workspace_id) as work:
            candidate = work / "prepared.parquet"
            shutil.copyfile(path, candidate)
            artifacts.publish_prepared_snapshot(
                workspace_id, candidate, snapshot.parquet_storage_key,
                expected_sha256=snapshot.parquet_sha256,
            )
        bindings = replace(
            harness.bindings, mapping_id="mapping-polars",
            physical_selection_hash=projection.program.source_selection_hash,
            source_selection_hash=projection.program.source_selection_hash,
            mapping_hash=projection.program.mapping_content_hash,
            schema_hash=projection.program.schema_hash,
            source_hashes={projection.dataset: projection.source_hash},
        )
        session = harness.repository.begin_direct_session(workspace_id, bindings, actor=LOCAL_ACTOR)
        harness.repository.bind_prepared_snapshot(workspace_id, session.session_id, snapshot)
        with artifacts.materialize_prepared_snapshot(
            workspace_id, snapshot.parquet_storage_key, expected_sha256=snapshot.parquet_sha256,
        ) as prepared_path:
            native = harness.repository.append_native_prepared_projection(
                workspace_id, session.session_id, snapshot, projection, prepared_path,
            )
        self.assertIsNotNone(native)
        assert native is not None
        collector = _TransformationImpactCollector(
            mapping_content_hash=bindings.mapping_hash, detail_limit=0,
        )
        collector.record_persisted_precomputed(native.impact_counts, ())
        stored = harness.repository.finalize_direct_session(
            workspace_id, session.session_id,
            dataset_evidence={projection.dataset: (
                projection.dataset_id, StagingDatasetRole.DIRECT, 1, projection.program.target_model,
            )}, run_issues=(), control_totals=(), impact_report=collector.report(),
        )
        reopened = PreparationSessionRepository(harness.repository._database, artifacts)
        loaded = reopened.load_stored_run(workspace_id, session.session_id)
        self.assertEqual(loaded.validated_content_hash, stored.validated_content_hash)
        batches = tuple(reopened._iter_direct_encoded_batches(
            workspace_id, session.session_id, batch_size=1,
        ))
        self.assertEqual(batches[0][0][-1], case["encoded_row"])
        replay_hash, count = reopened._hash_direct_run(
            workspace_id=workspace_id, run_id=session.session_id, bindings=bindings,
            datasets=loaded.datasets, issues=loaded.issues,
            reconciliation=loaded.reconciliation, control_totals=loaded.control_totals,
        )
        self.assertEqual(replay_hash, stored.validated_content_hash)
        self.assertEqual(count, 1)
        staging = StagingRepository(
            harness.repository._database, artifacts, source_selections=SimpleNamespace(),
        )
        self.assertIsNotNone(staging.get_canonical_staging_run(
            workspace_id, session.session_id, expected_content_hash=stored.validated_content_hash,
        ))
        database_path = reopened.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with reopened._connect(database_path) as connection:
            persisted = connection.execute(
                "SELECT projection_json FROM canonical_prepared_projection WHERE run_id = ?",
                [session.session_id],
            ).fetchone()
        assert persisted is not None
        self.assertEqual(json.loads(persisted[0]), case["projection"])
        changed = {**case["projection"], "contract_version": 4}
        with reopened._connect(database_path) as connection:
            connection.execute(
                "UPDATE canonical_prepared_projection SET projection_json = ? WHERE run_id = ?",
                [json.dumps(changed), session.session_id],
            )
        with self.assertRaisesRegex(WorkspaceError, "content hash is invalid"):
            staging.get_canonical_staging_run(
                workspace_id, session.session_id, expected_content_hash=stored.validated_content_hash,
            )

    def test_new_projection_matches_domain_bytes_for_all_reference_shapes(self):
        relationships = []
        aliases = (ValueMapping("Retail", "Consommateur 'A' \"夏\"\n💡\\path"),)
        for origin in (ResolverOrigin.DATASET, ResolverOrigin.TARGET_CATALOG,
                       ResolverOrigin.TARGET_THEN_DATASET):
            for composite, scoped in ((False, False), (False, True), (True, False), (True, True)):
                keys = ("product.category", "product.sku") if composite else ("product.category",)
                scope_keys = ("product.scope",) if scoped else ()
                uses_target = origin is not ResolverOrigin.DATASET
                relationships.append(RelationshipMapping(
                    target_field=f"x_{origin.value}_{int(composite)}_{int(scoped)}",
                    kind="many2one", source_column_keys=(*keys, *scope_keys),
                    resolver=RelationshipResolver(
                        origin=origin,
                        dataset_id=fixtures.DATASET_ID if origin is not ResolverOrigin.TARGET_CATALOG else None,
                        model="x_custom.choice" if uses_target else None,
                        key_mappings=tuple(
                            ReferenceKeyMapping(key, f"key_{index}")
                            for index, key in enumerate(keys)
                        ) if uses_target else (),
                        scope_mappings=(ReferenceKeyMapping("product.scope", "company_code"),)
                        if scoped and uses_target else (),
                        value_mappings=aliases if uses_target and not composite and not scoped else (),
                    ),
                ))
        for field, keys, scopes in (
            ("x_constant", (("name", "Unit 'A' 夏"),), ()),
            ("x_constant_scoped", (("name", "Unit 'A' 夏"),), (("company_code", "BE"),)),
            ("x_constant_composite", (("name", "Unit 'A' 夏"), ("x_code", "U-01")),
             (("company_code", "BE"),)),
        ):
            relationships.append(RelationshipMapping(
                target_field=field, kind="many2one", source_column_keys=(),
                value_source=RelationshipValueSource.CONSTANT_EXISTING,
                constant_reference=ConstantBusinessReference(
                    key_values=tuple(ConstantReferenceComponent(*item) for item in keys),
                    scope_values=tuple(ConstantReferenceComponent(*item) for item in scopes),
                ),
                resolver=RelationshipResolver(
                    origin=ResolverOrigin.TARGET_CATALOG, model="x_custom.unit",
                ),
            ))
        selection = fixtures._selection()
        base = fixtures._definition(selection)
        definition = canonicalize_mapping_definition(replace(base, datasets=(replace(
            base.datasets[0], target_model="x_custom.document",
            target_identity=(IdentityComponentMapping(
                source_column_keys=("product.id",), target_fields=("x_code",),
            ),), target_scope=(),
            fields=tuple(field for field in base.datasets[0].fields
                         if field.target_field in {"name", "quantity", "list_price"}),
            relationships=tuple(relationships),
        ),)))
        values = (
            (" Retail ", " ALT ", " BE "),
            (" Équipe \"夏\"\\unit\n💡\tline ", "a/b ~ '🧾'", " BE\u2028/site "),
            (None, None, None),
            ("Retail", None, None),
            (None, "ALT", "BE"),
            (" \t ", " ", "\n"),
        )
        rows = tuple(replace(fixtures._rows()[0], number=index + 2, values={
            **fixtures._rows()[0].values, "id": f"R{index}", "category": key,
            "sku": second_key, "scope": scope, "quantity": index,
            "price": "12,3400",
        }) for index, (key, second_key, scope) in enumerate(values))
        decision = compile_columnar_transformation_program(definition, selection, fixtures.DATASET_ID)
        self.assertEqual(decision.support, ColumnarSupport.SUPPORTED)
        assert decision.program is not None
        selection, snapshot, path, _ = self._prepare(decision.program, rows)
        expected, _ = fixtures._python_oracle(definition, selection, rows)
        self.assertTrue(all(not record.issues for record in expected),
                        tuple(record.issues for record in expected))
        projection = PreparedCanonicalProjection(
            dataset_id=fixtures.DATASET_ID, dataset="products", ordinal_start=11,
            row_count=len(rows), mode=decision.program.target_mode,
            source_hash=snapshot.content_hash, physical_dataset_id=fixtures.DATASET_ID,
            field_sources={"x_constant": (), "name": ("product.name",)},
            program=decision.program, set_based_projection=True,
        )
        self.assertEqual(projection.contract_version, 4)
        self.assertEqual(
            PreparedCanonicalProjection.from_portable_dict(projection.to_portable_dict()),
            projection,
        )
        with duckdb.connect() as connection:
            self.assertTrue(supports_clean_native_projection(connection, path, decision.program))
        expected_rows = tuple(canonical_prepared_session_row(
            dataset=record.dataset, source_row=record.source_row,
            target_model=record.target_model, source_identity=record.source_identity,
            target_identity=record.target_identity, target_scope=record.target_scope,
            scalar_values=record.scalar_values, references=record.references,
            issues=record.issues, ordinal=11 + index, mode=projection.mode,
            source_hash=snapshot.content_hash,
            source_selection_hash=decision.program.source_selection_hash,
            mapping_hash=decision.program.mapping_content_hash,
            schema_hash=decision.program.schema_hash, field_sources=projection.field_sources,
            physical_dataset_id=fixtures.DATASET_ID,
        ) for index, record in enumerate(expected))
        for batch_size in (1, 17, 5_000):
            with self.subTest(batch_size=batch_size):
                actual = self._project(projection, path, batch_size=batch_size)
                self.assertEqual(
                    tuple(row[-1] for row in actual),
                    tuple(row.row_json for row in expected_rows),
                )
                self.assertEqual(tuple(row[0] for row in actual), tuple(range(11, 17)))
        references = json.loads(self._project(projection, path)[0][-1])["references"]
        self.assertNotIn("target_scope_fields", references["x_constant"])
        self.assertEqual(references["x_constant_composite"]["key"], ["Unit 'A' 夏", "U-01"])
        self.assertEqual(references["x_constant_composite"]["scope"], ["BE"])
        self.assertEqual(references["x_target_then_dataset_0_0"]["incoming_key"], ["Retail"])
        self.assertEqual(references["x_target_then_dataset_0_0"]["key"], [aliases[0].target_value])


if __name__ == "__main__":
    unittest.main()
