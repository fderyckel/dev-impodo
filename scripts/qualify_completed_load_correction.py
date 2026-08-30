"""Qualify completed-load corrections against disposable Odoo 19.

The runner exercises the real Polars sparse comparison, exact-ID correction
review, guarded JSON-2 writes, and read-back reconciliation.  It accepts only
an explicitly disposable ``impodo_correction_`` database.  Fixture setup and
exact cleanup use a narrow qualification-only seam and are reported
separately from correction calls.

The result contains counts, timings, memory, and request shapes.  It never
contains the API key, URL, user identity, fixture values, or Odoo record IDs.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
import resource
import sys
import tempfile
from time import perf_counter
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlparse
from uuid import uuid4

import polars as pl

from impodo.adapters.odoo.connectors import (
    Json2Config,
    Json2ReadConnector,
    Json2WriteIdentityConnector,
    _urllib_transport,
)
from impodo.adapters.odoo.readback import Json2ReadbackReader
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.adapters.polars_correction import (
    iter_polars_correction_candidate_batches,
    write_polars_correction_candidates,
)
from impodo.adapters.polars_transformation import PREPARED_ISSUE_COLUMN
from impodo.application.correction_execution import (
    CorrectionExecutionService,
    correction_api_scope,
)
from impodo.application.correction_orchestration import (
    CorrectionBinding,
)
from impodo.application.correction_service import (
    CorrectionPlanService,
    CorrectionReviewService,
)
from impodo.domain.compiler.columnar_transformation import (
    ColumnarExpressionStep,
    ColumnarFailureSemantics,
    ColumnarOperationKind,
    ColumnarScalarFieldProgram,
    ColumnarTransformationProgram,
    ColumnarValueProviderProgram,
)
from impodo.domain.correction import (
    CorrectionCandidate,
    CorrectionConfirmation,
    CorrectionPlanError,
    CorrectionValueKind,
)
from impodo.domain.correction_execution import CorrectionExecutionSnapshot
from impodo.domain.correction_origin import (
    CorrectionTargetIndexEntry,
    ProtectedCorrectionArtifactReference,
)
from impodo.domain.execution.models import MAX_CREATE_BATCH_ROWS
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.prepared_snapshot import PreparedSnapshot
from impodo.domain.serialization import canonical_json, content_hash
from impodo.domain.shared.access import (
    LOCAL_ACTOR,
    CapabilityAuthorizationPolicy,
)
from impodo.domain.shared.models import canonical_json_bytes
from impodo.domain.source_snapshot import SOURCE_ROW_COLUMN


DISPOSABLE_DATABASE_PREFIX = "impodo_correction_"
SCALAR_SOURCE_ROWS = 999
SCALAR_CORRECTIONS = 768
SCALAR_UNCHANGED = SCALAR_SOURCE_ROWS - SCALAR_CORRECTIONS
RELATIONSHIP_CORRECTIONS = 37
REJECTION_ROWS = 2
UNKNOWN_ROWS = 51
DATASET_ID = "dataset:1234567890abcdef12345678"
PRODUCT_MODEL = "product.template"
UOM_MODEL = "uom.uom"
NOW = datetime.now(UTC)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8069")
    parser.add_argument("--database", required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _connection_mode(base_url: str) -> str:
    hostname = (urlparse(base_url).hostname or "").casefold()
    return "LOCAL" if hostname in {"127.0.0.1", "::1"} else "REMOTE"


def _require_disposable_database(database: str) -> None:
    if not database.startswith(DISPOSABLE_DATABASE_PREFIX):
        raise SystemExit(
            "Correction qualification accepts only an impodo_correction_ "
            "disposable database"
        )


def _file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _physical_schema_hash(path: Path) -> str:
    schema = pl.read_parquet_schema(path)
    return content_hash(
        {
            "columns": [
                {"name": name, "type": str(data_type)}
                for name, data_type in schema.items()
            ]
        }
    )


def _scalar_program(mapping_hash: str) -> ColumnarTransformationProgram:
    field = ColumnarScalarFieldProgram(
        target_field="active",
        output_ordinal=0,
        value_type="boolean",
        source_label="Status",
        transformation_rules="Qualification fixture",
        provider=ColumnarValueProviderProgram(
            operation=ColumnarOperationKind.USE_CONSTANT,
            source=None,
            literal_value="false",
            value_mappings=(),
        ),
        transform_steps=(),
        required_step=None,
        conversion_step=ColumnarExpressionStep(
            operation=ColumnarOperationKind.PARSE_BOOLEAN
        ),
        post_conversion_steps=(),
        validation_steps=(),
        required=False,
        required_on_create=False,
        compare=True,
        validate_only=False,
        null_policy="KEEP_NULL",
        impact_required=False,
        failures=ColumnarFailureSemantics(),
    )
    return ColumnarTransformationProgram(
        dataset_id=DATASET_ID,
        dataset_name="Products",
        target_model=PRODUCT_MODEL,
        target_mode="CREATE_UPDATE",
        mapping_content_hash=mapping_hash,
        source_selection_hash=content_hash("phase-6-source-selection"),
        schema_hash=content_hash("phase-6-product-schema"),
        inputs=(),
        source_identity=(),
        target_identity=(),
        target_scope=(),
        scalar_fields=(field,),
        relationships=(),
        set_requirements=(),
    )


def _prepared_frame(values: Sequence[bool]) -> pl.DataFrame:
    count = len(values)
    return pl.DataFrame(
        {
            "__impodo_prepared_ordinal": pl.Series(range(count), dtype=pl.UInt32),
            SOURCE_ROW_COLUMN: pl.Series(range(2, count + 2), dtype=pl.UInt64),
            "__impodo_scalar_prepared_000000": pl.Series(
                ("true" if value else "false" for value in values),
                dtype=pl.String,
            ),
            "__impodo_scalar_value_000000": pl.Series(values, dtype=pl.Boolean),
            PREPARED_ISSUE_COLUMN: pl.Series(
                [[] for _ in range(count)],
                dtype=pl.List(pl.String),
            ),
        }
    )


def _prepared_snapshot(
    path: Path,
    program: ColumnarTransformationProgram,
) -> PreparedSnapshot:
    return PreparedSnapshot.create(
        workspace_id="11111111-1111-4111-8111-111111111111",
        dataset_id=program.dataset_id,
        dataset_name=program.dataset_name,
        source_snapshot_hash=content_hash("phase-6-source-snapshot"),
        mapping_hash=program.mapping_content_hash,
        schema_hash=program.schema_hash,
        transformation_program_hash=program.content_hash,
        row_count=SCALAR_SOURCE_ROWS,
        physical_schema_hash=_physical_schema_hash(path),
        parquet_sha256=_file_hash(path),
        created_at=NOW,
    )


def run_vectorized_fixture() -> tuple[tuple[CorrectionCandidate, ...], dict[str, Any]]:
    """Reduce 999 prepared rows to the exact 768 sparse scalar candidates."""

    previous_values = (False,) * SCALAR_SOURCE_ROWS
    corrected_values = (
        (True,) * SCALAR_CORRECTIONS + (False,) * SCALAR_UNCHANGED
    )
    previous_program = _scalar_program(content_hash("phase-6-previous-mapping"))
    corrected_program = _scalar_program(content_hash("phase-6-corrected-mapping"))
    started = perf_counter()
    with tempfile.TemporaryDirectory(prefix="impodo-correction-phase6-") as name:
        root = Path(name)
        previous_path = root / "previous.parquet"
        corrected_path = root / "corrected.parquet"
        candidate_path = root / "candidates.parquet"
        _prepared_frame(previous_values).write_parquet(previous_path)
        _prepared_frame(corrected_values).write_parquet(corrected_path)
        previous_snapshot = _prepared_snapshot(previous_path, previous_program)
        corrected_snapshot = _prepared_snapshot(corrected_path, corrected_program)
        artifact = write_polars_correction_candidates(
            previous_path,
            previous_snapshot,
            previous_program,
            corrected_path,
            corrected_snapshot,
            corrected_program,
            candidate_path,
        )
        candidates = tuple(
            item
            for batch in iter_polars_correction_candidate_batches(artifact)
            for item in batch
        )
        artifact_bytes = candidate_path.stat().st_size
    if (
        len(candidates) != SCALAR_CORRECTIONS
        or any(
            item.target_field != "active"
            or item.previous is not False
            or item.corrected is not True
            for item in candidates
        )
    ):
        raise RuntimeError("The vectorized correction fixture is inconsistent")
    return candidates, {
        "candidate_artifact_bytes": artifact_bytes,
        "candidate_count": len(candidates),
        "prepared_artifacts": 2,
        "source_rows": SCALAR_SOURCE_ROWS,
        "unchanged_intents": SCALAR_UNCHANGED,
        "wall_seconds": round(perf_counter() - started, 6),
    }


@dataclass(slots=True)
class RecordingTransport:
    counts: Counter[str]
    elapsed_seconds: float = 0.0

    def __init__(self) -> None:
        self.counts = Counter()
        self.elapsed_seconds = 0.0

    def __call__(self, url, headers, body, timeout_seconds, method):
        path = urlparse(url).path.rstrip("/").split("/")
        key = ".".join(path[-2:]) if len(path) >= 2 else path[-1]
        self.counts[key] += 1
        started = perf_counter()
        try:
            return _urllib_transport(
                url,
                headers,
                body,
                timeout_seconds,
                method,
            )
        finally:
            self.elapsed_seconds += perf_counter() - started

    def public_counts(self) -> dict[str, int]:
        return dict(sorted(self.counts.items()))


class InjectedWriteTransport:
    """Inject one known rejection or lost response at the writer boundary."""

    def __init__(self, recorder: RecordingTransport, mode: str) -> None:
        self.recorder = recorder
        self.mode = mode
        self.injected = False

    def __call__(self, url, headers, body, timeout_seconds, method):
        is_write = urlparse(url).path.endswith("/product.template/write")
        if is_write and not self.injected:
            self.injected = True
            if self.mode == "rejected":
                self.recorder.counts["product.template.write"] += 1
                return 403, {"message": "qualification rejection"}
            if self.mode == "unknown":
                self.recorder(url, headers, body, timeout_seconds, method)
                raise TimeoutError("qualification lost response")
        return self.recorder(url, headers, body, timeout_seconds, method)


class QualificationAdmin:
    """Fixed setup/cleanup seam for exact synthetic qualification records."""

    def __init__(self, config: Json2Config, recorder: RecordingTransport) -> None:
        self.config = config
        self.recorder = recorder

    def search_read(
        self,
        model: str,
        domain: Sequence[Sequence[Any]],
        fields: Sequence[str],
        *,
        limit: int,
        order: str = "id asc",
    ) -> tuple[Mapping[str, Any], ...]:
        if model not in {PRODUCT_MODEL, UOM_MODEL} or not 1 <= limit <= 2_000:
            raise RuntimeError("Qualification setup read is outside its fixed scope")
        response = self._post(
            model,
            "search_read",
            {
                "domain": list(domain),
                "fields": ["id", *fields],
                "limit": limit,
                "order": order,
                "context": {"active_test": False},
            },
        )
        if not isinstance(response, list) or any(
            not isinstance(item, Mapping) for item in response
        ):
            raise RuntimeError("Qualification setup read returned invalid data")
        return tuple(response)

    def unlink_exact_products(self, identifiers: Sequence[int]) -> None:
        ids = tuple(identifiers)
        if (
            not ids
            or len(ids) > 2_000
            or len(set(ids)) != len(ids)
            or any(type(item) is not int or item <= 0 for item in ids)
        ):
            raise RuntimeError("Qualification cleanup IDs are invalid")
        result = self._post(
            PRODUCT_MODEL,
            "unlink",
            {"ids": list(ids), "context": {"active_test": False}},
        )
        if result is not True:
            raise RuntimeError("Qualification cleanup was not acknowledged")

    def write_exact_product(self, identifier: int, values: Mapping[str, Any]) -> None:
        if type(identifier) is not int or identifier <= 0 or set(values) != {"name"}:
            raise RuntimeError("Qualification concurrent change is outside scope")
        result = self._post(
            PRODUCT_MODEL,
            "write",
            {
                "ids": [identifier],
                "vals": dict(values),
                "context": {"active_test": False},
            },
        )
        if result is not True:
            raise RuntimeError("Qualification concurrent change was rejected")

    def _post(self, model: str, operation: str, payload: Mapping[str, Any]) -> Any:
        allowed = {
            (PRODUCT_MODEL, "search_read"),
            (PRODUCT_MODEL, "unlink"),
            (PRODUCT_MODEL, "write"),
            (UOM_MODEL, "search_read"),
        }
        if (model, operation) not in allowed:
            raise RuntimeError("Qualification setup operation is outside scope")
        url = (
            f"{self.config.base_url}/json/2/"
            f"{quote(model, safe='.')}/{quote(operation, safe='')}"
        )
        status, response = self.recorder(
            url,
            {
                "Authorization": f"bearer {self.config.api_key}",
                "Content-Type": "application/json; charset=utf-8",
                "X-Odoo-Database": self.config.database,
                "User-Agent": "impodo-correction-qualification",
            },
            canonical_json_bytes(dict(payload)),
            self.config.timeout_seconds,
            "POST",
        )
        if status != 200:
            raise RuntimeError("Qualification setup operation failed")
        return response


class _Journal:
    def __init__(self) -> None:
        self.run = None
        self.events: list[tuple[str, object]] = []

    def start_run(self, workspace_id, run, *, actor, correction_plan_hash=""):
        del workspace_id, actor
        self.run = run
        self.events.append(("start", correction_plan_hash))

    def record_batch_started(self, workspace_id, run_id, rows):
        del workspace_id, run_id
        self.events.append(("before-write", len(rows)))
        self._replace(rows)

    def record_outcomes(self, workspace_id, run_id, rows):
        del workspace_id, run_id
        self.events.append(("outcome", len(rows)))
        self._replace(rows)

    def _replace(self, changed) -> None:
        by_id = {item.row_id: item for item in changed}
        self.run = replace(
            self.run,
            rows=tuple(by_id.get(item.row_id, item) for item in self.run.rows),
        )

    def finish_run(self, workspace_id, run_id, status, *, actor):
        del workspace_id, run_id, actor
        self.run = replace(self.run, status=status, completed_at=datetime.now(UTC))
        self.events.append(("finish", status.value))
        return self.run


class _Results:
    def __init__(self) -> None:
        self.report = None

    def publish(self, workspace_id, report, *, actor):
        del workspace_id, actor
        self.report = report


def _reference(identifier: str, logical_hash: str, name: str):
    return ProtectedCorrectionArtifactReference(
        artifact_id=identifier,
        logical_hash=logical_hash,
        storage_key=f"qualification/{name}.ipe",
        artifact_hash=content_hash(f"qualification-{name}"),
    )


class _Bindings:
    def __init__(self, completed_workspace_id, plan, confirmation) -> None:
        self.invalidations = 0
        self.completions = 0
        self.binding = CorrectionBinding(
            correction_binding_id=str(uuid4()),
            project_id=plan.project_id,
            data_version_id=str(uuid4()),
            completed_migration_run_id=plan.completed_migration_run_id,
            completed_workspace_id=completed_workspace_id,
            origin=_reference(str(uuid4()), content_hash("origin"), "origin"),
            target_index=_reference(str(uuid4()), content_hash("index"), "index"),
            successor_migration_run_id=plan.successor_migration_run_id,
            successor_workspace_id=plan.workspace_id,
            current_mapping_hash=content_hash("current-mapping"),
            current_prepared_hash=plan.corrected_prepared_hash,
            current_plan=_reference(plan.plan_id, plan.plan_hash, "plan"),
            current_confirmation=_reference(
                confirmation.confirmation_id,
                confirmation.confirmation_hash,
                "confirmation",
            ),
            optimistic_revision=4,
            created_at=NOW,
            updated_at=NOW,
        )

    def get_for_completed_workspace(self, completed_workspace_id):
        if completed_workspace_id != self.binding.completed_workspace_id:
            return None
        return self.binding

    def invalidate_plan(self, *args, **kwargs):
        del args, kwargs
        self.invalidations += 1
        self.binding = replace(
            self.binding,
            current_plan=None,
            current_confirmation=None,
            optimistic_revision=self.binding.optimistic_revision + 1,
        )
        return self.binding

    def complete_verified_successor(self, *args, **kwargs):
        del args, kwargs
        self.completions += 1
        self.binding = replace(
            self.binding,
            optimistic_revision=self.binding.optimistic_revision + 1,
        )
        return self.binding


def _review_scope() -> OdooApiScope:
    return OdooApiScope(
        preview_hash=content_hash("phase-6-review-scope"),
        models=(
            OdooModelScope(
                PRODUCT_MODEL,
                read_fields=("active", "name", "uom_id"),
            ),
            OdooModelScope(
                UOM_MODEL,
                read_fields=("name",),
                lookup_fields=("name",),
            ),
        ),
    )


def _target_entry(candidate: CorrectionCandidate, odoo_id: int):
    return CorrectionTargetIndexEntry(
        dataset=candidate.dataset,
        source_row=candidate.source_row,
        row_id=str(uuid4()),
        target_model=candidate.target_model,
        odoo_id=odoo_id,
        completed_disposition="CREATE",
        target_binding_hash="",
    )


def _review(
    config: Json2Config,
    candidate_batches: Sequence[tuple[CorrectionCandidate, ...]],
    targets: Sequence[CorrectionTargetIndexEntry],
    recorder: RecordingTransport,
):
    scope = _review_scope()
    reader = Json2ReadbackReader(config, scope, transport=recorder)
    return CorrectionReviewService().review(
        candidate_batches,
        targets,
        reader=reader,
        expected_target_hash=reader.target_hash,
        expected_reader_scope_hash=scope.semantic_hash,
    )


def _create_plan(review, read_identity):
    return CorrectionPlanService().create_plan(
        review,
        plan_id=str(uuid4()),
        project_id=str(uuid4()),
        completed_migration_run_id=str(uuid4()),
        successor_migration_run_id=str(uuid4()),
        workspace_id=str(uuid4()),
        origin_evidence_hash=content_hash("phase-6-origin"),
        previous_prepared_hash=content_hash("phase-6-previous"),
        corrected_prepared_hash=content_hash("phase-6-corrected"),
        read_credential_binding_hash=content_hash("phase-6-read-credential"),
        read_identity=read_identity,
        created_by=LOCAL_ACTOR.identity,
        created_at=datetime.now(UTC),
    )


def _execute(
    config: Json2Config,
    plan,
    write_identity,
    recorder: RecordingTransport,
    *,
    injected_failure: str = "",
):
    confirmation = CorrectionConfirmation.create(
        confirmation_id=str(uuid4()),
        plan=plan,
        write_credential_binding_hash=content_hash("phase-6-write-credential"),
        write_identity=write_identity,
        confirmed_by=LOCAL_ACTOR.identity,
        confirmed_at=datetime.now(UTC),
    )
    snapshot = CorrectionExecutionSnapshot.create(
        plan,
        confirmation,
        target_database=config.database,
    )
    scope = correction_api_scope(snapshot)
    reader = Json2ReadbackReader(config, scope, transport=recorder)
    writer_transport = (
        InjectedWriteTransport(recorder, injected_failure)
        if injected_failure
        else recorder
    )
    writer = Json2WriteExecutor(config, scope, transport=writer_transport)
    completed_workspace_id = str(uuid4())
    bindings = _Bindings(completed_workspace_id, plan, confirmation)
    journal = _Journal()
    service = CorrectionExecutionService(
        bindings,
        object(),
        journal,
        _Results(),
        CapabilityAuthorizationPolicy(),
    )
    result = service.execute(
        completed_workspace_id,
        plan,
        confirmation,
        target_database=config.database,
        write_credential_binding_hash=content_hash("phase-6-write-credential"),
        write_identity=write_identity,
        reader=reader,
        writer=writer,
        actor=LOCAL_ACTOR,
    )
    return result, bindings, journal


def _seed_scope() -> OdooApiScope:
    fields = ("active", "default_code", "name", "uom_id")
    return OdooApiScope(
        preview_hash=content_hash("phase-6-seed-scope"),
        models=(
            OdooModelScope(
                PRODUCT_MODEL,
                write_fields=fields,
                read_fields=fields,
            ),
        ),
    )


def _seed_products(
    config: Json2Config,
    recorder: RecordingTransport,
    previous_uom_id: int,
    result: dict[str, tuple[int, ...]],
) -> dict[str, tuple[int, ...]]:
    writer = Json2WriteExecutor(config, _seed_scope(), transport=recorder)
    groups: dict[str, Sequence[Mapping[str, Any]]] = {
        "scalar": tuple(
            {
                "active": False,
                "default_code": f"IMPODO-CORR-P6-S-{index:04d}",
                "name": f"Impodo correction scalar {index:04d}",
            }
            for index in range(1, SCALAR_CORRECTIONS + 1)
        ),
        "relationship": tuple(
            {
                "active": True,
                "default_code": f"IMPODO-CORR-P6-R-{index:04d}",
                "name": f"Impodo correction relationship {index:04d}",
                "uom_id": previous_uom_id,
            }
            for index in range(1, RELATIONSHIP_CORRECTIONS + 1)
        ),
        "conflict": (
            {
                "active": True,
                "default_code": "IMPODO-CORR-P6-C-0001",
                "name": "old",
            },
        ),
        "rejected": tuple(
            {
                "active": False,
                "default_code": f"IMPODO-CORR-P6-K-{index:04d}",
                "name": f"Impodo correction rejected {index:04d}",
            }
            for index in range(1, REJECTION_ROWS + 1)
        ),
        "unknown": tuple(
            {
                "active": False,
                "default_code": f"IMPODO-CORR-P6-U-{index:04d}",
                "name": f"Impodo correction unknown {index:04d}",
            }
            for index in range(1, UNKNOWN_ROWS + 1)
        ),
    }
    for name, values in groups.items():
        result[name] = ()
        for start in range(0, len(values), MAX_CREATE_BATCH_ROWS):
            created = writer.create_rows(
                PRODUCT_MODEL,
                values[start : start + MAX_CREATE_BATCH_ROWS],
            )
            result[name] = (*result[name], *created)
    return result


def _choose_uoms(admin: QualificationAdmin) -> tuple[tuple[str, int], tuple[str, int]]:
    records = admin.search_read(
        UOM_MODEL,
        (),
        ("name", "relative_uom_id", "active"),
        limit=500,
    )
    name_counts = Counter(
        item.get("name") for item in records if isinstance(item.get("name"), str)
    )
    by_category: dict[int, list[tuple[str, int]]] = {}
    for item in records:
        identifier = item.get("id")
        name = item.get("name")
        relative = item.get("relative_uom_id")
        category_id = (
            relative[0]
            if isinstance(relative, (list, tuple)) and len(relative) == 2
            else identifier
        )
        if (
            type(identifier) is int
            and identifier > 0
            and isinstance(name, str)
            and name_counts[name] == 1
            and type(category_id) is int
            and bool(item.get("active"))
        ):
            by_category.setdefault(category_id, []).append((name, identifier))
    choices = next(
        (sorted(items)[:2] for items in by_category.values() if len(items) >= 2),
        None,
    )
    if choices is None:
        raise RuntimeError("Two exact existing units in one category are required")
    return choices[0], choices[1]


def _relationship_candidates(
    previous_name: str,
    corrected_name: str,
) -> tuple[CorrectionCandidate, ...]:
    return tuple(
        CorrectionCandidate(
            dataset="Product units",
            source_row=index,
            target_model=PRODUCT_MODEL,
            target_field="uom_id",
            value_kind=CorrectionValueKind.MANY2ONE,
            previous=(previous_name,),
            corrected=(corrected_name,),
            relationship_model=UOM_MODEL,
            relationship_key_fields=("name",),
        )
        for index in range(1, RELATIONSHIP_CORRECTIONS + 1)
    )


def _scalar_candidates(dataset: str, count: int, field: str = "active"):
    return tuple(
        CorrectionCandidate(
            dataset=dataset,
            source_row=index,
            target_model=PRODUCT_MODEL,
            target_field=field,
            value_kind=CorrectionValueKind.SCALAR,
            previous=("old" if field == "name" else False),
            corrected=("new" if field == "name" else True),
        )
        for index in range(1, count + 1)
    )


def _rss_mib() -> float:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    bytes_value = raw if sys.platform == "darwin" else raw * 1024
    return round(bytes_value / (1024 * 1024), 3)


def _emit(payload: Mapping[str, Any], output: Path) -> None:
    serialized = canonical_json(payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


def main() -> int:
    args = _arguments()
    _require_disposable_database(args.database)
    api_key = args.api_key_file.read_text("utf-8").strip()
    if not api_key:
        raise SystemExit("The correction qualification API key file is empty")
    config = Json2Config(
        base_url=args.base_url.rstrip("/"),
        database=args.database,
        api_key=api_key,
        connection_mode=_connection_mode(args.base_url),
        page_size=100,
        retries=0,
        relevant_modules=("base", "product", "uom"),
    )
    total_started = perf_counter()
    scalar_candidates, vectorized = run_vectorized_fixture()
    setup_recorder = RecordingTransport()
    admin = QualificationAdmin(config, setup_recorder)
    existing = admin.search_read(
        PRODUCT_MODEL,
        (("default_code", "=like", "IMPODO-CORR-P6-%"),),
        ("default_code",),
        limit=2_000,
    )
    if existing:
        raise RuntimeError("The disposable database already contains this fixture")
    (previous_name, previous_uom_id), (corrected_name, _corrected_uom_id) = (
        _choose_uoms(admin)
    )
    seeded: dict[str, tuple[int, ...]] = {}
    cleanup_verified = False
    try:
        seed_started = perf_counter()
        _seed_products(config, setup_recorder, previous_uom_id, seeded)
        seed_seconds = perf_counter() - seed_started
        relationship_candidates = _relationship_candidates(
            previous_name,
            corrected_name,
        )
        main_targets = tuple(
            _target_entry(candidate, identifier)
            for candidate, identifier in zip(
                scalar_candidates,
                seeded["scalar"],
                strict=True,
            )
        ) + tuple(
            _target_entry(candidate, identifier)
            for candidate, identifier in zip(
                relationship_candidates,
                seeded["relationship"],
                strict=True,
            )
        )

        identity_recorder = RecordingTransport()
        identity_connector = Json2ReadConnector(
            config,
            transport=identity_recorder,
        )
        fingerprint = identity_connector.get_target_fingerprint()
        read_identity = identity_connector.probe_read_identity(
            (PRODUCT_MODEL, UOM_MODEL)
        )
        write_identity = Json2WriteIdentityConnector(
            config,
            transport=identity_recorder,
        ).probe_write_identity((PRODUCT_MODEL,), (PRODUCT_MODEL,))

        review_recorder = RecordingTransport()
        review_started = perf_counter()
        review = _review(
            config,
            (scalar_candidates, relationship_candidates),
            main_targets,
            review_recorder,
        )
        review_seconds = perf_counter() - review_started
        if (
            review.blockers
            or len(review.ready_fields)
            != SCALAR_CORRECTIONS + RELATIONSHIP_CORRECTIONS
        ):
            raise RuntimeError("The main correction review did not pass")
        plan = _create_plan(review, read_identity)

        execution_recorder = RecordingTransport()
        execution_started = perf_counter()
        successful, successful_bindings, successful_journal = _execute(
            config,
            plan,
            write_identity,
            execution_recorder,
        )
        execution_seconds = perf_counter() - execution_started
        expected_main = SCALAR_CORRECTIONS + RELATIONSHIP_CORRECTIONS
        if (
            successful.execution.committed_count != expected_main
            or successful.reconciliation.verified_count != expected_main
            or successful.reconciliation.fallout_count
            or successful_bindings.completions != 1
        ):
            raise RuntimeError("The main correction did not verify")

        repeat_recorder = RecordingTransport()
        repeat_started = perf_counter()
        repeated = _review(
            config,
            (scalar_candidates, relationship_candidates),
            main_targets,
            repeat_recorder,
        )
        repeat_seconds = perf_counter() - repeat_started
        if (
            repeated.blockers
            or repeated.ready_fields
            or repeated.already_corrected_count != expected_main
        ):
            raise RuntimeError("The repeated correction review is not idempotent")

        conflict_candidates = _scalar_candidates("Conflict", 1, field="name")
        conflict_targets = (
            _target_entry(conflict_candidates[0], seeded["conflict"][0]),
        )
        conflict_review = _review(
            config,
            (conflict_candidates,),
            conflict_targets,
            RecordingTransport(),
        )
        conflict_plan = _create_plan(conflict_review, read_identity)
        admin.write_exact_product(
            seeded["conflict"][0],
            {"name": "concurrent"},
        )
        conflict_recorder = RecordingTransport()
        conflict_writes = 0
        conflict_invalidations = 0
        try:
            _execute(
                config,
                conflict_plan,
                write_identity,
                conflict_recorder,
            )
        except CorrectionPlanError as error:
            if "changed after confirmation" not in str(error):
                raise
            conflict_writes = conflict_recorder.counts["product.template.write"]
            conflict_invalidations = 1
        else:
            raise RuntimeError("The concurrent change did not stop execution")

        rejected_candidates = _scalar_candidates("Known rejection", REJECTION_ROWS)
        rejected_targets = tuple(
            _target_entry(candidate, identifier)
            for candidate, identifier in zip(
                rejected_candidates,
                seeded["rejected"],
                strict=True,
            )
        )
        rejected_review = _review(
            config,
            (rejected_candidates,),
            rejected_targets,
            RecordingTransport(),
        )
        rejected_plan = _create_plan(rejected_review, read_identity)
        rejected_recorder = RecordingTransport()
        rejected, rejected_bindings, _rejected_journal = _execute(
            config,
            rejected_plan,
            write_identity,
            rejected_recorder,
            injected_failure="rejected",
        )

        unknown_candidates = _scalar_candidates("Unknown response", UNKNOWN_ROWS)
        unknown_targets = tuple(
            _target_entry(candidate, identifier)
            for candidate, identifier in zip(
                unknown_candidates,
                seeded["unknown"],
                strict=True,
            )
        )
        unknown_review = _review(
            config,
            (unknown_candidates,),
            unknown_targets,
            RecordingTransport(),
        )
        unknown_plan = _create_plan(unknown_review, read_identity)
        unknown_recorder = RecordingTransport()
        unknown, unknown_bindings, _unknown_journal = _execute(
            config,
            unknown_plan,
            write_identity,
            unknown_recorder,
            injected_failure="unknown",
        )

        payload = {
            "captured_at": datetime.now(UTC).isoformat(),
            "connection_mode": config.connection_mode,
            "database": config.database,
            "fixture": {
                "relationship_corrections": RELATIONSHIP_CORRECTIONS,
                "scalar_corrections": SCALAR_CORRECTIONS,
                "scalar_source_rows": SCALAR_SOURCE_ROWS,
                "scalar_unchanged_intents": SCALAR_UNCHANGED,
            },
            "identity_probe_calls": identity_recorder.public_counts(),
            "main_execution": {
                "before_write_journal_events": sum(
                    event[0] == "before-write" for event in successful_journal.events
                ),
                "committed": successful.execution.committed_count,
                "fallout": successful.reconciliation.fallout_count,
                "readback_verified": successful.reconciliation.verified_count,
                "requests": execution_recorder.public_counts(),
                "uom_write_calls": execution_recorder.counts["uom.uom.write"],
            },
            "main_review": {
                "blockers": len(review.blockers),
                "ready_fields": len(review.ready_fields),
                "requests": review_recorder.public_counts(),
            },
            "module_versions": dict(sorted(fingerprint.module_versions.items())),
            "odoo_version": fingerprint.odoo_version,
            "peak_rss_mib": _rss_mib(),
            "repeat_review": {
                "already_corrected": repeated.already_corrected_count,
                "ready_fields": len(repeated.ready_fields),
                "requests": repeat_recorder.public_counts(),
            },
            "safety_scenarios": {
                "concurrent_change": {
                    "invalidated": conflict_invalidations,
                    "write_calls": conflict_writes,
                },
                "known_rejection": {
                    "failed": rejected.execution.failed_count,
                    "unknown": rejected.execution.unknown_count,
                    "verified": rejected.reconciliation.verified_count,
                    "binding_completions": rejected_bindings.completions,
                },
                "lost_response": {
                    "blocked": unknown.execution.blocked_count,
                    "execution_unknown": unknown.execution.unknown_count,
                    "readback_verified": unknown.reconciliation.verified_count,
                    "binding_completions": unknown_bindings.completions,
                },
            },
            "setup": {
                "created_products": sum(len(items) for items in seeded.values()),
                "requests": setup_recorder.public_counts(),
                "uom_write_calls": setup_recorder.counts["uom.uom.write"],
            },
            "status": "verified",
            "target_hash": fingerprint.target_hash,
            "timing_seconds": {
                "execution_and_readback": round(execution_seconds, 6),
                "review": round(review_seconds, 6),
                "repeat_review": round(repeat_seconds, 6),
                "seed": round(seed_seconds, 6),
                "total_before_cleanup": round(perf_counter() - total_started, 6),
            },
            "vectorized_comparison": vectorized,
        }
    finally:
        all_ids = tuple(
            identifier for items in seeded.values() for identifier in items
        )
        if all_ids:
            admin.unlink_exact_products(all_ids)
            remaining = admin.search_read(
                PRODUCT_MODEL,
                (("id", "in", list(all_ids)),),
                (),
                limit=len(all_ids),
            )
            cleanup_verified = not remaining
    if not cleanup_verified:
        raise RuntimeError("Qualification cleanup did not remove every fixture record")
    payload["cleanup"] = {
        "deleted_products": sum(len(items) for items in seeded.values()),
        "remaining_products": 0,
        "verified": True,
    }
    payload["timing_seconds"]["total_with_cleanup"] = round(
        perf_counter() - total_started,
        6,
    )
    _emit(payload, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
