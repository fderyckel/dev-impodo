"""Display reviewed relationship identities without changing source link keys.

Incoming references identify prepared rows by their source identity. A review
workbook shows the business identity of the uniquely resolved row instead,
using only the saved comparison and its matching frozen preparation evidence.
"""

from collections.abc import Mapping
from typing import Any

from impodo.domain.shared.models import (
    BusinessReference,
    LogicalReference,
    PreparedRecord,
    canonical_json_bytes,
    portable_value,
    restore_portable_value,
)


class ReviewRelationshipValues:
    """Index successful reviewed links for one workbook's display only."""

    def __init__(
        self,
        manifest: Mapping[str, Any],
        prepared_by_trace: Mapping[str, PreparedRecord],
    ) -> None:
        decisions = {
            str(item.get("source_trace_id") or ""): item
            for item in manifest.get("decisions", ())
        }
        incoming: dict[tuple[str, bytes], BusinessReference | None] = {}
        for trace, record in prepared_by_trace.items():
            decision = decisions.get(trace)
            if decision is None or decision.get("classification") in {
                "BLOCKED",
                "AMBIGUOUS",
            }:
                continue
            key = (
                record.dataset,
                canonical_json_bytes(portable_value(record.source_identity)),
            )
            reference = BusinessReference(
                model=record.target_model,
                key=restore_portable_value(decision.get("business_identity", ())),
                scope=restore_portable_value(decision.get("business_scope", ())),
            )
            incoming[key] = None if key in incoming else reference

        self._resolved: dict[tuple[str, str, bytes], BusinessReference | None] = {}
        for item in manifest.get("reference_resolutions", ()):
            if item.get("match_count") != 1:
                continue
            reference = restore_portable_value(item.get("reference"))
            if not isinstance(reference, LogicalReference):
                continue
            status = item.get("status")
            resolved = None
            if (reference.origin == "incoming" and status == "RESOLVED") or (
                reference.origin == "target_then_incoming"
                and status == "RESOLVED_INCOMING"
            ):
                source_key = (
                    reference.incoming_key
                    if reference.origin == "target_then_incoming"
                    else reference.key
                )
                if reference.dataset is not None and source_key is not None:
                    resolved = incoming.get(
                        (
                            reference.dataset,
                            canonical_json_bytes(portable_value(source_key)),
                        )
                    )
            elif status in {"RESOLVED", "RESOLVED_TARGET"}:
                candidate = restore_portable_value(item.get("resolved_target"))
                if isinstance(candidate, BusinessReference):
                    resolved = candidate
            if resolved is not None:
                key = (
                    str(item.get("dataset") or ""),
                    str(item.get("field") or ""),
                    canonical_json_bytes(portable_value(reference)),
                )
                previous = self._resolved.setdefault(key, resolved)
                if previous != resolved:
                    self._resolved[key] = None

    def resolve(self, dataset: str, field: str, value: Any) -> Any:
        """Return reviewed display values, retaining unresolved lookup keys."""
        if isinstance(value, tuple):
            return tuple(self.resolve(dataset, field, item) for item in value)
        if isinstance(value, LogicalReference):
            return (
                self._resolved.get(
                    (dataset, field, canonical_json_bytes(portable_value(value)))
                )
                or value
            )
        return value
