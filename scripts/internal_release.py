"""Build a traceable internal Impodo release from a clean Git revision."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tomllib
from typing import Any
import zipfile


ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = ROOT / "requirements.windows-py312.lock"
SECRETS_BASELINE = ROOT / ".secrets.baseline"
DEFAULT_OUTPUT_ROOT = ROOT / "dist" / "internal"
WORK_ROOT = ROOT / "var" / "internal-release-work"
REQUIRED_PYTHON = (3, 12)
REQUIRED_RELEASE_TOOLS = {
    "build": "1.5.0",
    "cyclonedx-bom": "7.3.1",
    "detect-secrets": "1.5.0",
    "pip-audit": "2.10.1",
}


class ReleaseGateError(RuntimeError):
    """Raised when a security or reproducibility promotion gate fails."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Parent directory for the immutable release bundle.",
    )
    parser.add_argument(
        "--validate-lock-only",
        action="store_true",
        help="Validate the checked-in dependency lock without building.",
    )
    arguments = parser.parse_args(argv)

    try:
        _require_release_python()
        _validate_lock(LOCK_FILE)
        if arguments.validate_lock_only:
            print(f"Validated {LOCK_FILE.name} for Python 3.12 internal releases.")
            return 0
        bundle = build_internal_release(arguments.output_root)
    except ReleaseGateError as error:
        print(f"Internal release refused: {error}", file=sys.stderr)
        return 2

    print(f"Internal release ready: {bundle}")
    return 0


def build_internal_release(output_root: Path) -> Path:
    """Run every promotion gate and publish one immutable release bundle."""

    _require_release_tools()
    _require_locked_runtime_environment(LOCK_FILE)
    revision = _git("rev-parse", "HEAD")
    short_revision = _git("rev-parse", "--short=12", "HEAD")
    _require_clean_worktree()
    version = _project_version()
    release_id = f"impodo-{version}-{short_revision}"

    destination_root = output_root.expanduser().resolve()
    destination = destination_root / release_id
    if destination.exists():
        raise ReleaseGateError(f"release bundle already exists: {destination}")

    work = (WORK_ROOT / release_id).resolve()
    _require_relative_to(work, WORK_ROOT.resolve(), "release work directory")
    if work.exists():
        raise ReleaseGateError(
            f"previous release work still exists; inspect it before retrying: {work}"
        )
    bundle = work / "bundle"
    runtime = work / "runtime"
    source = work / "source"
    temporary = work / "temp"
    bundle.mkdir(parents=True)
    temporary.mkdir()
    _materialize_source_revision(revision, source, work)
    (source / ".tmp").mkdir()
    release_lock = source / LOCK_FILE.name
    _validate_lock(release_lock)

    command_environment = os.environ.copy()
    command_environment["TEMP"] = str(temporary)
    command_environment["TMP"] = str(temporary)
    existing_python_path = command_environment.get("PYTHONPATH")
    source_python_path = str(source / "src")
    command_environment["PYTHONPATH"] = (
        source_python_path
        if not existing_python_path
        else source_python_path + os.pathsep + existing_python_path
    )

    secret_report = _run_secret_gate(command_environment, source)
    _write_json(bundle / "secret-scan.json", secret_report)

    tests_output = _run_tests(command_environment, source)
    (bundle / "tests.txt").write_text(tests_output, encoding="utf-8", newline="\n")

    _run(
        sys.executable,
        "-m",
        "build",
        "--wheel",
        "--outdir",
        str(bundle),
        str(source),
        cwd=source,
        environment=command_environment,
    )
    wheels = tuple(bundle.glob("impodo-*.whl"))
    if len(wheels) != 1:
        raise ReleaseGateError("the build did not produce exactly one Impodo wheel")
    wheel = wheels[0]
    _validate_wheel_contents(wheel)

    _run(
        sys.executable,
        "-m",
        "venv",
        str(runtime),
        environment=command_environment,
    )
    runtime_python = runtime / "Scripts" / "python.exe"
    if not runtime_python.is_file():
        raise ReleaseGateError("the clean Windows runtime environment was not created")
    _run(
        str(runtime_python),
        "-m",
        "pip",
        "install",
        "--require-hashes",
        "--only-binary=:all:",
        "--requirement",
        str(release_lock),
        environment=command_environment,
    )
    _run(
        str(runtime_python),
        "-m",
        "pip",
        "install",
        "--no-deps",
        str(wheel),
        environment=command_environment,
    )
    if not (runtime / "Scripts" / "impodo.exe").is_file():
        raise ReleaseGateError("the clean runtime did not install impodo.exe")
    runtime_cli = runtime / "Scripts" / "impodo-cli.exe"
    if not runtime_cli.is_file():
        raise ReleaseGateError("the clean runtime did not install impodo-cli.exe")
    _run(
        str(runtime_cli),
        "--help",
        capture=True,
        environment=command_environment,
    )

    audit_path = bundle / "dependency-audit.json"
    _run(
        str(_tool("pip-audit")),
        "--requirement",
        str(release_lock),
        "--require-hashes",
        "--disable-pip",
        "--strict",
        "--progress-spinner=off",
        "--format=json",
        f"--output={audit_path}",
        environment=command_environment,
    )

    sbom_path = bundle / "sbom.cdx.json"
    _run(
        str(_tool("cyclonedx-py")),
        "environment",
        str(runtime_python),
        "--pyproject",
        str(source / "pyproject.toml"),
        "--output-reproducible",
        "--output-format=JSON",
        "--validate",
        f"--output-file={sbom_path}",
        environment=command_environment,
    )

    shutil.copy2(source / "scripts" / "install-internal-release.ps1", bundle)
    copied_lock = bundle / LOCK_FILE.name
    shutil.copy2(release_lock, copied_lock)
    manifest = _release_manifest(
        release_id=release_id,
        version=version,
        revision=revision,
        artifacts=tuple(
            path
            for path in bundle.iterdir()
            if path.name != "release-manifest.json"
        ),
    )
    _write_json(bundle / "release-manifest.json", manifest)

    destination_root.mkdir(parents=True, exist_ok=True)
    shutil.move(str(bundle), str(destination))
    _safe_remove_work_directory(work)
    return destination


