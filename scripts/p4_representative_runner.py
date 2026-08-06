"""Run the opt-in P4 representative migration against disposable Odoo.

The runner generates deterministic sanitized rows, captures the live read-only
preflight, executes the same practical writer and reconciliation services used
by the browser, and captures Odoo again to prove the repeat is all unchanged.
It refuses non-loopback URLs and database names outside the dedicated
``impodo_p4_`` namespace. Credentials are read from a private file and are
never printed or included in the result.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from impodo.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.application.execution_service import ExecutionService
from impodo.application.reconciliation_service import ReconciliationService
from impodo.connectors import (
    Json2Config,
    Json2ReadConnector,
    bind_snapshot_hashes,
)
from impodo.domain.compiler import compile_profile_document
from impodo.domain.execution import ExecutionRunStatus
from impodo.domain.execution_snapshot import build_execution_snapshot
from impodo.domain.preflight.frozen_input import FrozenPreflightInput
from impodo.engine import PreflightEngine
from impodo.models import Classification
from impodo.odoo_readback import Json2ReadbackReader
from impodo.odoo_writer import Json2WriteExecutor
from impodo.planner import plan_metadata_requests, plan_record_requests
from impodo.profile import load_profile
from impodo.projects import OdooConnectionMode
from impodo.source import PreparedBundle, prepare_sources


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles" / "examples" / "p4_representative.yaml"
TOTAL_ROWS = 150
EXPECTED_FIRST = {
    "CREATE": 125,
    "UPDATE": 20,
    "UNCHANGED": 5,
    "AMBIGUOUS": 0,
    "BLOCKED": 0,
}
EXPECTED_REPEAT = {
    "CREATE": 0,
    "UPDATE": 0,
    "UNCHANGED": TOTAL_ROWS,
    "AMBIGUOUS": 0,
    "BLOCKED": 0,
}


class _Journal:
    """Small live-run journal seam; durable behavior is covered separately."""

    def __init__(self) -> None:
        self.run = None
        self.rows = {}

    def get_current_run(self, project_id, snapshot_hash=None):
        if self.run is None or self.run.project_id != project_id:
            return None
        if snapshot_hash is not None and self.run.snapshot_hash != snapshot_hash:
            return None
        return self.run

    def get_run(self, project_id, run_id):
        if self.run is None:
            return None
        return self.run if (self.run.project_id, self.run.run_id) == (project_id, run_id) else None

    def start_run(self, project_id, run, *, actor):
        del actor
        if project_id != run.project_id or self.run is not None:
            raise RuntimeError("P4 journal received an invalid start")
        self.run = run
        self.rows = {item.row_id: item for item in run.rows}

    def record_outcomes(self, project_id, run_id, rows):
        if self.run is None or (project_id, run_id) != (
            self.run.project_id,
            self.run.run_id,
        ):
            raise RuntimeError("P4 journal received an invalid outcome")
        self.rows.update({item.row_id: item for item in rows})

    def finish_run(self, project_id, run_id, status, *, actor):
        del actor
        if self.run is None or (project_id, run_id) != (
            self.run.project_id,
            self.run.run_id,
        ):
            raise RuntimeError("P4 journal received an invalid finish")
        self.run = replace(
            self.run,
            status=status,
            completed_at=datetime.now(timezone.utc),
            rows=tuple(self.rows[item.row_id] for item in self.run.rows),
        )
        return self.run


class _Results:
    def __init__(self) -> None:
        self.report = None

    def get_current(self, project_id, execution_run_id=None):
        if self.report is None or self.report.project_id != project_id:
            return None
        if execution_run_id and self.report.execution_run_id != execution_run_id:
            return None
        return self.report

    def publish(self, project_id, report, *, actor):
        del actor
        if report.project_id != project_id or self.report is not None:
            raise RuntimeError("P4 result publication is invalid")
        self.report = report


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8069")
    parser.add_argument("--database", required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    return parser.parse_args()


def _write_sources(directory: Path) -> None:
    with (directory / "categories.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("category_code", "name"))
        writer.writerows(
            (f"CAT-{index:02d}", f"Impodo P4 Category {index:02d}")
            for index in range(1, 11)
        )
    with (directory / "contacts.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("reference", "name", "email", "phone", "city"))
        writer.writerows(
            (
                f"IMPODO-P4-C-{index:03d}",
                f"Representative Contact {index:03d}",
                f"contact{index:03d}@example.invalid",
                f"+352 2600 {index:04d}",
                "Luxembourg",
            )
            for index in range(1, 101)
        )
    with (directory / "products.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("product_code", "name", "list_price", "active", "category_code"))
        writer.writerows(
            (
                f"IMPODO-P4-P-{index:03d}",
                f"Representative Product {index:03d}",
                f"{Decimal('10.00') + Decimal(index) / Decimal('4'):.2f}",
                "true",
                f"CAT-{((index - 1) % 10) + 1:02d}",
            )
            for index in range(1, 41)
        )


def _capture(config: Json2Config, plan, prepared: PreparedBundle):
    connector = Json2ReadConnector(config)
    metadata, records = bind_snapshot_hashes(
        connector.get_model_metadata(plan_metadata_requests(plan)),
        connector.get_records(plan_record_requests(plan, prepared.records)),
    )
    return PreflightEngine().run(plan, prepared, metadata, records), records


def _seed_existing(executor: Json2WriteExecutor) -> None:
    category_ids = executor.create_rows(
        "product.category",
        tuple({"name": f"Impodo P4 Category {index:02d}"} for index in range(1, 6)),
    )
    executor.create_rows(
        "res.partner",
        tuple(
            {
                "ref": f"IMPODO-P4-C-{index:03d}",
                "name": f"Old Contact {index:03d}",
                "email": f"old{index:03d}@example.invalid",
                "phone": f"+352 2700 {index:04d}",
                "city": "Esch-sur-Alzette",
            }
            for index in range(1, 11)
        ),
    )
    executor.create_rows(
        "product.template",
        tuple(
            {
                "default_code": f"IMPODO-P4-P-{index:03d}",
                "name": f"Old Product {index:03d}",
                "list_price": float(Decimal("5.00") + Decimal(index) / 10),
                "active": True,
                "categ_id": category_ids[(index - 1) % len(category_ids)],
            }
            for index in range(1, 11)
        ),
    )


def _frozen(project_id: str, plan, prepared: PreparedBundle) -> FrozenPreflightInput:
    binding_hash = plan.semantic_hash
    return FrozenPreflightInput(
        project_id=project_id,
        revision=SimpleNamespace(
            mapping_id=str(uuid4()),
            version=1,
            definition=SimpleNamespace(content_hash=binding_hash),
        ),
        staging=SimpleNamespace(run_id=str(uuid4()), content_hash=binding_hash),
        quality=SimpleNamespace(
            run_id=str(uuid4()),
            content_hash=binding_hash,
            effective_dataset_run_id=None,
            effective_dataset_hash=binding_hash,
        ),
        normalization=SimpleNamespace(
            run_id=str(uuid4()),
            content_hash=binding_hash,
            lifecycle_version=1,
            eligible_dataset_hash=binding_hash,
        ),
        plan=plan,
        prepared=prepared,
        dataset_labels={item.name: item.name for item in plan.datasets},
        source_field_labels={},
        eligible_row_ids=tuple(item.source_trace_id for item in prepared.records),
    )


def _assert_counts(actual: dict[str, int], expected: dict[str, int], label: str) -> None:
    normalized = {name: int(actual.get(name, 0)) for name in expected}
    if normalized != expected:
        raise RuntimeError(f"{label} counts are {normalized}, expected {expected}")


def _target_counts(records) -> dict[str, int]:
    return {
        model: len(records.records.get(model, ()))
        for model in ("product.category", "res.partner", "product.template")
    }


def main() -> int:
    args = _arguments()
    if args.base_url.rstrip("/") != "http://127.0.0.1:8069":
        raise SystemExit("P4 accepts only the loopback rehearsal URL")
    if not args.database.startswith("impodo_p4_"):
        raise SystemExit("P4 accepts only an impodo_p4_ disposable database")
    api_key = args.api_key_file.read_text("utf-8").strip()
    if not api_key:
        raise SystemExit("The P4 API key file is empty")
    config = Json2Config(
        base_url=args.base_url,
        database=args.database,
        api_key=api_key,
        connection_mode="LOCAL",
        page_size=100,
        retries=0,
        relevant_modules=("base", "contacts", "product"),
    )
    executor = Json2WriteExecutor(config)
    reader = Json2ReadbackReader(config)
    with tempfile.TemporaryDirectory(prefix="impodo-p4-") as directory_name:
        directory = Path(directory_name)
        _write_sources(directory)
        plan = compile_profile_document(load_profile(PROFILE))
        prepared = prepare_sources(plan, directory)
        if len(prepared.records) != TOTAL_ROWS or prepared.issues:
            raise RuntimeError("The generated P4 source package is invalid")

        baseline, baseline_records = _capture(config, plan, prepared)
        if int(baseline.counts.get("CREATE", 0)) == TOTAL_ROWS:
            _seed_existing(executor)
        elif {
            name: int(baseline.counts.get(name, 0)) for name in EXPECTED_REPEAT
        } == EXPECTED_REPEAT:
            target_counts = _target_counts(baseline_records)
            expected_target_counts = {
                "product.category": 10,
                "res.partner": 100,
                "product.template": 40,
            }
            if target_counts != expected_target_counts:
                raise RuntimeError(
                    f"P4 target counts are {target_counts}, expected "
                    f"{expected_target_counts}"
                )
            print(
                json.dumps(
                    {
                        "database": args.database,
                        "source_rows": TOTAL_ROWS,
                        "status": "already_migrated_and_unchanged",
                        "repeat_preview": EXPECTED_REPEAT,
                        "target_rows": target_counts,
                    },
                    sort_keys=True,
                )
            )
            return 0
        elif baseline.counts != EXPECTED_FIRST:
            raise RuntimeError(
                "The disposable target is neither empty nor at the expected P4 seed state"
            )

        first, _first_records = _capture(config, plan, prepared)
        _assert_counts(first.counts, EXPECTED_FIRST, "first preview")
        if any(
            decision.classification
            in {Classification.BLOCKED, Classification.AMBIGUOUS}
            for decision in first.decisions
        ):
            raise RuntimeError("The first P4 preview contains unsafe rows")

        project_id = str(uuid4())
        frozen = _frozen(project_id, plan, prepared)
        snapshot = build_execution_snapshot(
            preflight_run_id=str(uuid4()),
            frozen=frozen,
            result=first,
        )
        journal = _Journal()
        preflight = SimpleNamespace(
            current_execution_snapshot=lambda _project_id: snapshot,
            execution_snapshot=lambda _project_id, _run_id: snapshot,
        )
        project = SimpleNamespace(
            project_id=project_id,
            odoo_connection_mode=OdooConnectionMode.LOCAL,
        )
        authorization = CapabilityAuthorizationPolicy()
        execution = ExecutionService(
            SimpleNamespace(get=lambda _project_id: project),
            preflight,
            journal,
            authorization,
        )
        run = execution.execute(
            project_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )
        if run.status is not ExecutionRunStatus.COMPLETED or run.committed_count != 145:
            raise RuntimeError("The P4 execution did not commit every planned write")

        reconciliation = ReconciliationService(
            preflight,
            journal,
            _Results(),
            authorization,
        ).reconcile(
            project_id,
            expected_execution_run_id=run.run_id,
            reader=reader,
            actor=LOCAL_ACTOR,
        )
        if reconciliation.fallout_count or reconciliation.verified_count != TOTAL_ROWS:
            raise RuntimeError("The P4 read-back did not verify every row")

        repeated, repeated_records = _capture(config, plan, prepared)
        _assert_counts(repeated.counts, EXPECTED_REPEAT, "repeat preview")
        target_counts = _target_counts(repeated_records)
        expected_target_counts = {
            "product.category": 10,
            "res.partner": 100,
            "product.template": 40,
        }
        if target_counts != expected_target_counts:
            raise RuntimeError(
                f"P4 target counts are {target_counts}, expected {expected_target_counts}"
            )

        print(
            json.dumps(
                {
                    "database": args.database,
                    "source_rows": TOTAL_ROWS,
                    "first_preview": EXPECTED_FIRST,
                    "execution": {
                        "committed": run.committed_count,
                        "failed": run.failed_count,
                        "unknown": run.unknown_count,
                    },
                    "readback": {
                        "verified": reconciliation.verified_count,
                        "fallout": reconciliation.fallout_count,
                        "unknown": reconciliation.unknown_count,
                    },
                    "repeat_preview": EXPECTED_REPEAT,
                    "target_rows": target_counts,
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
