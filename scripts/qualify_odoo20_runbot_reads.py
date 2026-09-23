"""Qualify Impodo's JSON-2 reads against a disposable Odoo 20 Runbot.

The runner signs in with the supplied temporary Runbot account, creates a
one-day API key through Odoo's interactive identity check, exercises only
Impodo's closed JSON-2 read connector, and revokes the key before exit. The
sanitized report contains no credentials, API key, session cookie, numeric
Odoo identifiers, or record values.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
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


class RunbotQualificationError(RuntimeError):
    """Stop without making a remote compatibility claim."""


class RunbotSession:
    """Minimal interactive session used only to mint a temporary API key."""

    def __init__(self, base_url: str, database: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.database = database
        self._request_id = 0
        self._opener = build_opener(
            ProxyHandler({}),
            HTTPCookieProcessor(CookieJar()),
        )

    def rpc(self, path: str, params: dict[str, object]) -> object:
        self._request_id += 1
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "call",
                "params": params,
                "id": self._request_id,
            }
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=30) as response:
                result = json.loads(response.read(8 * 1024 * 1024))
        except (HTTPError, URLError, OSError, ValueError) as error:
            raise RunbotQualificationError(
                "Runbot interactive request failed"
            ) from error
        if not isinstance(result, dict) or result.get("error"):
            raise RunbotQualificationError("Runbot rejected an interactive request")
        return result.get("result")

    def authenticate(self, login: str, password: str) -> None:
        result = self.rpc(
            "/web/session/authenticate",
            {
                "db": self.database,
                "login": login,
                "password": password,
            },
        )
        if not isinstance(result, dict) or not result.get("uid"):
            raise RunbotQualificationError("Runbot login was not accepted")

    def call_kw(
        self,
        model: str,
        method: str,
        args: list[object],
        *,
        context: dict[str, object] | None = None,
    ) -> object:
        return self.rpc(
            f"/web/dataset/call_kw/{model}/{method}",
            {
                "model": model,
                "method": method,
                "args": args,
                "kwargs": {"context": context or {}},
            },
        )

    def create_api_key(self, password: str) -> str:
        wizard_id = self.call_kw(
            "res.users.apikeys.description",
            "create",
            [[{
                "name": "Impodo final Odoo 20 Runbot read qualification",
                "scope": "rpc",
                "duration": "1",
            }]],
        )
        if isinstance(wizard_id, list):
            wizard_id = wizard_id[0] if wizard_id else 0
        if not isinstance(wizard_id, int) or wizard_id < 1:
            raise RunbotQualificationError("Odoo did not create the API-key wizard")
        action = self.call_kw(
            "res.users.apikeys.description",
            "make_key",
            [[wizard_id]],
        )
        if not isinstance(action, dict):
            raise RunbotQualificationError("Odoo did not start the identity check")
        if action.get("res_model") == "res.users.identitycheck":
            identity_id = action.get("res_id")
            if not isinstance(identity_id, int) or identity_id < 1:
                raise RunbotQualificationError("Odoo returned an invalid identity check")
            action = self.call_kw(
                "res.users.identitycheck",
                "run_check",
                [[identity_id]],
                context={"password": password},
            )
        if not isinstance(action, dict):
            raise RunbotQualificationError("Odoo did not return the API key")
        context = action.get("context")
        key = context.get("default_key") if isinstance(context, dict) else None
        if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{40}", key) is None:
            raise RunbotQualificationError("Odoo returned an invalid API key")
        return key


def _json2_revoke(base_url: str, database: str, api_key: str) -> None:
    request = Request(
        f"{base_url.rstrip('/')}/json/2/res.users.apikeys/revoke",
        data=json.dumps({"key": api_key}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Odoo-Database": database,
        },
        method="POST",
    )
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=30) as response:
            result = json.loads(response.read(64 * 1024))
    except (HTTPError, URLError, OSError, ValueError) as error:
        raise RunbotQualificationError("The temporary Runbot API key was not revoked") from error
    if result is not True:
        raise RunbotQualificationError("The temporary Runbot API key was not revoked")


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


def _qualify(base_url: str, database: str, api_key: str) -> dict[str, object]:
    from impodo.adapters.odoo.connectors import Json2Config, Json2ReadConnector
    from impodo.domain.odoo.compatibility import OdooOperation, assess_odoo_operation
    from impodo.domain.odoo.contracts import MetadataRequest, RecordRequest
    from impodo.domain.odoo_source_policy import ODOO_SOURCE_POLICY_HASHES
    from impodo.domain.workspace.reference_keys import (
        REFERENCE_POLICY_HASHES,
        captured_reference_field_contracts,
        standard_reference_key,
    )

    config = Json2Config(
            base_url=base_url,
            database=database,
            api_key=api_key,
            connection_mode="REMOTE",
            relevant_modules=RELEVANT_MODULES,
    )
    connector = Json2ReadConnector(config)
    fingerprint = connector.get_target_fingerprint()
    identity = connector.probe_read_identity(QUALIFIED_MODELS)
    metadata = connector.get_model_metadata(
        tuple(
            MetadataRequest(model=model, fields=(), all_fields=True)
            for model in QUALIFIED_MODELS
        )
    )
    records = connector.get_records(
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

    source_capture = qualify_bounded_source_capture(config)
    decision = assess_odoo_operation(fingerprint.odoo_version, OdooOperation.COMPARE)
    if not decision.allowed or decision.version.major != 20:
        raise RunbotQualificationError("Runbot did not report qualified final Odoo 20")
    if identity.readable_models != QUALIFIED_MODELS:
        raise RunbotQualificationError("The identity probe omitted a qualified model")
    if set(metadata.models) != set(QUALIFIED_MODELS):
        raise RunbotQualificationError("JSON-2 omitted a qualified model")
    missing_required_fields = {
        model: sorted(set(required_fields) - set(metadata.models[model].fields))
        for model, required_fields in REQUIRED_FIELDS.items()
        if set(required_fields) - set(metadata.models[model].fields)
    }
    if missing_required_fields:
        raise RunbotQualificationError("A required final Odoo 20 field is missing")
    if len(records.records.get("res.country", ())) != 2:
        raise RunbotQualificationError("The bounded country read did not return two fixtures")

    reference_contracts: dict[str, bool] = {}
    for model, field_names in REFERENCE_FIELDS.items():
        standard = standard_reference_key(model, odoo_major_version=20)
        captured = captured_reference_field_contracts(
            metadata.models[model].fields[name] for name in field_names
        )
        reference_contracts[model] = bool(
            standard is not None and captured == standard.field_contracts
        )
    if not all(reference_contracts.values()):
        raise RunbotQualificationError("A reviewed Odoo 20 reference contract changed")

    operations = {
        operation.value: assess_odoo_operation(
            fingerprint.odoo_version,
            operation,
        ).allowed
        for operation in OdooOperation
    }
    if not all(operations[name] for name in ("CONNECT", "CAPTURE_SCHEMA", "CAPTURE_SOURCE", "COMPARE", "RECIPE")):
        raise RunbotQualificationError("A qualified Odoo 20 read operation is disabled")
    if any(operations[name] for name in ("WRITE", "PRODUCTION", "RECOVER")):
        raise RunbotQualificationError("An Odoo 20 write operation became enabled")

    return {
        "contract_version": 1,
        "status": "PASSED",
        "scope": "Final Odoo 20 Enterprise Runbot JSON-2 read qualification; writes and Production excluded",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "deployment": {
            "edition": "Enterprise",
            "host": urlparse(base_url).hostname,
            "reported_version": fingerprint.odoo_version,
            "module_versions": dict(sorted(fingerprint.module_versions.items())),
        },
        "policy": {
            "operations": operations,
            "reference_policy_hash": REFERENCE_POLICY_HASHES[20],
            "source_policy_hash": ODOO_SOURCE_POLICY_HASHES[20],
        },
        "evidence": {
            "qualified_models": list(QUALIFIED_MODELS),
            "field_counts": {
                model: len(metadata.models[model].fields)
                for model in QUALIFIED_MODELS
            },
            "required_field_contracts_match": True,
            "reference_contracts_match": reference_contracts,
            "bounded_country_rows": len(records.records["res.country"]),
            "identity_probe_model_count": len(identity.readable_models),
            "metadata_limitations": list(metadata.limitations),
            "bounded_source_capture": source_capture,
        },
        "limitations": [
            "No Impodo write adapter or Odoo mutation other than the revoked temporary API key was exercised.",
            "No Production, recovery, custom-module, or performance claim was established.",
            "Runbot is temporary evidence and must be refreshed after the instance expires or changes.",
            "Odoo 20 unit-of-measure fields are qualified under their native names; no Odoo 19 field alias is implied.",
        ],
    }


def qualify(
    base_url: str,
    database: str,
    login: str,
    password: str,
    output_path: Path,
) -> None:
    session = RunbotSession(base_url, database)
    session.authenticate(login, password)
    api_key = session.create_api_key(password)
    report: dict[str, object] | None = None
    qualification_error: BaseException | None = None
    try:
        report = _qualify(base_url, database, api_key)
    except BaseException as error:
        qualification_error = error
    try:
        _json2_revoke(base_url, database, api_key)
    except BaseException as revoke_error:
        if qualification_error is None:
            raise
        raise RunbotQualificationError(
            "Qualification failed and the temporary API key could not be revoked"
        ) from revoke_error
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
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--login", default="admin")
    parser.add_argument("--password-env", default="IMPODO_RUNBOT_PASSWORD")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / ".tmp" / "odoo-compatibility" / "odoo20" / "runbot-read-qualification.json",
    )
    args = parser.parse_args()
    password = os.environ.get(args.password_env, "")
    if not password:
        print("Runbot qualification password environment variable is missing", file=sys.stderr)
        return 1
    try:
        qualify(
            args.base_url.rstrip("/"),
            args.database,
            args.login,
            password,
            args.output.resolve(),
        )
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Odoo 20 Runbot read qualification failed: {error}", file=sys.stderr)
        return 1
    print(f"Odoo 20 Runbot read qualification passed: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
