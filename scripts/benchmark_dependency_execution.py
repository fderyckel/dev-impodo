"""Measure current dependency ordering and execution for representative shapes.

The fixtures are deterministic and contain no customer values. Each measured
shape runs in a fresh process against an in-memory journal and recording Odoo
writer. No Odoo server is contacted and no production behavior is changed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
from threading import Event, Thread
from time import perf_counter
from types import SimpleNamespace
from typing import Any, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

from impodo.application.workspace.execution.service import (
    ExecutionService,
    execution_api_scope,
)
from impodo.domain.execution.models import ExecutionRunStatus
from impodo.domain.execution_snapshot import (
    ExecutionDataset,
    ExecutionRow,
    ExecutionSnapshot,
    FieldIntent,
    RelationshipPlan,
    dependency_ordered_execution_datasets,
    plan_execution_rows,
)
from impodo.domain.shared.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.domain.shared.models import (
    Classification,
    LogicalReference,
    canonical_json_bytes,
)
from impodo.domain.workspace.workbench import OdooConnectionMode, SourceMode


ROOT = Path(__file__).resolve().parents[1]
RESULT_PREFIX = "IMPODO_DEPENDENCY_EXECUTION_JSON="
MIB = 1024 * 1024
HASH = "sha256:" + "1" * 64
TARGET_HASH = "sha256:" + "2" * 64
SHAPES = ("product_unit", "same_dataset_hierarchy", "optional_cycle", "product_bom")


@dataclass(frozen=True, slots=True)
class Fixture:
    """One current-contract execution snapshot and its expected business order."""

    name: str
    snapshot: ExecutionSnapshot
    reviewed_dataset_order: tuple[str, ...]
    expected_dataset_order: tuple[str, ...]
    relationship_edge_count: int


class DependencyBenchmarkError(RuntimeError):
    """Raised when baseline evidence is incomplete or incomparable."""


class _PeakSampler:
    """Sample process RSS without retaining fixture-domain objects."""

    def __init__(self, process, *, interval_seconds: float = 0.005) -> None:
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


class _Journal:
    """Record current service journal traffic without persistence overhead."""

    def __init__(self) -> None:
        self.run = None
        self.rows = {}
        self.start_calls = 0
        self.outcome_calls = 0
        self.finish_calls = 0

    def get_current_run(self, _workspace_id, _snapshot_hash=None):
        if self.run is None or self.run.status is ExecutionRunStatus.RUNNING:
            return None
        return self.run

    def start_run(self, _workspace_id, run, *, actor) -> None:
        del actor
        self.start_calls += 1
        self.run = run
        self.rows = {item.row_id: item for item in run.rows}

    def record_outcomes(self, _workspace_id, _run_id, rows) -> None:
        self.outcome_calls += 1
        self.rows.update({item.row_id: item for item in rows})

    def finish_run(self, _workspace_id, _run_id, status, *, actor):
        del actor
        self.finish_calls += 1
        self.run = replace(
            self.run,
            status=status,
            completed_at=datetime.now(timezone.utc),
            rows=tuple(self.rows[item.row_id] for item in self.run.rows),
        )
        return self.run


class _Executor:
    """Return deterministic receipts and retain exact connector-call evidence."""

    target_hash = TARGET_HASH

    def __init__(self, scope_hash: str) -> None:
        self.scope_hash = scope_hash
        self.lookups: list[tuple[str, tuple[tuple[str, str, Any], ...]]] = []
        self.creates: list[tuple[str, tuple[dict[str, Any], ...]]] = []
        self.loads: list[
            tuple[str, tuple[dict[str, Any], ...], tuple[str, ...]]
        ] = []
        self.updates: list[tuple[str, int, dict[str, Any]]] = []
        self.calls: list[dict[str, object]] = []
        self._next_id = 10_000

    def find_ids(self, model, domain):
        self.lookups.append((model, tuple(domain)))
        self.calls.append(
            {"operation": "lookup", "model": model, "domain": tuple(domain)}
        )
        identifier = self._next_id
        self._next_id += 1
        return (identifier,)

    def create_rows(self, model, values):
        rows = tuple(dict(item) for item in values)
        self.creates.append((model, rows))
        self.calls.append(
            {"operation": "create", "model": model, "rows": len(rows)}
        )
        return self._identifiers(len(rows))

    def load_create_rows(self, model, values, external_ids):
        rows = tuple(dict(item) for item in values)
        self.loads.append((model, rows, tuple(external_ids)))
        self.calls.append(
            {
                "operation": "load_create",
                "model": model,
                "rows": len(rows),
                "fields": [sorted(row) for row in rows],
            }
        )
        return self._identifiers(len(rows))

    def update_row(self, model, record_id, values) -> None:
        self.updates.append((model, int(record_id), dict(values)))
        self.calls.append(
            {
                "operation": "update",
                "model": model,
                "record_id": int(record_id),
                "fields": sorted(values),
            }
        )

    def _identifiers(self, count: int) -> tuple[int, ...]:
        identifiers = tuple(range(self._next_id, self._next_id + count))
        self._next_id += count
        return identifiers


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--small-products", type=int, default=12)
    parser.add_argument("--hierarchy-depth", type=int, default=8)
    parser.add_argument("--bom-products", type=int, default=100)
    parser.add_argument("--bom-headers", type=int, default=25)
    parser.add_argument("--bom-lines", type=int, default=500)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--shape",
        choices=("all", *SHAPES),
        default="all",
    )
    parser.add_argument(
        "--child-shape",
        choices=SHAPES,
        help=argparse.SUPPRESS,
    )
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    _validate_arguments(arguments)
    if arguments.child_shape:
        result = measure_shape(
            arguments.child_shape,
            batch_size=arguments.batch_size,
            small_products=arguments.small_products,
            hierarchy_depth=arguments.hierarchy_depth,
            bom_products=arguments.bom_products,
            bom_headers=arguments.bom_headers,
            bom_lines=arguments.bom_lines,
        )
        print(RESULT_PREFIX + json.dumps(result, separators=(",", ":"), sort_keys=True))
        return 0

    report = _run_parent(arguments)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


def _validate_arguments(arguments: argparse.Namespace) -> None:
    values = (
        arguments.runs,
        arguments.batch_size,
        arguments.small_products,
        arguments.hierarchy_depth,
        arguments.bom_products,
        arguments.bom_headers,
        arguments.bom_lines,
    )
    if min(values) < 1:
        raise DependencyBenchmarkError("Counts, runs, and batch size must be positive")
    if arguments.output is not None and not arguments.output.parent.is_dir():
        raise DependencyBenchmarkError("Benchmark output parent directory is missing")


def _run_parent(arguments: argparse.Namespace) -> dict[str, object]:
    selected = SHAPES if arguments.shape == "all" else (arguments.shape,)
    evidence = {
        shape: tuple(_spawn_child(shape, arguments) for _ in range(arguments.runs))
        for shape in selected
    }
    for shape, runs in evidence.items():
        _require_same_semantics(shape, runs)
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "command": _configuration(arguments),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "result_schema_version": 1,
        "revision": _revision(),
        "shapes": {
            shape: {
                "runs": runs,
                "summary": summarize(runs),
            }
            for shape, runs in evidence.items()
        },
        "worktree_dirty": _worktree_dirty(),
    }


def _configuration(arguments: argparse.Namespace) -> dict[str, int | str]:
    return {
        "batch_size": arguments.batch_size,
        "bom_headers": arguments.bom_headers,
        "bom_lines": arguments.bom_lines,
        "bom_products": arguments.bom_products,
        "hierarchy_depth": arguments.hierarchy_depth,
        "runs": arguments.runs,
        "shape": arguments.shape,
        "small_products": arguments.small_products,
    }


def _spawn_child(shape: str, arguments: argparse.Namespace) -> dict[str, object]:
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
        "--child-shape",
        shape,
        "--batch-size",
        str(arguments.batch_size),
        "--small-products",
        str(arguments.small_products),
        "--hierarchy-depth",
        str(arguments.hierarchy_depth),
        "--bom-products",
        str(arguments.bom_products),
        "--bom-headers",
        str(arguments.bom_headers),
        "--bom-lines",
        str(arguments.bom_lines),
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=300,
    )
    if completed.returncode:
        raise DependencyBenchmarkError(
            f"Dependency benchmark child failed ({shape}):\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    marked = [
        line.removeprefix(RESULT_PREFIX)
        for line in completed.stdout.splitlines()
        if line.startswith(RESULT_PREFIX)
    ]
    if len(marked) != 1:
        raise DependencyBenchmarkError(
            f"Dependency benchmark child emitted invalid evidence ({shape})"
        )
    result = json.loads(marked[0])
    if not isinstance(result, dict):
        raise DependencyBenchmarkError("Dependency benchmark evidence is not an object")
    return result


def measure_shape(
    shape: str,
    *,
    batch_size: int,
    small_products: int = 12,
    hierarchy_depth: int = 8,
    bom_products: int = 100,
    bom_headers: int = 25,
    bom_lines: int = 500,
) -> dict[str, object]:
    """Measure one shape using current snapshot and execution behavior."""

    import psutil

    process = psutil.Process()
    baseline_rss = int(process.memory_info().rss)
    with _PeakSampler(process) as sampler:
        fixture_started = perf_counter()
        fixture = build_fixture(
            shape,
            small_products=small_products,
            hierarchy_depth=hierarchy_depth,
            bom_products=bom_products,
            bom_headers=bom_headers,
            bom_lines=bom_lines,
        )
        fixture_seconds = perf_counter() - fixture_started

        planning_started = perf_counter()
        planned = dependency_ordered_execution_datasets(
            tuple(
                replace(item, sequence=index)
                for index, item in enumerate(
                    _datasets_in_reviewed_order(fixture)
                )
            )
        )
        dataset_planning_seconds = perf_counter() - planning_started
        observed_order = tuple(item.dataset for item in planned)
        if observed_order != fixture.expected_dataset_order:
            raise DependencyBenchmarkError(
                f"{shape} produced unexpected dataset order {observed_order}"
            )

        snapshot_json = fixture.snapshot.to_json()
        ExecutionSnapshot.from_json(snapshot_json)
        service, journal = _service(fixture.snapshot)
        preview_started = perf_counter()
        preview = service.current_preview(fixture.snapshot.workspace_id)
        preview_seconds = perf_counter() - preview_started
        if preview is None or not preview.can_load:
            reason = "missing preview" if preview is None else preview.scope_error
            raise DependencyBenchmarkError(f"{shape} is not loadable: {reason}")

        executor = _Executor(execution_api_scope(fixture.snapshot).semantic_hash)
        load_started = perf_counter()
        run = service.execute(
            fixture.snapshot.workspace_id,
            expected_snapshot_hash=fixture.snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
            batch_rows=batch_size,
        )
        load_seconds = perf_counter() - load_started

    call_counts = _call_counts(executor)
    call_sequence = _call_sequence(executor)
    return {
        "baseline_rss_mib": baseline_rss / MIB,
        "call_counts": call_counts,
        "call_sequence_hash": "sha256:"
        + sha256(canonical_json_bytes(call_sequence)).hexdigest(),
        "committed_rows": run.committed_count,
        "dataset_planning_seconds": dataset_planning_seconds,
        "expected_dataset_order": list(fixture.expected_dataset_order),
        "fixture_build_seconds": fixture_seconds,
        "journal_calls": {
            "finish": journal.finish_calls,
            "outcomes": journal.outcome_calls,
            "start": journal.start_calls,
        },
        "load_seconds": load_seconds,
        "observed_dataset_order": list(observed_order),
        "peak_increment_mib": max(0, sampler.peak_bytes - baseline_rss) / MIB,
        "peak_rss_mib": sampler.peak_bytes / MIB,
        "preview_seconds": preview_seconds,
        "relationship_edge_count": fixture.relationship_edge_count,
        "reviewed_dataset_order": list(fixture.reviewed_dataset_order),
        "row_count": len(fixture.snapshot.rows),
        "run_status": run.status.value,
        "shape": shape,
        "snapshot_semantic_hash": fixture.snapshot.semantic_hash,
        "snapshot_size_bytes": len(snapshot_json.encode("utf-8")),
    }


def summarize(runs: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Keep exact semantic counts and median volatile measurements."""

    if not runs:
        raise DependencyBenchmarkError("At least one dependency run is required")
    timings = (
        "dataset_planning_seconds",
        "fixture_build_seconds",
        "load_seconds",
        "peak_increment_mib",
        "peak_rss_mib",
        "preview_seconds",
    )
    first = runs[0]
    return {
        "call_counts": first["call_counts"],
        "relationship_edge_count": first["relationship_edge_count"],
        "row_count": first["row_count"],
        "run_count": len(runs),
        "snapshot_size_bytes": first["snapshot_size_bytes"],
        **{
            f"median_{name}": statistics.median(float(item[name]) for item in runs)
            for name in timings
        },
    }


