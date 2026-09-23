"""Shared bounded source-capture probe for Odoo 20 qualification runners."""

from __future__ import annotations

from dataclasses import replace


_PLACEHOLDER_HASH = "sha256:" + "0" * 64


def qualify_bounded_source_capture(config) -> dict[str, int]:
    """Exercise the production JSON-2 capture adapter without exposing values."""

    from impodo.adapters.odoo_source_capture import Json2OdooSourceCapture
    from impodo.domain.odoo_capture import (
        OdooCaptureConsistency,
        OdooCaptureFilterClause,
        OdooCaptureFilterOperator,
        OdooCaptureFilterPolicy,
    )
    from impodo.domain.odoo_source_capture import (
        OdooCaptureFieldProjection,
        OdooSourceCaptureRequest,
    )
    from impodo.domain.odoo_source_policy import (
        ODOO_SOURCE_POLICIES,
        ODOO_SOURCE_POLICY_HASHES,
    )

    policy = ODOO_SOURCE_POLICIES[20]
    request = OdooSourceCaptureRequest(
        data_version_id="00000000-0000-0000-0000-000000000020",
        selection_id="00000000-0000-0000-0000-000000000021",
        selection_version=1,
        selection_hash=_PLACEHOLDER_HASH,
        policy_hash=ODOO_SOURCE_POLICY_HASHES[20],
        model="res.country",
        projection=(
            OdooCaptureFieldProjection("code", "char"),
            OdooCaptureFieldProjection("name", "char"),
        ),
        filter_clauses=(
            OdooCaptureFilterClause(
                field_name="code",
                operator=OdooCaptureFilterOperator.IN_SET,
                values=("BE", "US"),
            ),
        ),
        filter_policy=OdooCaptureFilterPolicy.ALL_MATCHING_RECORDS,
        schema_model_names=("res.country",),
        maximum_rows=10,
        page_size=10,
        max_sample_rows=policy.max_sample_rows,
        max_request_bytes=policy.max_request_bytes,
        max_response_bytes=policy.max_response_bytes,
        max_value_bytes=policy.max_value_bytes,
        max_row_bytes=policy.max_row_bytes,
        max_snapshot_bytes=policy.max_snapshot_bytes,
        expected_connection_target_hash=_PLACEHOLDER_HASH,
        expected_schema_scope_hash=_PLACEHOLDER_HASH,
        expected_read_principal_hash=_PLACEHOLDER_HASH,
        expected_read_permission_hash=_PLACEHOLDER_HASH,
        expected_context_hash=_PLACEHOLDER_HASH,
        consistency=OdooCaptureConsistency.KEYSET_HIGH_WATER_INTERVAL,
        target_instance_assurance=policy.target_instance_assurance,
    )
    adapter = Json2OdooSourceCapture(config)
    identity, context = adapter.probe_identity(request)
    request = replace(
        request,
        expected_connection_target_hash=identity.target_hash,
        expected_read_principal_hash=identity.principal_hash,
        expected_read_permission_hash=identity.permission_hash,
        expected_context_hash=identity.context_hash,
    )
    metadata = adapter.probe_schema(request, context)
    country_fields = metadata.models["res.country"].fields
    if (
        country_fields.get("code") is None
        or country_fields["code"].type != "char"
        or country_fields.get("name") is None
        or country_fields["name"].type != "char"
    ):
        raise RuntimeError("The bounded source-capture field contract changed")
    sample = adapter.sample(request, context, limit=2)
    if sample.page is None or sample.page.row_count != 2:
        raise RuntimeError("The bounded source-capture sample did not return two rows")
    session = adapter.open_capture(request, context)
    pages = tuple(session.pages())
    accounting = session.accounting
    if accounting.row_count != 2 or sum(page.row_count for page in pages) != 2:
        raise RuntimeError("The bounded source capture did not freeze two rows")
    return {
        "matching_rows": session.matching_rows,
        "captured_rows": accounting.row_count,
        "page_count": accounting.page_count,
        "record_request_count": accounting.record_request_count,
    }
