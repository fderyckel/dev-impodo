"""Bind Linux secrets to the private PostgreSQL migration command."""

from __future__ import annotations

from pathlib import PurePosixPath

from impodo.adapters.postgresql.migrations import (
    PostgreSQLMigrationExecutionError,
    PostgreSQLMigrationIncompatibleError,
    PostgreSQLMigrationResult,
    PostgreSQLMigrationSessionFactory,
    PostgreSQLMigrationUnavailableError,
    PsycopgPostgreSQLMigrator,
    open_psycopg_migration_session,
)
from impodo.adapters.postgresql.startup import PostgreSQLDsnError
from impodo.adapters.secrets.linux_files import (
    LinuxFileDescriptorOperations,
    LinuxSecretFileError,
    LinuxSecretFileReader,
)
from impodo.web.composition.server_config import (
    SERVER_GID,
    SecretProviderKind,
    ServerConfigurationError,
    ServerMigrationSettings,
)


class ServerMigrationUnavailableError(RuntimeError):
    """Report a retryable private migration failure."""


class ServerMigrationIncompatibleError(RuntimeError):
    """Report a migration history that this build cannot change safely."""


class ServerMigrationExecutionError(RuntimeError):
    """Report a non-retryable private migration execution failure."""


def run_file_secret_linux_postgresql_migrations(
    settings: ServerMigrationSettings,
    *,
    secret_file_operations: LinuxFileDescriptorOperations | None = None,
    postgresql_session_factory: PostgreSQLMigrationSessionFactory = (
        open_psycopg_migration_session
    ),
) -> PostgreSQLMigrationResult:
    """Run one atomic migration transaction from a privileged file secret."""

    secret_root = settings.secret_provider.root
    if (
        settings.secret_provider.kind is not SecretProviderKind.FILES
        or secret_root is None
    ):
        raise ServerConfigurationError(
            "The Linux migration command requires the files secret provider"
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
                "PostgreSQL migration DSN file could not be resolved"
            ) from error

    migrator = PsycopgPostgreSQLMigrator(
        read_dsn,
        session_factory=postgresql_session_factory,
    )
    try:
        return migrator.migrate(settings.postgres_migration_dsn_file)
    except PostgreSQLMigrationUnavailableError as error:
        raise ServerMigrationUnavailableError(
            "PostgreSQL migration service is unavailable"
        ) from error
    except PostgreSQLMigrationIncompatibleError as error:
        raise ServerMigrationIncompatibleError(
            "PostgreSQL migration history is incompatible"
        ) from error
    except PostgreSQLMigrationExecutionError as error:
        raise ServerMigrationExecutionError(
            "PostgreSQL migration execution failed"
        ) from error


__all__ = [
    "ServerMigrationExecutionError",
    "ServerMigrationIncompatibleError",
    "ServerMigrationUnavailableError",
    "run_file_secret_linux_postgresql_migrations",
]