def _require_same_semantics(
    shape: str,
    runs: Sequence[Mapping[str, object]],
) -> None:
    if not runs:
        raise DependencyBenchmarkError(f"{shape} has no benchmark runs")
    stable_fields = (
        "call_counts",
        "call_sequence_hash",
        "committed_rows",
        "expected_dataset_order",
        "journal_calls",
        "observed_dataset_order",
        "relationship_edge_count",
        "row_count",
        "run_status",
        "snapshot_semantic_hash",
        "snapshot_size_bytes",
    )
    baseline = {name: runs[0][name] for name in stable_fields}
    if any({name: item[name] for name in stable_fields} != baseline for item in runs[1:]):
        raise DependencyBenchmarkError(f"{shape} changed semantics between runs")


def build_fixture(
    shape: str,
    *,
    small_products: int = 12,
    hierarchy_depth: int = 8,
    bom_products: int = 100,
    bom_headers: int = 25,
    bom_lines: int = 500,
) -> Fixture:
    builders = {
        "product_unit": lambda: _product_unit_fixture(small_products),
        "same_dataset_hierarchy": lambda: _hierarchy_fixture(hierarchy_depth),
        "optional_cycle": _optional_cycle_fixture,
        "product_bom": lambda: _product_bom_fixture(
            products=bom_products,
            bom_headers=bom_headers,
            bom_lines=bom_lines,
        ),
    }
    try:
        return builders[shape]()
    except KeyError as error:
        raise DependencyBenchmarkError(f"Unknown dependency shape: {shape}") from error