def _require_release_python() -> None:
    if sys.version_info[:2] != REQUIRED_PYTHON:
        raise ReleaseGateError(
            "internal releases must be built with an approved Python 3.12 runtime"
        )
    if sys.platform != "win32":
        raise ReleaseGateError(
            "requirements.windows-py312.lock is valid only for Windows releases"
        )


def _require_release_tools() -> None:
    mismatches: list[str] = []
    for distribution, expected in REQUIRED_RELEASE_TOOLS.items():
        try:
            actual = package_version(distribution)
        except PackageNotFoundError:
            actual = "missing"
        if actual != expected:
            mismatches.append(f"{distribution}={actual} (expected {expected})")
    if mismatches:
        raise ReleaseGateError(
            "the pinned release toolchain is not installed: " + ", ".join(mismatches)
        )


def _require_locked_runtime_environment(lock: Path) -> None:
    mismatches: list[str] = []
    for line in lock.read_text(encoding="utf-8").splitlines():
        if not line or line[0].isspace() or line.startswith(("#", "--")):
            continue
        distribution, expected = line.rstrip(" \\").split("==", 1)
        try:
            actual = package_version(distribution)
        except PackageNotFoundError:
            actual = "missing"
        if actual != expected:
            mismatches.append(
                f"{distribution}={actual} (locked {expected})"
            )
    if mismatches:
        raise ReleaseGateError(
            "the release environment does not match the runtime lock; install "
            "requirements.windows-py312.lock first: " + ", ".join(mismatches)
        )


