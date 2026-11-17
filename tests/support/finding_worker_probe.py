"""Observe real preparation workers after their native runtime is configured."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def finding_worker(*args):
    """Delegate to production preparation with isolated scratch and SQL counts."""
    _run(*args, wait_for_cancel=False)


def finding_worker_waiting_for_cancel(*args):
    """Wait at the normalization notification for the parent's normal cancel API."""
    _run(*args, wait_for_cancel=True)


def _run(
    root, workspace_id, workspace, build, actor, events, cancel, *, wait_for_cancel
):
    from impodo.application.shared.preparation_runtime import (
        configure_preparation_runtime,
    )

    configure_preparation_runtime()

    from impodo.web.composition.preparation_job_manager import _run_preparation_worker
    from tests.support.database_probe import count_duckdb_statements

    evidence_root = Path(root).parent / "worker-evidence"
    scratch_root = evidence_root / f"scratch-{os.getpid()}"
    scratch_root.mkdir(parents=True)
    tempfile.tempdir = str(scratch_root)
    observed = {"active_finding_stores_before_normalization": 0}

    class Events:
        def put(self, event):
            if event[:2] == ("progress", "NORMALIZING"):
                observed["active_finding_stores_before_normalization"] = len(
                    tuple(scratch_root.glob("impodo-quality-*"))
                )
                events.put(event)
                if wait_for_cancel and not cancel.wait(timeout=30):
                    raise RuntimeError(
                        "Parent did not request phase-boundary cancellation"
                    )
            else:
                events.put(event)

    try:
        with count_duckdb_statements() as statements:
            _run_preparation_worker(
                root, workspace_id, workspace, build, actor, Events(), cancel
            )
    finally:
        observed["sql_statements"] = len(statements)
        observed["remaining_finding_stores"] = len(
            tuple(scratch_root.glob("impodo-quality-*"))
        )
        (evidence_root / f"worker-{os.getpid()}.json").write_text(
            json.dumps(observed), encoding="utf-8"
        )