def _product_unit_fixture(products: int) -> Fixture:
    datasets = (
        _dataset("products", "product.template", ("uoms",)),
        _dataset("uoms", "uom.uom"),
    )
    rows = [
        _row("products", "product.template", f"P-{index:04d}", (
            _scalar("default_code", f"P-{index:04d}"),
            _relation("uom_id", "uom.uom", "uoms", "UNIT" if index % 2 else "DOZEN"),
        ))
        for index in range(1, products + 1)
    ]
    rows.extend(
        (
            _row("uoms", "uom.uom", "UNIT", (_scalar("name", "Unit"),)),
            _row("uoms", "uom.uom", "DOZEN", (_scalar("name", "Dozen"),)),
        )
    )
    return _fixture("product_unit", datasets, rows, ("uoms", "products"))


def _hierarchy_fixture(depth: int) -> Fixture:
    datasets = (_dataset("categories", "product.category", ("categories",)),)
    rows = []
    for index in range(depth, 0, -1):
        code = f"CAT-{index:04d}"
        fields = [_scalar("name", code)]
        if index > 1:
            fields.append(
                _relation(
                    "parent_id",
                    "product.category",
                    "categories",
                    f"CAT-{index - 1:04d}",
                    defer_on_create=True,
                )
            )
        rows.append(_row("categories", "product.category", code, tuple(fields)))
    return _fixture(
        "same_dataset_hierarchy",
        datasets,
        rows,
        ("categories",),
    )


