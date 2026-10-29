"""Bind Linux file secrets to the server administration web journey."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from fastapi.templating import Jinja2Templates

from impodo.adapters.identity.scrypt_passwords import ScryptPasswordHasher
from impodo.adapters.identity.secure_recovery_tokens import (
    HmacIdentifierDigester,
    SecureRecoveryTokenFactory,
)
from impodo.adapters.identity.secure_sessions import SecureSessionSecretFactory
from impodo.adapters.postgresql.identity_administration import (
    PsycopgIdentityAdministrationRepository,
)
from impodo.adapters.postgresql.platform_authentication import (
    PsycopgPlatformAuthenticationRepository,
)
from impodo.adapters.postgresql.platform_sessions import (
    PsycopgServerSessionRepository,
)
from impodo.adapters.postgresql.startup import PostgreSQLDsnError
from impodo.adapters.secrets.linux_files import (
    LinuxFileDescriptorOperations,
    LinuxSecretFileError,
    LinuxSecretFileReader,
)
from impodo.application.identity.administration_service import (
    IdentityAdministrationService,
)
from impodo.application.identity.authentication_service import (
    PlatformAuthenticationService,
)
from impodo.application.identity.session_service import ServerSessionService
from impodo.domain.identity.sessions import ServerSessionPolicy
from impodo.web.composition.server_administration import (
    ServerAdministrationDependencies,
)
from impodo.web.composition.server_config import (
    SERVER_GID,
    SecretProviderKind,
    ServerConfigurationError,
    ServerSettings,
)

IDENTIFIER_DIGEST_KEY_FILENAME = "identifier-digest-key"


def create_file_secret_server_administration_dependencies(
    settings: ServerSettings,
    *,
    secret_file_operations: LinuxFileDescriptorOperations | None = None,
) -> ServerAdministrationDependencies:
    """Create service objects whose repositories open only short transactions."""

    secret_root = settings.secret_provider.root
    if (
        settings.secret_provider.kind is not SecretProviderKind.FILES
        or secret_root is None
    ):
        raise ServerConfigurationError(
            "The Linux administration web requires the files secret provider"
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

    key_reference = secret_root / IDENTIFIER_DIGEST_KEY_FILENAME
    try:
        identifier_key = secret_reader.read_text(
            secret_root,
            key_reference,
        ).encode("utf-8")
    except LinuxSecretFileError as error:
        raise ServerConfigurationError(
            "The identifier digest key could not be resolved"
        ) from error

    password_hasher = ScryptPasswordHasher()
    identifier_digester = HmacIdentifierDigester(identifier_key)
    administration_repository = PsycopgIdentityAdministrationRepository(
        read_dsn,
        settings.postgres_dsn_file,
    )
    session_policy = ServerSessionPolicy()
    package_directory = Path(__file__).resolve().parents[1]
    return ServerAdministrationDependencies(
        authenticator=PlatformAuthenticationService(
            PsycopgPlatformAuthenticationRepository(
                read_dsn,
                settings.postgres_dsn_file,
            ),
            password_hasher,
            identifier_digester=identifier_digester,
        ),
        sessions=ServerSessionService(
            PsycopgServerSessionRepository(
                read_dsn,
                settings.postgres_dsn_file,
            ),
            SecureSessionSecretFactory(),
            policy=session_policy,
        ),
        organizations=IdentityAdministrationService(
            administration_repository,
            SecureRecoveryTokenFactory(),
            password_hasher,
        ),
        identifier_digester=identifier_digester,
        templates=Jinja2Templates(directory=package_directory / "templates"),
        deployment_id=settings.deployment_id,
        session_absolute_timeout=session_policy.absolute_timeout,
    )


__all__ = [
    "IDENTIFIER_DIGEST_KEY_FILENAME",
    "create_file_secret_server_administration_dependencies",
]
