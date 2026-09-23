"""Qualify Impodo read adapters against the pinned disposable Odoo 20 lab.

The runner starts only the PostgreSQL cluster and Odoo process named by a
trusted lab receipt, creates a one-hour RPC key for the disposable database,
and exercises the closed local-shell and JSON-2 read surfaces. The key is kept
in memory, revoked before exit, and never written to the report or logs.

The report contains version, module, model, field-count, and policy evidence.
It contains no credentials, numeric Odoo identifiers, or record values. This
runner does not exercise or authorize Impodo writes or Production use.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
KEY_MARKER = "__IMPODO_QUALIFICATION_KEY__"
KEY_ID_MARKER = "__IMPODO_QUALIFICATION_KEY_ID__"
QUALIFIED_MODELS = (
    "mrp.bom",
    "mrp.bom.line",
    "product.category",
    "product.template",
    "res.country",
    "res.currency",
    "res.lang",
    "res.partner",
    "uom.uom",
)
REFERENCE_FIELDS = {
    "res.country": ("code", "name"),
    "res.currency": ("name",),
    "res.lang": ("code", "name"),
}
REQUIRED_FIELDS = {
    "mrp.bom": ("bom_line_ids", "company_id", "product_id", "product_qty", "product_tmpl_id", "type", "uom_id", "write_date"),
    "mrp.bom.line": ("bom_id", "product_id", "product_qty", "uom_id", "write_date"),
    "product.category": ("complete_name", "name", "parent_id", "write_date"),
    "product.template": ("active", "categ_id", "name", "uom_id", "write_date"),
    "res.country": REFERENCE_FIELDS["res.country"],
    "res.currency": REFERENCE_FIELDS["res.currency"],
    "res.lang": REFERENCE_FIELDS["res.lang"],
    "res.partner": ("active", "company_id", "country_id", "email", "name", "write_date"),
    "uom.uom": ("active", "factor", "name", "relative_factor", "relative_uom_id", "sequence", "write_date"),
}
RELEVANT_MODULES = ("base", "contacts", "product", "mrp")


class QualificationError(RuntimeError):
    """Stop without making a compatibility claim."""


def _run(
    arguments: list[str],
    *,
    timeout: int = 120,
    input_text: str | None = None,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=(subprocess.PIPE if capture_output else subprocess.DEVNULL),
        stderr=(subprocess.PIPE if capture_output else subprocess.DEVNULL),
        stdin=(None if input_text is not None else subprocess.DEVNULL),
        creationflags=NO_WINDOW,
        timeout=timeout,
    )
    if result.returncode:
        raise QualificationError(f"{Path(arguments[0]).name} failed")
    return result


def _require_free_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", port))


def _wait_for_version(base_url: str, expected: str, process: subprocess.Popen) -> None:
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise QualificationError("Odoo stopped before the version probe succeeded")
        try:
            with opener.open(Request(f"{base_url}/web/version"), timeout=2) as response:
                payload = json.loads(response.read(64 * 1024))
            if payload.get("version") != expected:
                raise QualificationError("Odoo reported an unexpected version")
            return
        except QualificationError:
            raise
        except (OSError, ValueError):
            time.sleep(0.25)
    raise QualificationError("Odoo did not become ready before the timeout")


def _shell_command(receipt: dict[str, object]) -> list[str]:
    return [
        str(receipt["python"]),
        str(Path(str(receipt["source"])) / "odoo-bin"),
        "shell",
        "-c",
        str(receipt["config"]),
        "-d",
        str(receipt["database"]),
        "--no-http",
    ]


def _create_temporary_key(receipt: dict[str, object]) -> tuple[str, int]:
    script = "\n".join(
        (
            "import datetime",
            "user = env['res.users'].sudo().search([('login', '=', 'admin'), ('active', '=', True)], limit=2)",
            "if len(user) != 1:",
            "    raise RuntimeError('Disposable lab admin user is unavailable')",
            "key = env['res.users.apikeys'].with_user(user)._generate(",
            "    'rpc',",
            "    'Impodo final Odoo 20 read qualification',",
            "    datetime.datetime.now() + datetime.timedelta(hours=1),",
            ")",
            "env.cr.execute('SELECT id FROM res_users_apikeys WHERE index = %s', (key[:8],))",
            "key_id = env.cr.fetchone()[0]",
            "env.cr.commit()",
            f"print('{KEY_MARKER}' + key)",
            f"print('{KEY_ID_MARKER}' + str(key_id))",
            "env.cr.rollback()",
        )
    )
    result = _run(_shell_command(receipt), input_text=script)
    key = ""
    key_id = 0
    for line in result.stdout.splitlines():
        if line.startswith(KEY_MARKER):
            key = line.removeprefix(KEY_MARKER).strip()
        elif line.startswith(KEY_ID_MARKER):
            key_id = int(line.removeprefix(KEY_ID_MARKER).strip())
    if re.fullmatch(r"[0-9a-f]{40}", key) is None or key_id < 1:
        raise QualificationError("Odoo did not create the temporary RPC key")
    return key, key_id


def _revoke_temporary_key(receipt: dict[str, object], key_id: int) -> None:
    if key_id < 1:
        return
    script = "\n".join(
        (
            f"key = env['res.users.apikeys'].sudo().browse({key_id})",
            "if key.exists():",
            "    key._remove()",
            "env.cr.commit()",
            "env.cr.rollback()",
        )
    )
    _run(_shell_command(receipt), input_text=script)


def _field_signature(field) -> dict[str, object]:
    values = asdict(field)
    return {
        name: values[name]
        for name in (
            "name",
            "type",
            "required",
            "readonly",
            "relation",
            "relation_field",
            "selection",
            "stored",
            "computed",
            "has_inverse",
            "related",
            "translated",
            "company_dependent",
            "searchable",
            "sortable",
            "exportable",
            "digits",
            "currency_field",
        )
    }


def _qualify(receipt: dict[str, object], api_key: str) -> dict[str, object]:
    from impodo.adapters.odoo.connectors import Json2Config, Json2ReadConnector
    from impodo.adapters.odoo.local_reader import LocalOdooMetadataReader
    from impodo.adapters.odoo.local_stack import LocalStackProfile
    from impodo.domain.odoo.compatibility import OdooOperation, assess_odoo_operation
    from impodo.domain.odoo.contracts import MetadataRequest, RecordRequest
    from impodo.domain.odoo_source_policy import ODOO_SOURCE_POLICY_HASHES
    from impodo.domain.workspace.reference_keys import REFERENCE_POLICY_HASHES
    from impodo.domain.workspace.workbench import OdooConnectionMode, WorkspaceState

    base_url = f"http://127.0.0.1:{int(receipt['http_port'])}"
    database = str(receipt["database"])
    workspace = WorkspaceState(
        workspace_id="odoo20-read-qualification",
        name="Final Odoo 20 read qualification",
        source_system="Synthetic disposable fixture",
        odoo_connection_mode=OdooConnectionMode.LOCAL,
        odoo_base_url=base_url,
        odoo_database=database,
        intended_models=QUALIFIED_MODELS,
    )
    postgres_bin = Path(str(receipt["postgres_bin"]))
    profile = LocalStackProfile(
        config_path=Path(str(receipt["config"])),
        workspace_root=Path(str(receipt["config"])).parent.parent,
        db_host="127.0.0.1",
        db_port=int(receipt["postgres_port"]),
        db_user="impodo_lab",
        http_interface="127.0.0.1",
        http_port=int(receipt["http_port"]),
        base_url=base_url,
        database_hint=database,
        pg_isready_path=postgres_bin / "pg_isready.exe",
        pg_ctl_path=postgres_bin / "pg_ctl.exe",
        pg_data_path=Path(str(receipt["pgdata"])),
        python_path=Path(str(receipt["python"])),
        odoo_bin_path=Path(str(receipt["source"])) / "odoo-bin",
        logs_path=Path(str(receipt["config"])).parent,
    )

    local = LocalOdooMetadataReader(timeout_seconds=180)
    local_fingerprint = local.get_target_fingerprint(workspace, profile)
    local_catalog = local.get_model_catalog(workspace, profile)
    local_metadata = local.get_model_metadata(workspace, profile, QUALIFIED_MODELS)
    local_preflight_metadata, local_records = local.get_preflight_snapshots(
        workspace,
        profile,
        (
            MetadataRequest(
                model="res.country",
                fields=REFERENCE_FIELDS["res.country"],
            ),
        ),
        (
            RecordRequest(
                model="res.country",
                fields=REFERENCE_FIELDS["res.country"],
                domain=(("code", "in", ("BE", "US")),),
                limit=2,
            ),
        ),
    )

    remote = Json2ReadConnector(
        remote_config := Json2Config(
            base_url=base_url,
            database=database,
            api_key=api_key,
            connection_mode="LOCAL",
            relevant_modules=RELEVANT_MODULES,
        )
    )
    remote_fingerprint = remote.get_target_fingerprint()
    identity = remote.probe_read_identity(QUALIFIED_MODELS)
    metadata_requests = tuple(
        MetadataRequest(model=model, fields=(), all_fields=True)
        for model in QUALIFIED_MODELS
    )
    remote_metadata = remote.get_model_metadata(metadata_requests)
    remote_records = remote.get_records(
        (
            RecordRequest(
                model="res.country",
                fields=REFERENCE_FIELDS["res.country"],
                domain=(("code", "in", ("BE", "US")),),
                limit=2,
            ),
        )
    )
    from odoo20_read_qualification_support import qualify_bounded_source_capture

    source_capture = qualify_bounded_source_capture(remote_config)

    if local_fingerprint.odoo_version != "20.0" or remote_fingerprint.odoo_version != "20.0":
        raise QualificationError("Both readers must observe final Odoo 20.0")
    if identity.readable_models != QUALIFIED_MODELS:
        raise QualificationError("The JSON-2 identity probe omitted a qualified model")
    if set(local_metadata.models) != set(QUALIFIED_MODELS) or set(remote_metadata.models) != set(QUALIFIED_MODELS):
        raise QualificationError("A metadata reader omitted a qualified model")

    field_contracts_equal: dict[str, bool] = {}
    required_field_contracts_equal: dict[str, bool] = {}
    visibility_delta: dict[str, dict[str, object]] = {}
    field_counts: dict[str, dict[str, int]] = {}
    for model in QUALIFIED_MODELS:
        local_fields = local_metadata.models[model].fields
        remote_fields = remote_metadata.models[model].fields
        field_counts[model] = {
            "local": len(local_fields),
            "json2": len(remote_fields),
        }
        field_contracts_equal[model] = (
            set(local_fields) == set(remote_fields)
            and all(
                _field_signature(local_fields[name])
                == _field_signature(remote_fields[name])
                for name in local_fields
            )
        )
        required_names = REQUIRED_FIELDS[model]
        required_field_contracts_equal[model] = all(
            name in local_fields
            and name in remote_fields
            and _field_signature(local_fields[name])
            == _field_signature(remote_fields[name])
            for name in required_names
        )
        shared_names = set(local_fields) & set(remote_fields)
        visibility_delta[model] = {
            "local_only": len(set(local_fields) - set(remote_fields)),
            "json2_only": len(set(remote_fields) - set(local_fields)),
            "changed_shared_contracts": sum(
                _field_signature(local_fields[name])
                != _field_signature(remote_fields[name])
                for name in shared_names
            ),
            "changed_shared_field_names": sorted(
                name
                for name in shared_names
                if _field_signature(local_fields[name])
                != _field_signature(remote_fields[name])
            ),
        }
    if not all(required_field_contracts_equal.values()):
        failed_fields = {}
        for model, matched in required_field_contracts_equal.items():
            if matched:
                continue
            local_fields = local_metadata.models[model].fields
            remote_fields = remote_metadata.models[model].fields
            failed_fields[model] = {
                "missing_local": sorted(set(REQUIRED_FIELDS[model]) - set(local_fields)),
                "missing_json2": sorted(set(REQUIRED_FIELDS[model]) - set(remote_fields)),
                "changed": sorted(
                    name
                    for name in REQUIRED_FIELDS[model]
                    if name in local_fields
                    and name in remote_fields
                    and _field_signature(local_fields[name])
                    != _field_signature(remote_fields[name])
                ),
            }
        raise QualificationError(
            "Required local and JSON-2 field contracts differ: "
            + json.dumps(failed_fields, sort_keys=True)
        )
    if len(local_records.records.get("res.country", ())) != 2 or len(remote_records.records.get("res.country", ())) != 2:
        raise QualificationError("The bounded country read did not return two fixtures")
    if set(local_preflight_metadata.models["res.country"].fields) != set(REFERENCE_FIELDS["res.country"]):
        raise QualificationError("The local bounded metadata projection changed")

    operations = {
        operation.value: assess_odoo_operation("20.0", operation).allowed
        for operation in OdooOperation
    }
    if not all(operations[name] for name in ("CONNECT", "CAPTURE_SCHEMA", "CAPTURE_SOURCE", "COMPARE", "RECIPE")):
        raise QualificationError("A qualified Odoo 20 read operation is disabled")
    if any(operations[name] for name in ("WRITE", "PRODUCTION", "RECOVER")):
        raise QualificationError("An Odoo 20 write operation became enabled")

    catalog_models = {
        str(record.values.get("model"))
        for record in local_catalog.records["ir.model"]
    }
    if not set(QUALIFIED_MODELS).issubset(catalog_models):
        raise QualificationError("The model catalogue omitted a qualified model")

    return {
        "contract_version": 1,
        "status": "PASSED",
        "scope": "Final Odoo 20 Community read qualification; writes and Production excluded",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "odoo": {
            "commit": str(receipt["commit"]),
            "edition": "Community",
            "reported_version": "20.0",
            "module_versions": dict(sorted(remote_fingerprint.module_versions.items())),
        },
        "runtime": {
            "python": str(receipt["python_version"]),
            "postgresql": str(receipt["postgres_version"]),
        },
        "policy": {
            "operations": operations,
            "reference_policy_hash": REFERENCE_POLICY_HASHES[20],
            "source_policy_hash": ODOO_SOURCE_POLICY_HASHES[20],
        },
        "evidence": {
            "qualified_models": list(QUALIFIED_MODELS),
            "persistent_model_count": len(local_catalog.records["ir.model"]),
            "field_counts": field_counts,
            "local_json2_field_contracts_equal": field_contracts_equal,
            "required_field_contracts_equal": required_field_contracts_equal,
            "metadata_visibility_delta": visibility_delta,
            "bounded_country_rows": {
                "local": len(local_records.records["res.country"]),
                "json2": len(remote_records.records["res.country"]),
            },
            "identity_probe_model_count": len(identity.readable_models),
            "bounded_source_capture": source_capture,
        },
        "limitations": [
            "No Impodo write adapter or Odoo mutation was exercised.",
            "No Production, recovery, Enterprise, hosted, or custom-module claim was established.",
            "The JSON-2 probe used loopback HTTP; remote HTTPS remains a separate deployment qualification.",
        ],
    }


def qualify(receipt_path: Path, output_path: Path) -> None:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    required = {
        "commit",
        "reported_version",
        "config",
        "source",
        "python",
        "postgres_bin",
        "pgdata",
        "database",
        "http_port",
        "postgres_port",
        "python_version",
        "postgres_version",
    }
    if not required.issubset(receipt) or receipt.get("target") != "odoo20":
        raise QualificationError("Use a complete prepared receipt for the odoo20 target")
    if receipt.get("reported_version") != "20.0":
        raise QualificationError("The lab receipt is not final Odoo 20")
    if receipt.get("services_running") is not False:
        raise QualificationError("The lab receipt must say that services are stopped")

    http_port = int(receipt["http_port"])
    postgres_port = int(receipt["postgres_port"])
    _require_free_port(http_port)
    _require_free_port(postgres_port)
    postgres_bin = Path(str(receipt["postgres_bin"]))
    pg_ctl = str(postgres_bin / "pg_ctl.exe")
    pgdata = str(receipt["pgdata"])
    lab_dir = receipt_path.parent
    postgres_started = False
    odoo_process: subprocess.Popen | None = None
    odoo_log = None
    key_id = 0
    report: dict[str, object] | None = None
    qualification_error: BaseException | None = None
    cleanup_errors: list[BaseException] = []
    try:
        _run(
            [
                pg_ctl,
                "-D",
                pgdata,
                "-l",
                str(lab_dir / "postgres-qualification.log"),
                "-o",
                f"-h 127.0.0.1 -p {postgres_port}",
                "-w",
                "start",
            ],
            timeout=60,
            capture_output=False,
        )
        postgres_started = True
        odoo_log = (lab_dir / "odoo-qualification.log").open("ab")
        odoo_process = subprocess.Popen(
            [
                str(receipt["python"]),
                str(Path(str(receipt["source"])) / "odoo-bin"),
                "-c",
                str(receipt["config"]),
            ],
            stdin=subprocess.DEVNULL,
            stdout=odoo_log,
            stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW,
        )
        base_url = f"http://127.0.0.1:{http_port}"
        _wait_for_version(base_url, "20.0", odoo_process)
        api_key, key_id = _create_temporary_key(receipt)
        report = _qualify(receipt, api_key)
    except BaseException as error:
        qualification_error = error
    finally:
        if key_id and postgres_started:
            try:
                _revoke_temporary_key(receipt, key_id)
            except (OSError, QualificationError, subprocess.SubprocessError) as error:
                cleanup_errors.append(error)
        if odoo_process is not None and odoo_process.poll() is None:
            try:
                odoo_process.terminate()
                try:
                    odoo_process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    odoo_process.kill()
                    odoo_process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError) as error:
                cleanup_errors.append(error)
        if odoo_log is not None:
            odoo_log.close()
        if postgres_started:
            try:
                _run(
                    [pg_ctl, "-D", pgdata, "-m", "fast", "-w", "stop"],
                    timeout=60,
                    capture_output=False,
                )
            except (OSError, QualificationError, subprocess.SubprocessError) as error:
                cleanup_errors.append(error)
    if cleanup_errors:
        raise QualificationError(
            "Odoo 20 qualification cleanup failed; verify that the temporary "
            "API key and isolated services were removed"
        ) from cleanup_errors[0]
    if qualification_error is not None:
        raise qualification_error
    assert report is not None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--receipt",
        type=Path,
        default=ROOT / ".tmp" / "odoo-compatibility" / "odoo20" / "prepared.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / ".tmp" / "odoo-compatibility" / "odoo20" / "read-qualification.json",
    )
    args = parser.parse_args()
    try:
        qualify(args.receipt.resolve(), args.output.resolve())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Odoo 20 read qualification failed: {error}", file=sys.stderr)
        return 1
    print(f"Odoo 20 read qualification passed: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
