"""Check captured related IDs against Odoo's company domains in bounded groups.

See docs/developer/contracts/preflight.md.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from impodo.domain.odoo.company import CompanyReferenceCheck
from impodo.domain.odoo.company_domains import operation_company_domain
from impodo.domain.odoo.contracts import (
    ConnectorIncompleteResultError,
    MetadataSnapshot,
    RecordSnapshot,
)


def capture_company_checks(
    metadata: MetadataSnapshot,
    records: RecordSnapshot,
    domains: Mapping[tuple[str, str], object],
    company_id: int,
    post: Callable[[str, str, Mapping[str, Any]], Any],
) -> MetadataSnapshot:
    """Reuse complete record evidence; read only exact IDs once per distinct domain."""
    checked: dict[tuple[str, str], tuple[int, ...]] = {}
    checks = []
    limitations = list(metadata.limitations)
    for model_name, model in sorted(metadata.models.items()):
        for name, field in sorted(model.fields.items()):
            if (
                field.type != "many2one"
                or field.company_dependent is not True
                or not field.relation
            ):
                continue
            domain = operation_company_domain(
                domains.get((model_name, name)), company_id
            )
            if domain is None:
                limitations.append(
                    f"Company compatibility remains checked by Odoo for {model_name}.{name}"
                )
                continue
            key = (
                field.relation,
                json.dumps(domain, sort_keys=True, separators=(",", ":")),
            )
            if key not in checked:
                identifiers = sorted(
                    {
                        record.odoo_id
                        for record in records.records.get(field.relation, ())
                    }
                )
                compatible: set[int] = set()
                for start in range(0, len(identifiers), 500):
                    page = identifiers[start : start + 500]
                    rows = post(
                        field.relation,
                        "search_read",
                        {
                            "domain": [["id", "in", page], *domain],
                            "fields": ["id"],
                            "order": "id asc",
                            "limit": len(page) + 1,
                            "context": {
                                "allowed_company_ids": [company_id],
                                "active_test": False,
                            },
                        },
                    )
                    if not isinstance(rows, list) or len(rows) > len(page):
                        raise ConnectorIncompleteResultError(
                            "Odoo company compatibility evidence is incomplete"
                        )
                    found = []
                    for row in rows:
                        identifier = row.get("id") if isinstance(row, Mapping) else None
                        if type(identifier) is not int or identifier not in page:
                            raise ConnectorIncompleteResultError(
                                "Odoo company compatibility returned unexpected records"
                            )
                        found.append(identifier)
                    if len(found) != len(set(found)):
                        raise ConnectorIncompleteResultError(
                            "Odoo company compatibility returned duplicate records"
                        )
                    compatible.update(found)
                checked[key] = tuple(sorted(set(identifiers) - compatible))
            checks.append(
                CompanyReferenceCheck(model_name, name, field.relation, checked[key])
            )
    return replace(
        metadata,
        company_checks=tuple(checks),
        limitations=tuple(dict.fromkeys(limitations)),
        content_hash=None,
    )
