"""Read company choices through a closed adapter outside local database scopes.

See docs/developer/workflow/02-odoo-data.md.
"""

from impodo.domain.odoo.company import OdooCompanyAccess
from impodo.domain.workspace.workbench import WorkspaceState

from .target_readers import _company_choice_connector


def read_company_access(state: WorkspaceState, api_key: str) -> OdooCompanyAccess:
    """Discover only companies available to the current checking user."""
    return _company_choice_connector(state, api_key).get_company_access()
