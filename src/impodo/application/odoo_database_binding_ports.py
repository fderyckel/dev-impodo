"""Registry ports for Organization-wide database declarations and workspace proof."""

from typing import Protocol

from impodo.domain.odoo_database_binding import (
    OdooDatabaseBinding,
    OdooDatabaseBindingRole,
    OdooDatabaseGeneration,
)


class OdooDatabaseBindingRepository(Protocol):
    """Keep each bounded ledger command atomic and free of remote Odoo work."""

    def get(
        self, workspace_id: str, role: OdooDatabaseBindingRole, scope_hash: str
    ) -> OdooDatabaseBinding | None: ...

    def current(
        self, organization_id: str, scope_hash: str
    ) -> OdooDatabaseGeneration: ...

    def bind(
        self, binding: OdooDatabaseBinding, *, expected_generation_hash: str
    ) -> OdooDatabaseBinding: ...

    def declare(
        self, generation: OdooDatabaseGeneration, *, expected_generation_hash: str
    ) -> OdooDatabaseGeneration: ...

    def assert_current(
        self,
        workspace_id: str,
        role: OdooDatabaseBindingRole,
        scope_hash: str,
        *,
        project_id: str,
    ) -> OdooDatabaseBinding: ...

    def workspace_binding_status(
        self,
        workspace_id: str,
        project_id: str,
        role: OdooDatabaseBindingRole,
        scope_hash: str,
    ) -> tuple[OdooDatabaseBinding | None, str | None]: ...
