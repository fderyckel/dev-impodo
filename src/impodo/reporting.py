"""Write deterministic preflight evidence and its Excel review projection.

`models.PreflightResult` is the canonical decision source. This module writes
that result as JSON first, then invokes the packaged JavaScript renderer to
build a business-facing workbook from the JSON file. The workbook is a
projection and never feeds conclusions back into the engine.
"""

from __future__ import annotations

import importlib.resources
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from .models import PreflightResult, canonical_json_bytes


MANIFEST_NAME = "impodo_preflight_manifest.json"
WORKBOOK_NAME = "impodo_preflight_report.xlsx"


class ReportGenerationError(RuntimeError):
    """Raised when canonical report artifacts cannot be generated safely."""


def write_preflight_outputs(
    result: PreflightResult,
    output_directory: str | Path,
    *,
    preview_directory: str | Path | None = None,
) -> tuple[Path, Path]:
    """Write the canonical manifest and its Excel review workbook.

    The manifest uses a `.partial` file followed by an atomic same-directory
    replace. Workbook construction runs only after the manifest is complete.

    Returns:
        `(manifest_path, workbook_path)` for the two required artifacts.
    """

    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / MANIFEST_NAME
    workbook_path = output / WORKBOOK_NAME
    manifest = result.to_portable_dict()
    temporary_manifest = manifest_path.with_suffix(".json.partial")
    temporary_manifest.write_bytes(canonical_json_bytes(manifest) + b"\n")
    temporary_manifest.replace(manifest_path)

    _build_workbook(
        manifest_path,
        workbook_path,
        preview_directory=preview_directory,
    )
    return manifest_path, workbook_path


def write_review_workbook(
    manifest_path: str | Path,
    workbook_path: str | Path,
    *,
    preview_directory: str | Path | None = None,
) -> Path:
    """Build the Excel review projection from an existing manifest."""

    manifest = Path(manifest_path)
    workbook = Path(workbook_path)
    if not manifest.is_file():
        raise ReportGenerationError("The readiness manifest does not exist")
    workbook.parent.mkdir(parents=True, exist_ok=True)
    _build_workbook(
        manifest,
        workbook,
        preview_directory=preview_directory,
    )
    return workbook


def _build_workbook(
    manifest_path: Path,
    workbook_path: Path,
    *,
    preview_directory: str | Path | None,
) -> None:
    """Run the vendored workbook renderer in an isolated temporary directory.

    Node.js and the artifact-tool modules must be supplied explicitly through
    the documented environment or local project installation. Output and
    errors are captured; only a bounded error tail is included in failures.
    """

    node_binary = os.environ.get("IMPODO_NODE_BINARY") or shutil.which("node")
    if not node_binary:
        raise ReportGenerationError(
            "Node.js is required to create the Excel review workbook; "
            "set IMPODO_NODE_BINARY"
        )
    supplied_modules = os.environ.get("IMPODO_ARTIFACT_TOOL_NODE_MODULES")
    project_modules = Path.cwd() / "node_modules"
    if supplied_modules:
        node_modules = Path(supplied_modules)
    elif project_modules.exists():
        node_modules = project_modules
    else:
        raise ReportGenerationError(
            "@oai/artifact-tool runtime is unavailable; set "
            "IMPODO_ARTIFACT_TOOL_NODE_MODULES"
        )
    if not node_modules.exists():
        raise ReportGenerationError(
            f"artifact-tool node_modules path does not exist: {node_modules}"
        )

    manifest_path = manifest_path.resolve()
    workbook_path = workbook_path.resolve()
    preview = Path(preview_directory).resolve() if preview_directory else None
    if preview:
        preview.mkdir(parents=True, exist_ok=True)

    resource = importlib.resources.files("impodo").joinpath(
        "resources/build_review_workbook.mjs"
    )
    with tempfile.TemporaryDirectory(
        prefix=".impodo-report-",
        dir=workbook_path.parent,
    ) as temporary_directory:
        temporary = Path(temporary_directory)
        runner = temporary / "build_review_workbook.mjs"
        runner.write_text(resource.read_text(encoding="utf-8"), encoding="utf-8")
        (temporary / "node_modules").symlink_to(node_modules, target_is_directory=True)
        command = [
            str(node_binary),
            str(runner),
            str(manifest_path),
            str(workbook_path),
        ]
        if preview:
            command.append(str(preview))
        completed = subprocess.run(
            command,
            cwd=temporary,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            safe_error = (completed.stderr or completed.stdout)[-4000:]
            raise ReportGenerationError(
                "Excel review workbook generation failed: " + safe_error
            )
        if not workbook_path.exists() or workbook_path.stat().st_size == 0:
            raise ReportGenerationError("Excel review workbook was not created")


def read_manifest(path: str | Path) -> dict[str, Any]:
    """Load a previously generated canonical JSON manifest."""

    return json.loads(Path(path).read_text(encoding="utf-8"))