def _optional_cycle_fixture() -> Fixture:
    datasets = (
        _dataset("first_nodes", "x.first.node", ("second_nodes",)),
        _dataset("second_nodes", "x.second.node", ("first_nodes",)),
    )
    rows = (
        _row(
            "first_nodes",
            "x.first.node",
            "FIRST",
            (
                _scalar("code", "FIRST"),
                _relation(
                    "second_id",
                    "x.second.node",
                    "second_nodes",
                    "SECOND",
                    defer_on_create=True,
                ),
            ),
        ),
        _row(
            "second_nodes",
            "x.second.node",
            "SECOND",
            (
                _scalar("code", "SECOND"),
                _relation(
                    "first_id",
                    "x.first.node",
                    "first_nodes",
                    "FIRST",
                    defer_on_create=True,
                ),
            ),
        ),
    )
    return _fixture(
        "optional_cycle",
        datasets,
        rows,
        ("first_nodes", "second_nodes"),
    )


def _product_bom_fixture(
    *,
    products: int,
    bom_headers: int,
    bom_lines: int,
) -> Fixture:
    datasets = (
        _dataset("bom_lines", "mrp.bom.line", ("boms", "products")),
        _dataset("boms", "mrp.bom", ("products",)),
        _dataset("products", "product.template", ("uoms",)),
        _dataset("uoms", "uom.uom"),
    )
    rows: list[ExecutionRow] = []
    for index in range(1, bom_lines + 1):
        code = f"LINE-{index:06d}"
        bom_code = f"BOM-{((index - 1) % bom_headers) + 1:04d}"
        product_code = f"P-{((index - 1) % products) + 1:05d}"
        rows.append(
            _row(
                "bom_lines",
                "mrp.bom.line",
                code,
                (
                    _scalar("sequence", index),
                    _relation("bom_id", "mrp.bom", "boms", bom_code),
                    _relation(
                        "product_id",
                        "product.template",
                        "products",
                        product_code,
                    ),
                ),
            )
        )
    for index in range(1, bom_headers + 1):
        code = f"BOM-{index:04d}"
        product_code = f"P-{((index - 1) % products) + 1:05d}"
        rows.append(
            _row(
                "boms",
                "mrp.bom",
                code,
                (
                    _scalar("code", code),
                    _relation(
                        "product_tmpl_id",
                        "product.template",
                        "products",
                        product_code,
                    ),
                ),
            )
        )
    for index in range(1, products + 1):
        code = f"P-{index:05d}"
        rows.append(
            _row(
                "products",
                "product.template",
                code,
                (
                    _scalar("default_code", code),
                    _relation(
                        "uom_id",
                        "uom.uom",
                        "uoms",
                        "UNIT" if index % 2 else "DOZEN",
                    ),
                ),
            )
        )
    rows.extend(
        (
            _row("uoms", "uom.uom", "UNIT", (_scalar("name", "Unit"),)),
            _row("uoms", "uom.uom", "DOZEN", (_scalar("name", "Dozen"),)),
        )
    )
    return _fixture(
        "product_bom",
        datasets,
        rows,
        ("uoms", "products", "boms", "bom_lines"),
    )


