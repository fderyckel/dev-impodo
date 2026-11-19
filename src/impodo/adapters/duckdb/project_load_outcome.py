"""Read compact journal totals and the bound destination of the selected load."""

from datetime import datetime

import duckdb

from impodo.application.workspace.navigation import ProjectLoadOutcome
from impodo.domain.execution.models import ExecutionRowStatus
from impodo.domain.odoo.company import OdooCompanySelection


def read_project_load_outcome(
    connection: duckdb.DuckDBPyConnection,
    run_id: str,
    *,
    company_binding_hash: str,
    read_context_hash: str,
    include_counts: bool,
) -> ProjectLoadOutcome | None:
    """Bind all displayed values to the exact run without reading saved row JSON."""

    row = connection.execute(
        """
        SELECT run.target_database, run.target_hash, run.write_context_hash,
               check_run.verified_at, company.selection_json
          FROM execution_run AS run
          JOIN execution_current AS current ON current.run_id = run.run_id
          LEFT JOIN reconciliation_current AS latest ON latest.singleton_id = 1
          LEFT JOIN reconciliation_run AS check_run
            ON check_run.reconciliation_id = latest.reconciliation_id
           AND check_run.execution_run_id = run.run_id
          LEFT JOIN odoo_company_binding AS company ON company.binding_hash = ?
         WHERE current.singleton_id = 1 AND run.run_id = ?
        """,
        [company_binding_hash, run_id],
    ).fetchone()
    if row is None:
        return None
    company_name = ""
    if row[4]:
        selection = OdooCompanySelection.from_json(row[4])
        if (
            selection.binding_hash == company_binding_hash
            and selection.target_hash == row[1]
            and selection.context_hash == (row[2] or read_context_hash)
        ):
            company_name = selection.name

    counts = ()
    if include_counts:
        grouped = connection.execute(
            "SELECT status, count(*) FROM execution_row WHERE run_id = ? GROUP BY status",
            [run_id],
        ).fetchall()
        counts = tuple((ExecutionRowStatus(status), count) for status, count in grouped)
    return ProjectLoadOutcome(
        database=row[0],
        company_name=company_name,
        verified_at=datetime.fromisoformat(row[3]) if row[3] else None,
        journal_counts=counts,
    )
