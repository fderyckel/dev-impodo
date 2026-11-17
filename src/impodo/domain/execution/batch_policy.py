"""Validate the request size chosen for a reviewed Odoo load."""

from __future__ import annotations

from impodo.domain.execution.models import MAX_CREATE_BATCH_ROWS
from impodo.domain.workspace.errors import WorkspaceError


def validated_create_batch_rows(value: int | str) -> int:
    """Return one bounded Odoo request size chosen for this load run."""

    if isinstance(value, bool):
        raise WorkspaceError(
            "Rows per Odoo batch must be a whole number",
            load_failure_code="LOAD_ROWS_PER_ODOO_BATCH_MUST_BE_A_WHOLE_NUMBER",
        )
    try:
        batch_rows = int(value)
    except (TypeError, ValueError) as error:
        raise WorkspaceError(
            "Rows per Odoo batch must be a whole number",
            load_failure_code="LOAD_ROWS_PER_ODOO_BATCH_MUST_BE_A_WHOLE_NUMBER",
        ) from error
    if str(value).strip() != str(batch_rows):
        raise WorkspaceError(
            "Rows per Odoo batch must be a whole number",
            load_failure_code="LOAD_ROWS_PER_ODOO_BATCH_MUST_BE_A_WHOLE_NUMBER",
        )
    if batch_rows < 1 or batch_rows > MAX_CREATE_BATCH_ROWS:
        raise WorkspaceError(
            f"Rows per Odoo batch must be between 1 and {MAX_CREATE_BATCH_ROWS}",
            load_failure_code="LOAD_ROWS_PER_ODOO_BATCH_MUST_BE_BETWEEN_1_AND_CONFIGURED_CONTEXT",
        )
    return batch_rows
