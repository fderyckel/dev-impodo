"""Compare materialized and set-based product/BOM relationship preparation.

The fixture is deterministic and contains no customer values. Every measured
route runs in a fresh process and includes bounded canonical-row ingestion,
relationship finalization, and quality. Project setup is excluded.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gc
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
from threading import Event, Thread
from time import perf_counter, process_time
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from impodo.access import LOCAL_ACTOR
from impodo.adapters.duckdb.database import DuckDbDatabase
from impodo.adapters.duckdb.preparation_session_repository import (
    PreparationSessionRepository,
)
from impodo.adapters.duckdb.project_repository import ProjectRepository
from impodo.application.bounded_quality import (
    build_bounded_quality_run,
    materialize_staging_run,
)
from impodo.domain.staging.canonical_projection import (
    canonical_quality_identity_key,
    canonical_quality_record_label,
)
from impodo.domain.staging.preparation_session import (
    CanonicalPreparedSessionRow,
    PreparationSessionBindings,
)
from impodo.domain.staging.transformation_impact import TransformationImpactReport
from impodo.models import LogicalReference, PreparedRecord, canonical_json_bytes
from impodo.projects import MigrationProject, OdooConnectionMode, ProjectStatus
from impodo.quality import QualityDisposition, default_quality_ruleset, evaluate_quality
from impodo.staging_contracts import (
    BROWSER_EVALUATOR_VERSION,
    STAGING_CONTRACT_VERSION,
    StagingDatasetRole,
    canonical_row_from_prepared,
)


ROOT = Path(__file__).resolve().parents[1]
RESULT_PREFIX = "IMPODO_RELATIONSHIP_BENCHMARK_JSON="
MIB = 1024 * 1024
PHYSICAL_HASH = "sha256:" + "1" * 64
SELECTION_HASH = "sha256:" + "2" * 64
MAPPING_HASH = "sha256:" + "3" * 64
SCHEMA_HASH = "sha256:" + "4" * 64
SOURCE_HASH = "sha256:" + "5" * 64
PLAN_HASH = "sha256:" + "6" * 64


class _PeakSampler:
    """Sample process RSS without retaining benchmark-domain objects."""

    def __init__(self, process, *, interval_seconds: float = 0.02) -> None:
        self._process = process
        self._interval_seconds = interval_seconds
        self._stop = Event()
        self._thread = Thread(target=self._sample_until_stopped, daemon=True)
        self.peak_bytes = 0

    def __enter__(self) -> "_PeakSampler":
        self._sample()
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join()
        self._sample()

    def _sample_until_stopped(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            self._sample()

    def _sample(self) -> None:
        self.peak_bytes = max(
            self.peak_bytes,
            int(self._process.memory_info().rss),
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--products", type=int, default=16_000)
    parser.add_argument("--bom-lines", type=int, default=80_000)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=5_000)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--child-route",
        choices=("materialized-control", "set-based-hybrid"),
        help=argparse.SUPPRESS,
    )
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    if (
        min(
            arguments.products,
            arguments.bom_lines,
            arguments.runs,
            arguments.batch_size,
        )
        < 1
    ):
        raise SystemExit("Counts, runs, and batch size must be positive")
    if arguments.child_route:
        print(
            RESULT_PREFIX
            + json.dumps(
                _run_child(
                    route=arguments.child_route,
                    products=arguments.products,
                    bom_lines=arguments.bom_lines,
                    batch_size=arguments.batch_size,
                ),
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0

    report = _run_parent(arguments)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


def _run_parent(arguments: argparse.Namespace) -> dict[str, object]:
    paired_runs = []
    for run_number in range(1, arguments.runs + 1):
        paired_runs.append(
            {
                "run": run_number,
                "materialized_control": _spawn_child("materialized-control", arguments),
                "set_based_hybrid": _spawn_child("set-based-hybrid", arguments),
            }
        )
    for pair in paired_runs:
        control = pair["materialized_control"]
        hybrid = pair["set_based_hybrid"]
        assert isinstance(control, dict) and isinstance(hybrid, dict)
        if control["semantic_summary"] != hybrid["semantic_summary"]:
            raise RuntimeError("Relationship routes produced different semantics")
        if control["staging_content_hash"] != hybrid["staging_content_hash"]:
            raise RuntimeError("Relationship routes produced different staging")

    summary: dict[str, object] = {}
    metrics = (
        "cpu_seconds",
        "wall_seconds",
        "peak_rss_mib",
        "ending_rss_mib",
        "database_file_mib",
        "database_used_mib",
    )
    for metric in metrics:
        controls = [float(pair["materialized_control"][metric]) for pair in paired_runs]
        hybrids = [float(pair["set_based_hybrid"][metric]) for pair in paired_runs]
        control_median = statistics.median(controls)
        hybrid_median = statistics.median(hybrids)
        summary[metric] = {
            "materialized_control_median": control_median,
            "set_based_hybrid_median": hybrid_median,
            "gain_percent": (
                100.0 * (control_median - hybrid_median) / control_median
                if control_median
                else 0.0
            ),
        }
    return {
        "batch_size": arguments.batch_size,
        "bom_lines": arguments.bom_lines,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "products": arguments.products,
        "result_schema_version": 1,
        "revision": _revision(),
        "runs": paired_runs,
        "summary": summary,
        "worktree_dirty": _worktree_dirty(),
    }


def _spawn_child(route: str, arguments: argparse.Namespace) -> dict[str, object]:
    environment = os.environ.copy()
    source_path = str(ROOT / "src")
    environment["PYTHONPATH"] = (
        source_path
        if not environment.get("PYTHONPATH")
        else source_path + os.pathsep + environment["PYTHONPATH"]
    )
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--child-route",
        route,
        "--products",
        str(arguments.products),
        "--bom-lines",
        str(arguments.bom_lines),
        "--batch-size",
        str(arguments.batch_size),
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=900,
    )
    if completed.returncode:
        raise RuntimeError(
            f"Relationship benchmark child failed ({route}):\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    marked = [
        line.removeprefix(RESULT_PREFIX)
        for line in completed.stdout.splitlines()
        if line.startswith(RESULT_PREFIX)
    ]
    if len(marked) != 1:
        raise RuntimeError("Relationship benchmark child emitted invalid evidence")
    result = json.loads(marked[0])
    if not isinstance(result, dict):
        raise RuntimeError("Relationship benchmark evidence is not an object")
    return result


def _run_child(
    *,
    route: str,
    products: int,
    bom_lines: int,
    batch_size: int,
) -> dict[str, object]:
    import psutil

    with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
        root = Path(directory)
        database = DuckDbDatabase(root)
        projects = ProjectRepository(database)
        repository = PreparationSessionRepository(database)
        project = _project(products, bom_lines)
        projects.create(project, actor=LOCAL_ACTOR)
        session = repository.begin_direct_session(
            project.project_id,
            _bindings(),
            actor=LOCAL_ACTOR,
        )
        gc.collect()
        process = psutil.Process()
        baseline_rss = int(process.memory_info().rss)
        cpu_times_started = process.cpu_times()
        cpu_started = process_time()
        wall_started = perf_counter()
        with _PeakSampler(process) as sampler:
            append_cpu_started = process_time()
            append_wall_started = perf_counter()
            _append_fixture(
                repository,
                project.project_id,
                session.session_id,
                products=products,
                bom_lines=bom_lines,
                batch_size=batch_size,
                persist_relationships=route == "set-based-hybrid",
            )
            append_cpu_seconds = process_time() - append_cpu_started
            append_wall_seconds = perf_counter() - append_wall_started
            final_cpu_started = process_time()
            final_wall_started = perf_counter()
            if route == "materialized-control":
                with patch.object(
                    repository,
                    "_resolve_relationship_edges",
                    return_value=None,
                ):
                    staging = _finalize(
                        repository,
                        project.project_id,
                        session.session_id,
                        products,
                        bom_lines,
                    )
                quality = evaluate_quality(
                    project=project,
                    staging=materialize_staging_run(staging),
                    physical_rows=_physical_rows(products, bom_lines),
                    ruleset=_ruleset(project.project_id),
                    published_staging_content_hash=(
                        staging.validated_content_hash or ""
                    ),
                )
                semantic_summary = {
                    "blocked": sum(
                        item.effective_disposition is QualityDisposition.BLOCKED
                        for item in quality.row_results
                    ),
                    "issues": len(quality.issues),
                    "quarantine": len(quality.quarantine),
                    "ready": sum(
                        item.effective_disposition
                        in {
                            QualityDisposition.CANDIDATE,
                            QualityDisposition.REFERENCE,
                        }
                        for item in quality.row_results
                    ),
                    "rows": len(quality.row_results),
                }
            else:
                staging = _finalize(
                    repository,
                    project.project_id,
                    session.session_id,
                    products,
                    bom_lines,
                )
                quality = build_bounded_quality_run(
                    project=project,
                    staging=staging,
                    physical_rows=_physical_rows(products, bom_lines),
                    ruleset=_ruleset(project.project_id),
                    published_staging_content_hash=(
                        staging.validated_content_hash or ""
                    ),
                )
                semantic_summary = {
                    "blocked": int(quality.summary_counts["blocked_count"]),
                    "issues": len(quality.issues),
                    "quarantine": len(quality.quarantine),
                    "ready": int(quality.summary_counts["ready_count"]),
                    "rows": len(quality.row_results),
                }
            final_cpu_seconds = process_time() - final_cpu_started
            final_wall_seconds = perf_counter() - final_wall_started
        wall_seconds = perf_counter() - wall_started
        cpu_seconds = process_time() - cpu_started
        cpu_times_finished = process.cpu_times()
        ending_rss = int(process.memory_info().rss)
        storage = _storage(repository, root, project.project_id, session.session_id)
        return {
            "baseline_rss_mib": baseline_rss / MIB,
            "cpu_seconds": cpu_seconds,
            "cpu_system_seconds": (
                cpu_times_finished.system - cpu_times_started.system
            ),
            "cpu_user_seconds": cpu_times_finished.user - cpu_times_started.user,
            "database_file_mib": storage["database_file_bytes"] / MIB,
            "database_used_mib": storage["database_used_bytes"] / MIB,
            "ending_rss_mib": ending_rss / MIB,
            "peak_increment_mib": max(0, sampler.peak_bytes - baseline_rss) / MIB,
            "peak_rss_mib": sampler.peak_bytes / MIB,
            "phase_cpu_seconds": {
                "canonical_ingestion": append_cpu_seconds,
                "finalization_and_quality": final_cpu_seconds,
            },
            "phase_wall_seconds": {
                "canonical_ingestion": append_wall_seconds,
                "finalization_and_quality": final_wall_seconds,
            },
            "relationship_state_counts": storage["relationship_state_counts"],
            "route": route,
            "semantic_summary": semantic_summary,
            "staging_content_hash": staging.validated_content_hash,
            "wall_seconds": wall_seconds,
        }


def _project(products: int, bom_lines: int) -> MigrationProject:
    project_id = str(
        uuid5(
            NAMESPACE_URL,
            f"impodo:relationship-benchmark:{products}:{bom_lines}",
        )
    )
    return MigrationProject(
        project_id=project_id,
        name="Sanitized relationship benchmark",
        source_system="CSV",
        data_manager="Data Manager",
        functional_owner="Functional Owner",
        business_unit="Operations",
        odoo_connection_mode=OdooConnectionMode.LOCAL,
        odoo_base_url="http://127.0.0.1:8069",
        odoo_database="odoo19_local",
        intended_models=("product.product", "mrp.bom.line"),
        status=ProjectStatus.REGISTERED,
        registered_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _bindings() -> PreparationSessionBindings:
    return PreparationSessionBindings(
        mapping_id="mapping:relationship-benchmark",
        mapping_version=1,
        physical_selection_hash=PHYSICAL_HASH,
        source_selection_hash=SELECTION_HASH,
        mapping_hash=MAPPING_HASH,
        schema_hash=SCHEMA_HASH,
        derived_plan_hash=None,
        compiled_plan_hash=PLAN_HASH,
        contract_version=STAGING_CONTRACT_VERSION,
        evaluator_version=BROWSER_EVALUATOR_VERSION,
        source_hashes={"bom": SOURCE_HASH, "products": SOURCE_HASH},
    )


def _append_fixture(
    repository: PreparationSessionRepository,
    project_id: str,
    session_id: str,
    *,
    products: int,
    bom_lines: int,
    batch_size: int,
    persist_relationships: bool,
) -> None:
    batch: list[CanonicalPreparedSessionRow] = []
    ordinal = 0
    for index in range(1, bom_lines + 1):
        product_key = f"P-{((index - 1) % products) + 1:06d}"
        reference = LogicalReference(
            origin="incoming",
            key=(product_key,),
            dataset="products",
        )
        batch.append(
            _row(
                ordinal=ordinal,
                dataset="bom",
                source_row=index + 1,
                identity=f"B-{index:07d}",
                reference=reference,
                persist_relationships=persist_relationships,
            )
        )
        ordinal += 1
        if len(batch) == batch_size:
            repository.append_direct_rows(project_id, session_id, batch)
            batch.clear()
    for index in range(1, products + 1):
        batch.append(
            _row(
                ordinal=ordinal,
                dataset="products",
                source_row=index + 1,
                identity=f"P-{index:06d}",
                reference=None,
                persist_relationships=persist_relationships,
            )
        )
        ordinal += 1
        if len(batch) == batch_size:
            repository.append_direct_rows(project_id, session_id, batch)
            batch.clear()
    if batch:
        repository.append_direct_rows(project_id, session_id, batch)


def _row(
    *,
    ordinal: int,
    dataset: str,
    source_row: int,
    identity: str,
    reference: LogicalReference | None,
    persist_relationships: bool,
) -> CanonicalPreparedSessionRow:
    references = {"product_id": reference} if reference is not None else {}
    record = PreparedRecord(
        dataset=dataset,
        source_row=source_row,
        target_model=("mrp.bom.line" if dataset == "bom" else "product.product"),
        source_identity=(identity,),
        target_identity=(identity,),
        target_scope=(),
        scalar_values={"name": identity},
        references=references,
    )
    canonical = canonical_row_from_prepared(
        record,
        mode="upsert",
        source_hash=SOURCE_HASH,
        source_selection_hash=SELECTION_HASH,
        mapping_hash=MAPPING_HASH,
        schema_hash=SCHEMA_HASH,
        derived_plan_hash=None,
        field_sources={"name": ("column:name",)},
        physical_dataset_id=f"dataset:{dataset}",
        physical_source_rows=(source_row,),
    )
    return CanonicalPreparedSessionRow(
        row_id=canonical.row_id,
        ordinal=ordinal,
        dataset=dataset,
        source_row=source_row,
        target_model=canonical.target_model,
        disposition=canonical.disposition,
        source_identity=canonical.source_identity,
        row_json=canonical_json_bytes(canonical.to_portable_dict()).decode("utf-8"),
        physical_sources={f"dataset:{dataset}": (source_row,)},
        references=references if persist_relationships else {},
        record_label=canonical_quality_record_label(
            canonical.source_identity,
            canonical.target_identity,
            source_row,
        ),
        quality_identity_key=canonical_quality_identity_key(
            dataset=dataset,
            target_model=canonical.target_model,
            target_identity=canonical.target_identity,
            target_scope=canonical.target_scope,
        ),
    )


def _finalize(
    repository: PreparationSessionRepository,
    project_id: str,
    session_id: str,
    products: int,
    bom_lines: int,
):
    return repository.finalize_direct_session(
        project_id,
        session_id,
        dataset_evidence={
            "bom": (
                "dataset:bom",
                StagingDatasetRole.CHILD,
                bom_lines,
                "mrp.bom.line",
            ),
            "products": (
                "dataset:products",
                StagingDatasetRole.PARENT,
                products,
                "product.product",
            ),
        },
        run_issues=(),
        control_totals=(),
        impact_report=TransformationImpactReport(
            mapping_content_hash=MAPPING_HASH,
            evaluated_count=0,
            changed_count=0,
            fallback_count=0,
            null_count=0,
            invalid_count=0,
            provided_count=0,
            unchanged_count=0,
            rows=(),
            detail_limit=0,
        ),
    )


def _ruleset(project_id: str):
    return default_quality_ruleset(
        project_id=project_id,
        mapping_hash=MAPPING_HASH,
        schema_hash=SCHEMA_HASH,
        datasets=("bom", "products"),
    )


def _physical_rows(products: int, bom_lines: int):
    return {
        "dataset:bom": range(2, bom_lines + 2),
        "dataset:products": range(2, products + 2),
    }


def _storage(
    repository: PreparationSessionRepository,
    root: Path,
    project_id: str,
    session_id: str,
) -> dict[str, object]:
    database_path = root / project_id / "project.duckdb"
    with repository._connect(database_path) as connection:
        connection.execute("CHECKPOINT")
        size = connection.execute("PRAGMA database_size").fetchone()
        states = connection.execute(
            """
            SELECT resolution_state, COUNT(*)
              FROM preparation_relationship_edge
             WHERE session_id = ?
             GROUP BY resolution_state
             ORDER BY resolution_state
            """,
            [session_id],
        ).fetchall()
    if size is None:
        raise RuntimeError("DuckDB did not report database size")
    block_size = int(size[2])
    return {
        "database_file_bytes": database_path.stat().st_size,
        "database_used_bytes": block_size * int(size[4]),
        "relationship_state_counts": {
            str(state): int(count) for state, count in states
        },
    }


def _revision() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout.strip()


def _worktree_dirty() -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    return bool(completed.stdout.strip())


if __name__ == "__main__":
    raise SystemExit(main())
