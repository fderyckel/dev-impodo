"""Bind Linux file secrets to non-HTTP Platform Administrator bootstrap."""

from __future__ import annotations

import getpass
from collections.abc import Callable
from pathlib import PurePosixPath

from impodo.adapters.identity.scrypt_passwords import ScryptPasswordHasher
from impodo.adapters.postgresql.platform_bootstrap import (
    PostgreSQLPlatformBootstrapSessionFactory,
    PsycopgPlatformAdministratorBootstrapRepository,
    open_psycopg_platform_bootstrap_session,
)
from impodo.adapters.postgresql.startup import PostgreSQLDsnError
from impodo.adapters.secrets.linux_files import (
    LinuxFileDescriptorOperations,
    LinuxSecretFileError,
    LinuxSecretFileReader,
)
from impodo.application.identity.bootstrap_ports import PasswordHasher
from impodo.application.identity.bootstrap_service import (
    PlatformAdministratorBootstrapService,
)
from impodo.domain.identity.bootstrap import (
    PlatformAdministratorBootstrapCommand,
    PlatformAdministratorBootstrapInputError,
    PlatformAdministratorBootstrapResult,
)
from impodo.web.composition.server_config import (
    SERVER_GID,
    SecretProviderKind,
    ServerAdminSettings,
    ServerConfigurationError,
)

InteractiveSecretReader = Callable[[str], str]


def _interactive_password(reader: InteractiveSecretReader) -> str:
    first = reader("Platform Administrator password: ")
    confirmation = reader("Confirm Platform Administrator password: ")
    if first != confirmation:
        raise PlatformAdministratorBootstrapInputError(
            "Bootstrap password confirmation does not match"
        )
    return first


def run_file_secret_linux_platform_admin_bootstrap(
    settings: ServerAdminSettings,
    *,
    login_email: str,
    display_name: str,
    password_file: PurePosixPath | None,
    secret_file_operations: LinuxFileDescriptorOperations | None = None,
    postgresql_session_factory: PostgreSQLPlatformBootstrapSessionFactory = (
        open_psycopg_platform_bootstrap_session
    ),
    interactive_secret_reader: InteractiveSecretReader = getpass.getpass,
    password_hasher: PasswordHasher | None = None,
) -> PlatformAdministratorBootstrapResult:
    """Read private inputs and run one atomic first-operator transaction."""

    secret_root = settings.secret_provider.root
    if (
        settings.secret_provider.kind is not SecretProviderKind.FILES
        or secret_root is None
    ):
        raise ServerConfigurationError(
            "The Linux admin command requires the files secret provider"
        )
    secret_reader = LinuxSecretFileReader(
        expected_gid=SERVER_GID,
        operations=secret_file_operations,
    )

    def read_secret(reference: PurePosixPath) -> str:
        try:
            return secret_reader.read_text(secret_root, reference)
        except LinuxSecretFileError as error:
            raise ServerConfigurationError(
                "A required admin secret file could not be resolved"
            ) from error

    def read_dsn(reference: PurePosixPath) -> str:
        try:
            return read_secret(reference)
        except ServerConfigurationError as error:
            raise PostgreSQLDsnError(
                "PostgreSQL admin DSN file could not be resolved"
            ) from error

    password = (
        read_secret(password_file)
        if password_file is not None
        else _interactive_password(interactive_secret_reader)
    )
    repository = PsycopgPlatformAdministratorBootstrapRepository(
        read_dsn,
        settings.postgres_dsn_file,
        session_factory=postgresql_session_factory,
    )
    service = PlatformAdministratorBootstrapService(
        repository,
        password_hasher or ScryptPasswordHasher(),
    )
    return service.bootstrap(
        PlatformAdministratorBootstrapCommand(
            login_email=login_email,
            display_name=display_name,
            password=password,
        ),
        deployment_id=settings.deployment_id,
    )


__all__ = [
    "InteractiveSecretReader",
    "run_file_secret_linux_platform_admin_bootstrap",
]