def _fixture(
    name: str,
    reviewed_datasets: Sequence[ExecutionDataset],
    rows: Sequence[ExecutionRow],
    expected_order: tuple[str, ...],
) -> Fixture:
    reviewed = tuple(
        replace(item, sequence=index)
        for index, item in enumerate(reviewed_datasets)
    )
    ordered = tuple(
        replace(item, sequence=index)
        for index, item in enumerate(dependency_ordered_execution_datasets(reviewed))
    )
    if tuple(item.dataset for item in ordered) != expected_order:
        raise DependencyBenchmarkError(f"{name} fixture dependency order is invalid")
    finalized_rows = tuple(
        _finalize_row(row, ordinal) for ordinal, row in enumerate(rows)
    )
    planned_rows, relationship_plan = plan_execution_rows(
        finalized_rows,
        ordered,
    )
    snapshot = _snapshot(
        name,
        ordered,
        planned_rows,
        relationship_plan=relationship_plan,
    )
    snapshot_json = snapshot.to_json()
    snapshot = ExecutionSnapshot.from_json(snapshot_json)
    return Fixture(
        name=name,
        snapshot=snapshot,
        reviewed_dataset_order=tuple(item.dataset for item in reviewed),
        expected_dataset_order=expected_order,
        relationship_edge_count=snapshot.relationship_plan.edge_count,
    )


def _datasets_in_reviewed_order(fixture: Fixture) -> tuple[ExecutionDataset, ...]:
    by_name = {item.dataset: item for item in fixture.snapshot.datasets}
    return tuple(by_name[name] for name in fixture.reviewed_dataset_order)


def _dataset(
    name: str,
    model: str,
    dependencies: tuple[str, ...] = (),
) -> ExecutionDataset:
    return ExecutionDataset(
        dataset=name,
        target_model=model,
        sequence=0,
        dependencies=dependencies,
        existing_policy="update",
        identity_fields=("code",),
        scope_fields=(),
    )


def _scalar(field: str, value: object) -> FieldIntent:
    return FieldIntent(field=field, action="SET_VALUE", value=value)