def _validate_lock(path: Path) -> None:
    if not path.is_file():
        raise ReleaseGateError(f"dependency lock is missing: {path.name}")
    text = path.read_text(encoding="utf-8")
    if "autogenerated by pip-compile with Python 3.12" not in text:
        raise ReleaseGateError("dependency lock does not declare its Python 3.12 origin")
    if "--only-binary :all:" not in text:
        raise ReleaseGateError("dependency lock does not forbid source distributions")

    package_count = 0
    current_requirement: list[str] = []
    for line in (*text.splitlines(), ""):
        if line and not line[0].isspace() and not line.startswith(("#", "--")):
            if current_requirement:
                _validate_locked_requirement(current_requirement)
            current_requirement = [line]
            package_count += 1
        elif current_requirement:
            current_requirement.append(line)
    if current_requirement:
        _validate_locked_requirement(current_requirement)
    if package_count < 10:
        raise ReleaseGateError("dependency lock is unexpectedly incomplete")


def _validate_locked_requirement(lines: list[str]) -> None:
    declaration = lines[0]
    if "==" not in declaration:
        raise ReleaseGateError(f"dependency is not exactly pinned: {declaration}")
    if not any("--hash=sha256:" in line for line in lines):
        raise ReleaseGateError(f"dependency has no SHA-256 hash: {declaration}")


def _require_clean_worktree() -> None:
    status = _git("status", "--porcelain", "--untracked-files=all")
    if status:
        raise ReleaseGateError(
            "the Git worktree is not clean; commit intentional changes before promotion"
        )


def _run_tests(environment: dict[str, str], source: Path = ROOT) -> str:
    completed = _run(
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-v",
        capture=True,
        cwd=source,
        environment=environment,
    )
    return (completed.stdout or "") + (completed.stderr or "")


def _run_secret_gate(
    environment: dict[str, str],
    source: Path = ROOT,
) -> dict[str, Any]:
    baseline_path = source / SECRETS_BASELINE.name
    if not baseline_path.is_file():
        raise ReleaseGateError("reviewed .secrets.baseline is missing")
    completed = _run(
        str(_tool("detect-secrets")),
        "scan",
        "--baseline",
        str(baseline_path),
        "--force-use-all-plugins",
        "--no-verify",
        ".",
        capture=True,
        cwd=source,
        environment=environment,
    )
    try:
        scan = json.loads(completed.stdout or "{}")
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ReleaseGateError(f"secret scanner returned invalid JSON: {error}") from error
    unexpected = _unexpected_secret_candidates(scan, baseline)
    if unexpected:
        locations = ", ".join(
            f"{candidate['filename']}:{candidate['line_number']}"
            for candidate in unexpected
        )
        raise ReleaseGateError(
            f"secret scan found unreviewed candidates: {locations}"
        )
    candidate_count = sum(
        len(candidates) for candidates in scan.get("results", {}).values()
    )
    return {
        "tool": "detect-secrets",
        "status": "passed",
        "candidate_count": candidate_count,
        "reviewed_baseline_sha256": _sha256(baseline_path),
    }


def _materialize_source_revision(revision: str, source: Path, work: Path) -> None:
    """Extract only Git-tracked bytes for the exact revision being promoted."""

    archive = work / "source.zip"
    _run(
        "git",
        "archive",
        "--format=zip",
        f"--output={archive}",
        revision,
        capture=True,
    )
    try:
        shutil.unpack_archive(archive, source)
    except (OSError, shutil.ReadError) as error:
        raise ReleaseGateError(
            f"could not materialize the clean source revision: {error}"
        ) from error
    finally:
        archive.unlink(missing_ok=True)


