"""Portable JSON and business-review workbook output."""

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


MANIFEST_NAME = "uc_preflight_manifest.json"
WORKBOOK_NAME = "uc_preflight_report.xlsx"


class ReportGenerationError(RuntimeError):
    pass


def write_preflight_outputs(
    result: PreflightResult,
    output_directory: str | Path,
    *,
    preview_directory: str | Path | None = None,
) -> tuple[Path, Path]:
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


def _build_workbook(
    manifest_path: Path,
    workbook_path: Path,
    *,
    preview_directory: str | Path | None,
) -> None:
    node_binary = os.environ.get("UC_NODE_BINARY") or shutil.which("node")
    if not node_binary:
        raise ReportGenerationError(
            "Node.js is required to create the Excel review workbook; "
            "set UC_NODE_BINARY"
        )
    supplied_modules = os.environ.get("UC_ARTIFACT_TOOL_NODE_MODULES")
    project_modules = Path.cwd() / "node_modules"
    if supplied_modules:
        node_modules = Path(supplied_modules)
    elif project_modules.exists():
        node_modules = project_modules
    else:
        raise ReportGenerationError(
            "@oai/artifact-tool runtime is unavailable; set "
            "UC_ARTIFACT_TOOL_NODE_MODULES"
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

    resource = importlib.resources.files("uc_migration_profiler").joinpath(
        "resources/build_review_workbook.mjs"
    )
    with tempfile.TemporaryDirectory(
        prefix=".uc-report-",
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
    return json.loads(Path(path).read_text(encoding="utf-8"))
