"""Install request-local database-generation fences on Odoo-source adapters.

Original deployment callables are retained once. Each operation asks composition
for one frozen, authorized binding assertion before entering a delegate factory;
the assertion releases its local owners before any remote call or page yield.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from impodo.application.odoo_capture_generation_fence import (
    GenerationFencedOdooSourceCaptureGateway,
)
from impodo.application.odoo_generation_fence import (
    GenerationFencedOdooReadbackReader,
    GenerationFencedOdooWriteExecutor,
)
from impodo.application.odoo_source_capture_service import OdooSourceCapturePort
from impodo.domain.execution.odoo_readback import OdooReadbackReader
from impodo.domain.execution.odoo_scope import OdooApiScope
from impodo.domain.execution.odoo_write import OdooWriteExecutor
from impodo.domain.odoo.contracts import (
    MetadataRequest,
    MetadataSnapshot,
    RecordRequest,
    RecordSnapshot,
)
from impodo.domain.odoo_database_binding import (
    OdooDatabaseBindingRole,
    database_scope_hash,
)
from impodo.domain.shared.models import OdooReadIdentity, OdooWriteIdentity
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import SourceMode, WorkspaceState
from impodo.web.context import (
    DestinationMatchingReader,
    ModelCatalogReader,
    OdooReadbackReaderFactory,
    OdooSourceCaptureFactory,
    OdooWriteExecutorFactory,
    ReadIdentityProbe,
    SchemaReader,
    SchemaReadSession,
    SchemaReadSessionFactory,
    WriteIdentityProbe,
)

type BindingGuardFactory = Callable[
    [WorkspaceState, OdooDatabaseBindingRole], Callable[[], None]
]


class DatabaseFenceContext(Protocol):
    """Only replace the deployment's closed Odoo adapter seams."""

    read_identity_probe: ReadIdentityProbe
    write_identity_probe: WriteIdentityProbe
    schema_reader: SchemaReader
    schema_read_session_factory: SchemaReadSessionFactory | None
    model_catalog_reader: ModelCatalogReader
    destination_match_reader: DestinationMatchingReader
    source_capture_factory: OdooSourceCaptureFactory
    write_executor_factory: OdooWriteExecutorFactory
    readback_reader_factory: OdooReadbackReaderFactory


_BINDING_REQUIRED = (
    "The reviewed Odoo database binding could not be confirmed. Prepare a fresh "
    "source or restore workspace for the declared current database before continuing."
)


def _check(guard: Callable[[], None]) -> None:
    try:
        guard()
    except Exception as error:
        raise WorkspaceError(_BINDING_REQUIRED) from error


def _checked[T](guard: Callable[[], None], call: Callable[[], T]) -> T:
    _check(guard)
    result = call()
    _check(guard)
    return result


def _role(workspace: WorkspaceState) -> OdooDatabaseBindingRole:
    if (
        workspace.odoo_base_url
        and workspace.odoo_database
        and workspace.destination_odoo_base_url
        and workspace.destination_odoo_database
        and database_scope_hash(workspace.odoo_base_url, workspace.odoo_database)
        == database_scope_hash(
            workspace.destination_odoo_base_url, workspace.destination_odoo_database
        )
    ):
        return OdooDatabaseBindingRole.DESTINATION
    return OdooDatabaseBindingRole.SOURCE


@dataclass(frozen=True, slots=True)
class _FencedSchemaReadSession:
    delegate: SchemaReadSession
    guard: Callable[[], None]

    def read_models(self, models: tuple[str, ...]) -> MetadataSnapshot:
        return _checked(self.guard, lambda: self.delegate.read_models(models))

    def read_model_catalog(self) -> RecordSnapshot:
        return _checked(self.guard, self.delegate.read_model_catalog)

    def verify_identity(self, models: tuple[str, ...]) -> OdooReadIdentity | None:
        return _checked(self.guard, lambda: self.delegate.verify_identity(models))


