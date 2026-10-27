"""Bind Linux storage facts to the deployment-neutral server runtime."""

from __future__ import annotations

from pathlib import PurePosixPath

from impodo.adapters.postgresql.startup import (
    PostgreSQLDsnError,
    PostgreSQLSchemaCompatibilityError,
    PostgreSQLStartupSessionFactory,
    PostgreSQLUnavailableError,
    PsycopgPostgreSQLStartupInspector,
    open_psycopg_startup_session,
)
from impodo.adapters.secrets.linux_files import (
    LinuxFileDescriptorOperations,
    LinuxSecretFileError,
    LinuxSecretFileReader,
)
from impodo.web.composition.server_config import (
    SERVER_GID,
    SecretProviderKind,
    ServerConfigurationError,
    ServerSettings,
)
from impodo.web.composition.server_lifecycle import ServerLifecycleOwner
from impodo.web.composition.server_linux_storage import (
    observe_linux_server_storage,
)
from impodo.web.composition.server_runtime import (
    ServerPostgreSQLInspector,
    ServerPostgreSQLProbeError,
    ServerRuntimeCoordinator,
    ServerRuntimePorts,
    ServerSchemaProbeError,
)


class _RuntimePostgreSQLInspector:
    def __init__(self, inspector: PsycopgPostgreSQLStartupInspector) -> None:
        self._inspector = inspector

    def __call__(self, settings: ServerSettings) -> None:
        try:
            self._inspector.inspect(settings.postgres_dsn_file)
        except PostgreSQLUnavailableError as error:
            raise ServerPostgreSQLProbeError(
                "PostgreSQL startup probe failed"
            ) from error
        except PostgreSQLSchemaCompatibilityError as error:
            raise ServerSchemaProbeError("PostgreSQL schema probe failed") from error


def create_linux_server_runtime(
    settings: ServerSettings,
    *,
    postgresql_inspector: ServerPostgreSQLInspector,
    lifecycle_owner: ServerLifecycleOwner | None = None,
) -> ServerRuntimeCoordinator:
    """Create one Linux runtime without supplying fake dependency fallbacks."""

    return ServerRuntimeCoordinator(
        settings,
        ServerRuntimePorts(
            storage_observer=observe_linux_server_storage,
            postgresql_inspector=postgresql_inspector,
        ),
        lifecycle_owner=lifecycle_owner,
    )


def create_file_secret_linux_server_runtime(
    settings: ServerSettings,
    *,
    lifecycle_owner: ServerLifecycleOwner | None = None,
    secret_file_operations: LinuxFileDescriptorOperations | None = None,
    postgresql_session_factory: PostgreSQLStartupSessionFactory = (
        open_psycopg_startup_session
    ),
) -> ServerRuntimeCoordinator:
    """Bind secure file secrets and Psycopg to one Linux server runtime."""

    secret_root = settings.secret_provider.root
    if (
        settings.secret_provider.kind is not SecretProviderKind.FILES
        or secret_root is None
    ):
        raise ServerConfigurationError(
            "The Linux file-secret runtime requires the files secret provider"
        )
    secret_reader = LinuxSecretFileReader(
        expected_gid=SERVER_GID,
        operations=secret_file_operations,
    )

    def read_dsn(reference: PurePosixPath) -> str:
        try:
            return secret_reader.read_text(secret_root, reference)
        except LinuxSecretFileError as error:
            raise PostgreSQLDsnError(
                "PostgreSQL DSN file could not be resolved"
            ) from error

    inspector = PsycopgPostgreSQLStartupInspector(
        read_dsn,
        session_factory=postgresql_session_factory,
    )
    return create_linux_server_runtime(
        settings,
        postgresql_inspector=_RuntimePostgreSQLInspector(inspector),
        lifecycle_owner=lifecycle_owner,
    )


__all__ = [
    "create_file_secret_linux_server_runtime",
    "create_linux_server_runtime",
]
