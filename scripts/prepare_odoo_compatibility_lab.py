"""Prepare a pinned Windows Odoo test database in an isolated workspace folder.

The caller supplies an already checked-out official Odoo source tree, Python
3.12, and PostgreSQL binaries. This script validates the recorded source pin,
creates a fresh virtual environment and disposable PostgreSQL cluster, and
initializes only the declared fixture modules. It never fetches source,
resets a database, or starts the normal developer installation. Both services
are stopped on success; the resulting receipt names the reusable local paths.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import socket
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "fixtures" / "odoo-compatibility" / "lab-targets.json"
LAB_ROOT = ROOT / ".tmp" / "odoo-compatibility"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _output(arguments: list[str]) -> str:
    return subprocess.check_output(
        arguments, text=True, encoding="utf-8", creationflags=NO_WINDOW,
        stderr=subprocess.STDOUT, timeout=30,
    ).strip()


def _run(arguments: list[str], log: Path, *, timeout: int = 600) -> None:
    with log.open("ab") as stream:
        result = subprocess.run(
            arguments, stdout=stream, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=timeout,
        )
    if result.returncode:
        raise RuntimeError(f"Command failed ({Path(arguments[0]).name}); inspect {log}")


def _require_free_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        # Windows must reject an already-bound port, including a reusable one.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", port))


def configuration(target: dict, source: Path, destination: Path) -> str:
    """Bind every Odoo path and listener to this disposable test target."""

    paths = (source, destination)
    if any(any(char in str(path) for char in "\r\n%") for path in paths):
        raise ValueError("Lab paths cannot contain newlines or percent signs")
    return "\n".join((
        "[options]",
        "admin_passwd = disabled-disposable-lab-only",
        f"addons_path = {source / 'addons'}",
        f"data_dir = {destination / 'odoo-data'}",
        "db_host = 127.0.0.1",
        f"db_port = {target['postgres_port']}",
        "db_user = impodo_lab",
        f"db_name = {target['database']}",
        f"dbfilter = ^{target['database']}$",
        "http_interface = 127.0.0.1",
        f"http_port = {target['http_port']}",
        "list_db = False",
        "workers = 0",
        "max_cron_threads = 0",
        "",
    ))


def prepare(target_name: str, source: Path, python: Path, postgres_bin: Path) -> Path:
    """Initialize a new lab, refusing changed sources or existing destinations."""

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    target = manifest["targets"][target_name]
    if not re.fullmatch(r"[0-9a-f]{40}", target["commit"]):
        raise ValueError("Lab source must be pinned to a full commit")
    source, python, postgres_bin = source.resolve(), python.resolve(), postgres_bin.resolve()
    python_lock = MANIFEST.parent / target["python_lock"]
    destination = (LAB_ROOT / target_name).resolve()
    if destination.parent != LAB_ROOT.resolve() or destination.exists():
        raise ValueError(f"Use a fresh lab destination; existing data is never reset: {destination}")
    required = [python, python_lock, source / "odoo-bin", source / "requirements.txt"]
    required += [postgres_bin / f"{name}.exe" for name in (
        "postgres", "pg_ctl", "initdb", "createuser", "createdb",
    )]
    for path in required:
        if not path.is_file():
            raise ValueError(f"Required local file is missing: {path}")
    commit = _output(["git", "-C", str(source), "rev-parse", "HEAD"])
    if commit != target["commit"]:
        raise ValueError("Odoo checkout does not match the pinned lab commit")
    if _output(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"]):
        raise ValueError("Use a clean Odoo checkout, without extra addons or configuration")
    python_version = _output([str(python), "--version"])
    if not python_version.startswith("Python 3.12."):
        raise ValueError("This baseline requires Python 3.12")
    postgres_version = _output([str(postgres_bin / "postgres.exe"), "--version"])
    if not re.search(r"PostgreSQL\) 17\.", postgres_version):
        raise ValueError("This baseline requires PostgreSQL 17")
    for port in (target["http_port"], target["postgres_port"]):
        _require_free_port(port)
    config = configuration(target, source, destination)
    destination.mkdir(parents=True)
    log = destination / "prepare.log"
    config_path = destination / "odoo.conf"
    config_path.write_text(config, encoding="utf-8")
    environment = destination / "venv"
    _run([str(python), "-m", "venv", str(environment)], log)
    lab_python = environment / "Scripts" / "python.exe"
    _run([str(lab_python), "-m", "pip", "install", "--disable-pip-version-check",
          "-r", str(source / "requirements.txt"), "-r", str(python_lock)], log)
    resolved = _output([str(lab_python), "-m", "pip", "freeze", "--all"])
    (destination / "resolved-requirements.txt").write_text(resolved + "\n", encoding="utf-8")
    version = _output([str(lab_python), str(source / "odoo-bin"), "--version"])
    if version != f"Odoo Server {target['reported_version']}":
        raise ValueError(f"Unexpected version from pinned source: {version}")
    data = destination / "pgdata"
    pg_ctl = str(postgres_bin / "pg_ctl.exe")
    # The cluster contains only generated fixtures and listens on loopback.
    # No developer or customer database and no existing credentials are reused.
    _run([str(postgres_bin / "initdb.exe"), "-D", str(data), "-U", "postgres",
          "--auth=trust", "--encoding=UTF8", "--locale=C"], log)
    started = False
    try:
        _run([pg_ctl, "-D", str(data), "-l", str(destination / "postgres.log"),
              "-o", f"-h 127.0.0.1 -p {target['postgres_port']}", "-w", "start"], log)
        started = True
        connection = ["--host=127.0.0.1", f"--port={target['postgres_port']}"]
        _run([str(postgres_bin / "createuser.exe"), *connection, "--username=postgres",
              "--no-superuser", "--no-createdb", "--no-createrole", "impodo_lab"], log)
        _run([str(postgres_bin / "createdb.exe"), *connection, "--username=postgres",
              "--owner=impodo_lab", target["database"]], log)
        _run([str(lab_python), str(source / "odoo-bin"), "-c", str(config_path),
              "-i", ",".join(manifest["modules"]), "--without-demo=all", "--stop-after-init"], log)
    finally:
        if started:
            _run([pg_ctl, "-D", str(data), "-m", "fast", "-w", "stop"], log, timeout=60)
    receipt = {
        "contract_version": 1,
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "target": target_name,
        "commit": commit,
        "reported_version": target["reported_version"],
        "qualification": "DATABASE_PREPARED_NOT_IMPODO_QUALIFIED",
        "modules": manifest["modules"],
        "python_version": python_version,
        "postgres_version": postgres_version,
        "requirements_sha256": hashlib.sha256((source / "requirements.txt").read_bytes()).hexdigest(),
        "resolved_requirements_sha256": hashlib.sha256((resolved + "\n").encode()).hexdigest(),
        "config": str(config_path),
        "source": str(source),
        "python": str(lab_python),
        "postgres_bin": str(postgres_bin),
        "pgdata": str(data),
        "database": target["database"],
        "http_port": target["http_port"],
        "postgres_port": target["postgres_port"],
        "services_running": False,
    }
    receipt_path = destination / "prepared.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("baseline", "preview"), required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--postgres-bin", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = prepare(args.target, args.source, args.python, args.postgres_bin)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Lab preparation stopped: {error}", file=sys.stderr)
        return 1
    print(f"Prepared isolated database; services stopped. Receipt: {receipt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