def install_odoo_database_fences(
    context: DatabaseFenceContext, guard_factory: BindingGuardFactory
) -> None:
    """Fence only Odoo-source transfers; ordinary File/Recipe seams stay direct."""

    original_read_identity = context.read_identity_probe
    original_write_identity = context.write_identity_probe
    original_schema = context.schema_reader
    original_session = context.schema_read_session_factory
    original_catalog = context.model_catalog_reader
    original_matching = context.destination_match_reader
    original_capture = context.source_capture_factory
    original_writer = context.write_executor_factory
    original_readback = context.readback_reader_factory

    def frozen_guard(
        workspace: WorkspaceState, role: OdooDatabaseBindingRole
    ) -> Callable[[], None]:
        try:
            return guard_factory(workspace, role)
        except Exception as error:
            raise WorkspaceError(_BINDING_REQUIRED) from error

    def read_identity(
        workspace: WorkspaceState, secret: str, models: tuple[str, ...]
    ) -> OdooReadIdentity:
        if workspace.source_mode is not SourceMode.ODOO:
            return original_read_identity(workspace, secret, models)
        guard = frozen_guard(workspace, _role(workspace))
        return _checked(
            guard, lambda: original_read_identity(workspace, secret, models)
        )

    def write_identity(
        workspace: WorkspaceState, secret: str, scope: OdooApiScope
    ) -> OdooWriteIdentity:
        if workspace.source_mode is not SourceMode.ODOO:
            return original_write_identity(workspace, secret, scope)
        guard = frozen_guard(workspace, OdooDatabaseBindingRole.DESTINATION)
        return _checked(
            guard, lambda: original_write_identity(workspace, secret, scope)
        )

    def schema(workspace: WorkspaceState, secret: str) -> MetadataSnapshot:
        if workspace.source_mode is not SourceMode.ODOO:
            return original_schema(workspace, secret)
        guard = frozen_guard(workspace, _role(workspace))
        return _checked(guard, lambda: original_schema(workspace, secret))

    def catalog(workspace: WorkspaceState, secret: str) -> RecordSnapshot:
        if workspace.source_mode is not SourceMode.ODOO:
            return original_catalog(workspace, secret)
        guard = frozen_guard(workspace, _role(workspace))
        return _checked(guard, lambda: original_catalog(workspace, secret))

    def matching(
        workspace: WorkspaceState,
        secret: str,
        metadata: tuple[MetadataRequest, ...],
        records: tuple[RecordRequest, ...],
    ) -> tuple[MetadataSnapshot, RecordSnapshot]:
        if workspace.source_mode is not SourceMode.ODOO:
            return original_matching(workspace, secret, metadata, records)
        guard = frozen_guard(workspace, OdooDatabaseBindingRole.DESTINATION)
        return _checked(
            guard, lambda: original_matching(workspace, secret, metadata, records)
        )

    def capture(workspace: WorkspaceState, secret: str) -> OdooSourceCapturePort:
        if workspace.source_mode is not SourceMode.ODOO:
            return original_capture(workspace, secret)
        guard = frozen_guard(workspace, OdooDatabaseBindingRole.SOURCE)
        delegate = _checked(guard, lambda: original_capture(workspace, secret))
        return GenerationFencedOdooSourceCaptureGateway(delegate, guard)

    def writer(
        workspace: WorkspaceState, secret: str, scope: OdooApiScope
    ) -> OdooWriteExecutor:
        if workspace.source_mode is not SourceMode.ODOO:
            return original_writer(workspace, secret, scope)
        guard = frozen_guard(workspace, OdooDatabaseBindingRole.DESTINATION)
        delegate = _checked(guard, lambda: original_writer(workspace, secret, scope))
        return GenerationFencedOdooWriteExecutor(delegate, guard)

    def readback(
        workspace: WorkspaceState, secret: str, scope: OdooApiScope
    ) -> OdooReadbackReader:
        if workspace.source_mode is not SourceMode.ODOO:
            return original_readback(workspace, secret, scope)
        guard = frozen_guard(workspace, OdooDatabaseBindingRole.DESTINATION)
        delegate = _checked(guard, lambda: original_readback(workspace, secret, scope))
        return GenerationFencedOdooReadbackReader(delegate, guard)

    context.read_identity_probe = read_identity
    context.write_identity_probe = write_identity
    context.schema_reader = schema
    context.model_catalog_reader = catalog
    context.destination_match_reader = matching
    context.source_capture_factory = capture
    context.write_executor_factory = writer
    context.readback_reader_factory = readback
    if original_session is not None:

        def session(workspace: WorkspaceState, secret: str) -> SchemaReadSession:
            if workspace.source_mode is not SourceMode.ODOO:
                return original_session(workspace, secret)
            guard = frozen_guard(workspace, _role(workspace))
            delegate = _checked(guard, lambda: original_session(workspace, secret))
            return _FencedSchemaReadSession(delegate, guard)

        context.schema_read_session_factory = session