def _validate_wheel_contents(wheel: Path) -> None:
    """Reject stale or unrelated packages injected by a local build directory."""

    try:
        with zipfile.ZipFile(wheel) as archive:
            names = [item.filename for item in archive.infolist() if item.filename]
    except (OSError, zipfile.BadZipFile) as error:
        raise ReleaseGateError(f"the Impodo wheel is unreadable: {error}") from error

    if len(names) != len(set(names)):
        raise ReleaseGateError("the Impodo wheel contains duplicate paths")

    unexpected: list[str] = []
    for name in names:
        path = PurePosixPath(name)
        if not path.parts or path.is_absolute() or ".." in path.parts:
            unexpected.append(name)
            continue
        top_level = path.parts[0]
        if top_level == "impodo" or re.fullmatch(
            r"impodo-[^/]+\.dist-info",
            top_level,
        ):
            continue
        unexpected.append(name)

    required = {
        "impodo/project_security.py",
        "impodo/web/launcher.py",
    }
    missing = sorted(required.difference(names))
    if unexpected or missing:
        detail = []
        if unexpected:
            detail.append(f"unexpected paths: {', '.join(sorted(unexpected)[:5])}")
        if missing:
            detail.append(f"missing paths: {', '.join(missing)}")
        raise ReleaseGateError(
            "the Impodo wheel failed its content allowlist; " + "; ".join(detail)
        )


def _unexpected_secret_candidates(
    scan: dict[str, Any],
    baseline: dict[str, Any],
) -> list[dict[str, Any]]:
    reviewed = {
        _secret_fingerprint(filename, candidate)
        for filename, candidates in baseline.get("results", {}).items()
        for candidate in candidates
        if candidate.get("is_secret") is False
    }
    return [
        {"filename": filename, **candidate}
        for filename, candidates in scan.get("results", {}).items()
        for candidate in candidates
        if _secret_fingerprint(filename, candidate) not in reviewed
    ]


def _secret_fingerprint(filename: str, candidate: dict[str, Any]) -> tuple[str, str, str]:
    return (
        filename.replace("\\", "/"),
        str(candidate.get("type", "")),
        str(candidate.get("hashed_secret", "")),
    )


def _release_manifest(
    *,
    release_id: str,
    version: str,
    revision: str,
    artifacts: tuple[Path, ...],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "release_id": release_id,
        "impodo_version": version,
        "source_revision": revision,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "platform": "windows",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "artifacts": [
            {
                "name": artifact.name,
                "sha256": _sha256(artifact),
                "bytes": artifact.stat().st_size,
            }
            for artifact in sorted(artifacts, key=lambda item: item.name)
        ],
    }


def _project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream).get("project", {})
    version = str(project.get("version", ""))
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+-]*", version):
        raise ReleaseGateError("pyproject.toml has no safe project version")
    return version


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(*arguments: str) -> str:
    completed = _run("git", *arguments, capture=True)
    return (completed.stdout or "").strip()


def _tool(name: str) -> Path:
    suffix = ".exe" if sys.platform == "win32" else ""
    candidate = Path(sys.executable).resolve().parent / f"{name}{suffix}"
    if not candidate.is_file():
        raise ReleaseGateError(
            f"release tool is missing: {name}; install .[test,release] first"
        )
    return candidate


def _run(
    *command: str,
    capture: bool = False,
    cwd: Path = ROOT,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            check=True,
            text=True,
            capture_output=capture,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        raise ReleaseGateError(
            f"command failed ({Path(command[0]).name}): {detail or error.returncode}"
        ) from error


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _require_relative_to(path: Path, parent: Path, label: str) -> None:
    try:
        path.relative_to(parent)
    except ValueError as error:
        raise ReleaseGateError(f"{label} escapes its governed parent") from error


def _safe_remove_work_directory(work: Path) -> None:
    resolved = work.resolve()
    parent = WORK_ROOT.resolve()
    _require_relative_to(resolved, parent, "release work directory")
    if resolved == parent:
        raise ReleaseGateError("refusing to remove the release work root")
    shutil.rmtree(resolved)


if __name__ == "__main__":
    raise SystemExit(main())
