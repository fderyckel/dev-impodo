"""Qualify the Odoo 19 generated-variant boundary for a Product transfer.

The runner uses one supplied Odoo connection as a read-only source for the
normal destination-matching check. It then creates one uniquely tagged Product
on a distinct disposable destination, verifies that Odoo generated exactly one
variant, and removes only the tagged Product. Credentials and business values
are never written to the result or printed.

This is a narrow prerequisite for the Product transfer vertical. It does not
qualify Category or Unit-of-Measure relationship identity, the complete Impodo
browser transfer, interruption recovery, or repeat idempotence.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from typing import Iterable
from urllib.parse import quote
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from impodo.adapters.odoo.connectors import Json2Config, Json2ReadConnector
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.odoo.contracts import RecordRequest
from impodo.domain.shared.models import canonical_json_bytes
from scripts.qualify_odoo_to_odoo_matching import _connections, qualify as qualify_matching


PRODUCT_MODEL = "product.template"
PRODUCT_SCOPE = OdooApiScope(
    preview_hash="sha256:" + "7" * 64,
    models=(
        OdooModelScope(
            PRODUCT_MODEL,
            write_fields=("default_code", "name"),
            read_fields=("default_code", "name", "product_variant_ids"),
            lookup_fields=("default_code",),
        ),
    ),
)


class QualificationError(RuntimeError):
    """Safe failure that can be retained without protected Odoo values."""


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connections-file", type=Path, required=True)
    parser.add_argument("--source-index", type=int, default=3)
    parser.add_argument("--destination-index", type=int, default=1)
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=ROOT / "build" / "acceptance" / "odoo19-product-create-hook",
    )
    return parser.parse_args()


def _executor(connection: tuple[str, str, str]) -> Json2WriteExecutor:
    base_url, database, api_key = connection
    return Json2WriteExecutor(
        Json2Config(
            base_url,
            database,
            api_key,
            retries=0,
            context={
                "mail_create_nosubscribe": True,
                "mail_notrack": True,
                "tracking_disable": True,
            },
        ),
        PRODUCT_SCOPE,
    )


def _unlink(executor: Json2WriteExecutor, identifiers: Iterable[int]) -> bool:
    exact_ids = tuple(dict.fromkeys(int(item) for item in identifiers if item))
    if not exact_ids:
        return True
    config = executor.config
    url = f"{config.base_url}/json/2/{quote(PRODUCT_MODEL, safe='.')}/unlink"
    headers = {
        "Authorization": f"bearer {config.api_key}",
        "Content-Type": "application/json; charset=utf-8",
        "X-Odoo-Database": config.database,
        "User-Agent": "impodo-product-create-hook-qualification",
    }
    try:
        status, response = executor.transport(
            url,
            headers,
            canonical_json_bytes({"ids": list(exact_ids), "context": {}}),
            config.timeout_seconds,
            "POST",
        )
    except Exception:
        return False
    return status == 200 and response is True


def qualify(args: argparse.Namespace) -> dict[str, object]:
    connections = _connections(args.connections_file)
    if (
        args.source_index == args.destination_index
        or not 1 <= args.source_index <= len(connections)
        or not 1 <= args.destination_index <= len(connections)
    ):
        raise QualificationError("Choose two distinct Odoo connections")

    source = connections[args.source_index - 1]
    destination = connections[args.destination_index - 1]
    executor = _executor(destination)
    source_reader = Json2ReadConnector(
        Json2Config(source[0], source[1], source[2], retries=0)
    )
    destination_reader = Json2ReadConnector(
        Json2Config(destination[0], destination[1], destination[2], retries=0)
    )
    if source_reader.get_target_fingerprint().target_hash == executor.target_hash:
        raise QualificationError("Source and destination resolve to the same target")

    result: dict[str, object] = {
        "contract": "impodo-odoo19-product-create-hook-v1",
        "started_at": datetime.now(UTC).isoformat(),
        "source_read_only": True,
        "destination_synthetic": True,
    }
    token = uuid4().hex
    reference = f"IMPODO-QA-PRODUCT-{token}"
    created: tuple[int, ...] = ()
    phase = "read_only_matching"
    try:
        matching = qualify_matching(
            args.connections_file,
            args.source_index,
            args.destination_index,
            PRODUCT_MODEL,
            ("default_code", "name"),
        )
        if "write_field_blockers_after_default_review=none" not in matching:
            raise QualificationError(
                "The live Product create-field plan remained blocked after review"
            )
        result["read_only_matching_ready_after_review"] = True

        phase = "create_synthetic_product"
        if executor.find_ids(PRODUCT_MODEL, (("default_code", "=", reference),)):
            raise QualificationError("The unique Product qualification key already exists")
        created = executor.create_rows(
            PRODUCT_MODEL,
            ({"default_code": reference, "name": "Impodo synthetic Product"},),
        )
        if len(created) != 1:
            raise QualificationError("Odoo did not return one Product receipt")

        phase = "verify_generated_variant"
        snapshot = destination_reader.get_records(
            (
                RecordRequest(
                    PRODUCT_MODEL,
                    ("default_code", "product_variant_ids"),
                    domain=(("id", "=", created[0]),),
                    limit=1,
                ),
            )
        )
        rows = snapshot.records.get(PRODUCT_MODEL, ())
        variants = rows[0].values.get("product_variant_ids") if len(rows) == 1 else None
        if (
            not isinstance(variants, (list, tuple))
            or len(variants) != 1
            or type(variants[0]) is not int
        ):
            raise QualificationError(
                "Odoo did not generate exactly one Product variant"
            )
        result["created_products"] = 1
        result["generated_variants"] = 1
        result["status"] = "PASSED"
    except Exception as error:
        result["status"] = "FAILED"
        result["phase"] = phase
        result["error_type"] = type(error).__name__
        if isinstance(error, QualificationError):
            result["error_detail"] = str(error)
    finally:
        exact_ids = executor.find_ids(
            PRODUCT_MODEL, (("default_code", "=", reference),)
        )
        result["cleanup"] = _unlink(executor, exact_ids)
        if result.get("status") == "PASSED" and not result["cleanup"]:
            result["status"] = "CLEANUP_FAILED"
        result["completed_at"] = datetime.now(UTC).isoformat()
    return result


def main() -> int:
    args = _arguments()
    args.output_directory.mkdir(parents=True, exist_ok=True)
    result_path = args.output_directory / "result.json"
    try:
        result = qualify(args)
    except Exception as error:
        result = {
            "contract": "impodo-odoo19-product-create-hook-v1",
            "status": "FAILED",
            "error_type": type(error).__name__,
            "completed_at": datetime.now(UTC).isoformat(),
        }
    result_path.write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
