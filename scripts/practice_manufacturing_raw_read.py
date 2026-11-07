"""Closed qualification read of actual template inverse variant links.

Odoo search_read removes active_test before formatting relational values in
both reviewed majors. An archived template's inverse can therefore look empty
even when its archived variant exists. Read the exact discovered template IDs
through native read(active_test=False), then independently cross-check forward
variant ownership in ManufacturingProjection. No production allowlist changes.
"""

from dataclasses import replace

from impodo.domain.serialization import canonical_json
from scripts.practice_customer_seed import require

_FIELDS = ("default_code", "active", "product_variant_ids")
_CODES = frozenset(f"MA-P-{number:04d}" for number in range(1, 33))


def read_template_variant_links(connector, records, *, major):
    """Replace only inverse values with independently validated native reads.

    The caller already selected this allocation's template namespace through a
    bounded standard read. No arbitrary model, method, fields, domain or IDs are
    accepted here. Existing private authentication/transport limits are reused.
    """
    require(major in (19, 20), "EXACT_SUPPORTED_MAJOR_REQUIRED")
    rows = tuple(records)
    if not rows:
        return rows
    require(len(rows) <= 32, "EXACT_VARIANT_READ_SCOPE_REQUIRED")
    identifiers = tuple(row.odoo_id for row in rows)
    require(
        len(set(identifiers)) == len(rows)
        and all(
            type(identifier) is int and identifier > 0 for identifier in identifiers
        )
        and all(
            row.model == "product.template"
            and set(_FIELDS) <= set(row.values)
            and row.values["default_code"] in _CODES
            and type(row.values["active"]) is bool
            for row in rows
        ),
        "EXACT_VARIANT_READ_SCOPE_REQUIRED",
    )
    config = connector._config
    status, response = connector._request_url(
        config.base_url.rstrip("/") + "/json/2/product.template/read",
        method="POST",
        body=canonical_json(
            {
                "ids": list(identifiers),
                "fields": list(_FIELDS),
                "context": {**config.context, "active_test": False},
            }
        ).encode("utf-8"),
    )
    require(status == 200 and isinstance(response, list), "EXACT_VARIANT_READ_FAILED")
    require(len(response) == len(rows), "EXACT_VARIANT_READ_INCOMPLETE")
    by_id = {}
    for item in response:
        require(
            isinstance(item, dict)
            and set(item) == {"id", *_FIELDS}
            and type(item["id"]) is int
            and item["id"] in identifiers
            and item["id"] not in by_id,
            "EXACT_VARIANT_READ_IDENTITIES_DIFFERED",
        )
        values = item["product_variant_ids"]
        require(
            isinstance(values, list)
            and len(values) == 1
            and all(
                type(identifier) is int and identifier > 0 for identifier in values
            ),
            "SINGLE_GENERATED_VARIANT_REQUIRED",
        )
        by_id[item["id"]] = item
    result = []
    for row in rows:
        item = by_id[row.odoo_id]
        require(
            item["default_code"] == row.values["default_code"]
            and type(item["active"]) is bool
            and item["active"] is row.values["active"],
            "EXACT_VARIANT_READ_BINDING_DIFFERED",
        )
        result.append(
            replace(
                row,
                values={
                    **row.values,
                    "product_variant_ids": item["product_variant_ids"],
                },
            )
        )
    return tuple(result)