def _relation(
    field: str,
    model: str,
    dataset: str,
    key: str,
    *,
    defer_on_create: bool = False,
) -> FieldIntent:
    return FieldIntent(
        field=field,
        action="SET_VALUE",
        value=LogicalReference(origin="incoming", key=(key,), dataset=dataset),
        kind="relation",
        relation_operation="replace",
        related_model=model,
        related_identity_fields=("code",),
        dependency_strength=("deferrable" if defer_on_create else "hard"),
        defer_on_create=defer_on_create,
    )


def _row(
    dataset: str,
    model: str,
    key: str,
    fields: tuple[FieldIntent, ...],
) -> ExecutionRow:
    token = sha256(f"{dataset}:{key}".encode("utf-8")).hexdigest()
    return ExecutionRow(
        row_id="sha256:" + token,
        dataset=dataset,
        source_row=0,
        source_trace_id="sha256:" + sha256(f"trace:{token}".encode()).hexdigest(),
        source_identity=(key,),
        target_model=model,
        business_identity=(key,),
        business_scope=(),
        disposition=Classification.CREATE.value,
        target_match_count=0,
        proposed_external_id=f"impodo_baseline.{dataset}_{token[:24]}",
        fields=fields,
    )


def _finalize_row(row: ExecutionRow, ordinal: int) -> ExecutionRow:
    provisional = replace(row, source_row=ordinal + 2, row_hash="")
    row_hash = "sha256:" + sha256(
        canonical_json_bytes(provisional.portable_dict(include_hash=False))
    ).hexdigest()
    return replace(provisional, row_hash=row_hash)


def _snapshot(
    name: str,
    datasets: tuple[ExecutionDataset, ...],
    rows: tuple[ExecutionRow, ...],
    *,
    relationship_plan: RelationshipPlan,
) -> ExecutionSnapshot:
    def identity(label: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"impodo:{name}:{label}"))

    counts = {item.value: 0 for item in Classification}
    counts[Classification.CREATE.value] = len(rows)
    root_hash = "sha256:" + sha256(
        canonical_json_bytes([row.row_hash for row in rows])
    ).hexdigest()
    return ExecutionSnapshot(
        workspace_id=identity("workspace"),
        preflight_run_id=identity("preflight"),
        mapping_id=identity("mapping"),
        mapping_version=1,
        mapping_content_hash=HASH,
        compiled_plan_hash=HASH,
        staging_run_id=identity("staging"),
        staging_content_hash=HASH,
        quality_run_id=identity("quality"),
        quality_content_hash=HASH,
        normalization_run_id=identity("normalization"),
        normalization_content_hash=HASH,
        normalization_lifecycle_version=1,
        eligible_dataset_hash=HASH,
        frozen_input_hash=HASH,
        preflight_result_hash=HASH,
        metadata_snapshot_hash=HASH,
        record_snapshot_hash=HASH,
        target_hash=TARGET_HASH,
        target_database="odoo19_disposable_baseline",
        target_odoo_version="19.0",
        target_snapshot_at="2026-08-28T00:00:00+00:00",
        target_module_versions={"base": "19.0.1.0"},
        datasets=datasets,
        counts=counts,
        rows=rows,
        root_hash=root_hash,
        relationship_plan=relationship_plan,
    )


def _service(snapshot: ExecutionSnapshot) -> tuple[ExecutionService, _Journal]:
    journal = _Journal()
    workspace = SimpleNamespace(
        workspace_id=snapshot.workspace_id,
        odoo_connection_mode=OdooConnectionMode.REMOTE,
        source_mode=SourceMode.FILE,
    )
    service = ExecutionService(
        SimpleNamespace(get=lambda _workspace_id: workspace),
        SimpleNamespace(current_execution_snapshot=lambda _workspace_id: snapshot),
        journal,
        CapabilityAuthorizationPolicy(),
    )
    return service, journal


def _call_counts(executor: _Executor) -> dict[str, int]:
    result = {
        "create": len(executor.creates),
        "load_create": len(executor.loads),
        "lookup": len(executor.lookups),
        "relationship_patch": len(executor.updates),
        "update": len(executor.updates),
    }
    result["total"] = sum(
        result[name] for name in ("create", "load_create", "lookup", "update")
    )
    return result


def _call_sequence(executor: _Executor) -> tuple[dict[str, object], ...]:
    return tuple(executor.calls)


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
    try:
        raise SystemExit(main())
    except DependencyBenchmarkError as error:
        print(f"Dependency benchmark failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
