"""Publish compact scenario results with an atomic file replacement.

Migration stages: cross-cutting qualification evidence. Layer: adapter.

See ``docs/plans/end-to-end-trial-and-scenario-qualification.md`` and
``tests/integration/scenarios/test_result_writer.py``.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

from impodo.domain.scenarios import ScenarioRunResult


def write_scenario_result(result: ScenarioRunResult, path: str | Path) -> Path:
    """Atomically write one portable result and return its absolute path."""

    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.is_symlink():
        raise OSError("scenario result path cannot be a symbolic link")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(result.to_json_bytes())
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination
