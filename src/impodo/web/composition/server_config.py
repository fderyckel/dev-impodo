"""Parse the fail-closed configuration contract for shared-server roles."""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from urllib.parse import urlsplit

SERVER_UID = 10001
SERVER_GID = 10001

_DEPLOYMENT_ID_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_HOSTNAME_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
)


class ServerConfigurationError(ValueError):
    """Report a rejected production setting without exposing its value."""


class SecretProviderKind(StrEnum):
    """Select how server roles resolve secret references at runtime."""

    FILES = "files"
    EXTERNAL = "external"


class OdooEgressMode(StrEnum):
    """Select the deployment-owned outbound Odoo network boundary."""

    DISABLED = "disabled"
    ALLOWLIST = "allowlist"
    PRIVATE_NETWORK = "private_network"
    HTTP_PROXY = "http_proxy"


class ServerLogLevel(StrEnum):
    """Bound the accepted production log levels."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class SecretProviderSettings:
    """Name a secret provider without containing secret material."""

    kind: SecretProviderKind
    root: PurePosixPath | None
    configuration_file: PurePosixPath | None


@dataclass(frozen=True, slots=True)
class OdooNetworkPolicy:
    """Describe the server's deployment-owned Odoo egress boundary."""

    mode: OdooEgressMode
    allowed_destinations: tuple[str, ...]
    proxy_url_file: PurePosixPath | None


@dataclass(frozen=True, slots=True)
class ServerSettings:
    """Hold validated non-secret settings for every server process role."""

    deployment_id: str
    public_url: str
    trusted_proxy_networks: tuple[
        ipaddress.IPv4Network | ipaddress.IPv6Network,
        ...,
    ]
    web_bind_host: ipaddress.IPv4Address | ipaddress.IPv6Address
    web_bind_port: int
    drain_timeout_seconds: int
    postgres_dsn_file: PurePosixPath
    artifact_root: PurePosixPath
    runtime_root: PurePosixPath
    temporary_root: PurePosixPath
    secret_provider: SecretProviderSettings
    worker_capacity: int
    log_level: ServerLogLevel
    odoo_network: OdooNetworkPolicy


@dataclass(frozen=True, slots=True)
class ServerMigrationSettings:
    """Hold only the settings and privileged secret needed by migrations."""

    deployment_id: str
    postgres_migration_dsn_file: PurePosixPath
    secret_provider: SecretProviderSettings


_REQUIRED_KEYS = frozenset(
    {
        "IMPODO_DEPLOYMENT_ID",
        "IMPODO_PUBLIC_URL",
        "IMPODO_TRUSTED_PROXY_CIDRS",
        "IMPODO_WEB_BIND_HOST",
        "IMPODO_WEB_BIND_PORT",
        "IMPODO_DRAIN_TIMEOUT_SECONDS",
        "IMPODO_POSTGRES_DSN_FILE",
        "IMPODO_ARTIFACT_ROOT",
        "IMPODO_RUNTIME_ROOT",
        "IMPODO_TEMP_ROOT",
        "IMPODO_SECRET_PROVIDER",
        "IMPODO_WORKER_CAPACITY",
        "IMPODO_ODOO_EGRESS_MODE",
    }
)
_OPTIONAL_KEYS = frozenset(
    {
        "IMPODO_SECRET_ROOT",
        "IMPODO_SECRET_PROVIDER_CONFIG_FILE",
        "IMPODO_LOG_LEVEL",
        "IMPODO_ODOO_ALLOWED_DESTINATIONS",
        "IMPODO_ODOO_PROXY_URL_FILE",
    }
)
_ALLOWED_KEYS = _REQUIRED_KEYS | _OPTIONAL_KEYS
_MIGRATION_REQUIRED_KEYS = frozenset(
    {
        "IMPODO_DEPLOYMENT_ID",
        "IMPODO_POSTGRES_MIGRATION_DSN_FILE",
        "IMPODO_SECRET_PROVIDER",
    }
)
_MIGRATION_OPTIONAL_KEYS = frozenset(
    {
        "IMPODO_SECRET_ROOT",
        "IMPODO_SECRET_PROVIDER_CONFIG_FILE",
    }
)
_MIGRATION_ALLOWED_KEYS = _MIGRATION_REQUIRED_KEYS | _MIGRATION_OPTIONAL_KEYS


def _required(environment: Mapping[str, str], key: str) -> str:
    value = environment.get(key, "").strip()
    if not value:
        raise ServerConfigurationError(f"Required server setting {key} is missing")
    return value


def _optional(environment: Mapping[str, str], key: str) -> str | None:
    value = environment.get(key)
    if value is None or not value.strip():
        return None
    return value.strip()


def _posix_path(value: str, *, key: str) -> PurePosixPath:
    if "\\" in value:
        raise ServerConfigurationError(f"{key} must be an absolute POSIX path")
    path = PurePosixPath(value)
    if not path.is_absolute() or path == PurePosixPath("/") or ".." in path.parts:
        raise ServerConfigurationError(f"{key} must be a bounded absolute POSIX path")
    return path


def _public_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ServerConfigurationError(
            "IMPODO_PUBLIC_URL must be an HTTPS origin without credentials, "
            "a path, query, or fragment"
        )
    try:
        port = parsed.port
    except ValueError as error:
        raise ServerConfigurationError(
            "IMPODO_PUBLIC_URL contains an invalid port"
        ) from error
    authority = parsed.hostname.lower()
    try:
        ipaddress.ip_address(authority)
    except ValueError:
        if _HOSTNAME_PATTERN.fullmatch(authority) is None:
            raise ServerConfigurationError(
                "IMPODO_PUBLIC_URL contains an invalid hostname"
            )
    if ":" in authority:
        authority = f"[{authority}]"
    if port is not None and port != 443:
        authority = f"{authority}:{port}"
    return f"https://{authority}"


def _trusted_proxy_networks(
    value: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in value.split(","):
        candidate = item.strip()
        if not candidate:
            raise ServerConfigurationError(
                "IMPODO_TRUSTED_PROXY_CIDRS contains an empty network"
            )
        try:
            network = ipaddress.ip_network(candidate, strict=True)
        except ValueError as error:
            raise ServerConfigurationError(
                "IMPODO_TRUSTED_PROXY_CIDRS contains a non-canonical network"
            ) from error
        if network.prefixlen == 0:
            raise ServerConfigurationError(
                "IMPODO_TRUSTED_PROXY_CIDRS may not trust an entire address family"
            )
        networks.append(network)
    return tuple(dict.fromkeys(networks))


def _enum_value[EnumValue: StrEnum](
    enum_type: type[EnumValue],
    value: str,
    *,
    key: str,
) -> EnumValue:
    try:
        return enum_type(value)
    except ValueError as error:
        allowed = ", ".join(item.value for item in enum_type)
        raise ServerConfigurationError(f"{key} must be one of: {allowed}") from error


def _secret_provider(environment: Mapping[str, str]) -> SecretProviderSettings:
    kind = _enum_value(
        SecretProviderKind,
        _required(environment, "IMPODO_SECRET_PROVIDER"),
        key="IMPODO_SECRET_PROVIDER",
    )
    root_value = _optional(environment, "IMPODO_SECRET_ROOT")
    configuration_value = _optional(
        environment,
        "IMPODO_SECRET_PROVIDER_CONFIG_FILE",
    )
    root = (
        _posix_path(root_value, key="IMPODO_SECRET_ROOT")
        if root_value is not None
        else None
    )
    configuration_file = (
        _posix_path(
            configuration_value,
            key="IMPODO_SECRET_PROVIDER_CONFIG_FILE",
        )
        if configuration_value is not None
        else None
    )
    if kind is SecretProviderKind.FILES:
        if root is None or configuration_file is not None:
            raise ServerConfigurationError(
                "The files secret provider requires IMPODO_SECRET_ROOT and "
                "forbids IMPODO_SECRET_PROVIDER_CONFIG_FILE"
            )
    elif configuration_file is None or root is not None:
        raise ServerConfigurationError(
            "The external secret provider requires "
            "IMPODO_SECRET_PROVIDER_CONFIG_FILE and forbids IMPODO_SECRET_ROOT"
        )
    return SecretProviderSettings(
        kind=kind,
        root=root,
        configuration_file=configuration_file,
    )


def _destination(value: str) -> str:
    candidate = value.strip().lower().rstrip(".")
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        pass
    if "/" in candidate:
        try:
            return str(ipaddress.ip_network(candidate, strict=True))
        except ValueError as error:
            raise ServerConfigurationError(
                "IMPODO_ODOO_ALLOWED_DESTINATIONS contains an invalid or "
                "non-canonical network"
            ) from error
    if (
        not candidate
        or "://" in candidate
        or any(character in candidate for character in ("/", "@", "*", "?", "#"))
    ):
        raise ServerConfigurationError(
            "IMPODO_ODOO_ALLOWED_DESTINATIONS accepts only exact hosts, "
            "host-and-port pairs, IP addresses, or CIDR networks"
        )
    try:
        parsed = urlsplit(f"//{candidate}")
        host = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ServerConfigurationError(
            "IMPODO_ODOO_ALLOWED_DESTINATIONS contains an invalid host or port"
        ) from error
    if host is None:
        raise ServerConfigurationError(
            "IMPODO_ODOO_ALLOWED_DESTINATIONS contains an invalid host"
        )
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if _HOSTNAME_PATTERN.fullmatch(host) is None:
            raise ServerConfigurationError(
                "IMPODO_ODOO_ALLOWED_DESTINATIONS contains an invalid hostname"
            )
    normalized_host = f"[{host}]" if ":" in host else host
    return normalized_host if port is None else f"{normalized_host}:{port}"


def _odoo_network_policy(environment: Mapping[str, str]) -> OdooNetworkPolicy:
    mode = _enum_value(
        OdooEgressMode,
        _required(environment, "IMPODO_ODOO_EGRESS_MODE"),
        key="IMPODO_ODOO_EGRESS_MODE",
    )
    destinations_value = _optional(
        environment,
        "IMPODO_ODOO_ALLOWED_DESTINATIONS",
    )
    destinations = (
        tuple(
            dict.fromkeys(_destination(item) for item in destinations_value.split(","))
        )
        if destinations_value is not None
        else ()
    )
    proxy_value = _optional(environment, "IMPODO_ODOO_PROXY_URL_FILE")
    proxy_file = (
        _posix_path(proxy_value, key="IMPODO_ODOO_PROXY_URL_FILE")
        if proxy_value is not None
        else None
    )
    if mode is OdooEgressMode.DISABLED:
        if destinations or proxy_file is not None:
            raise ServerConfigurationError(
                "Disabled Odoo egress forbids destinations and proxy settings"
            )
    elif not destinations:
        raise ServerConfigurationError(
            "Enabled Odoo egress requires exact allowed destinations"
        )
    if mode is OdooEgressMode.HTTP_PROXY and proxy_file is None:
        raise ServerConfigurationError(
            "HTTP proxy egress requires IMPODO_ODOO_PROXY_URL_FILE"
        )
    if mode is not OdooEgressMode.HTTP_PROXY and proxy_file is not None:
        raise ServerConfigurationError(
            "IMPODO_ODOO_PROXY_URL_FILE is valid only for HTTP proxy egress"
        )
    return OdooNetworkPolicy(
        mode=mode,
        allowed_destinations=destinations,
        proxy_url_file=proxy_file,
    )


def _worker_capacity(value: str) -> int:
    try:
        capacity = int(value)
    except ValueError as error:
        raise ServerConfigurationError(
            "IMPODO_WORKER_CAPACITY must be an integer"
        ) from error
    if not 1 <= capacity <= 256:
        raise ServerConfigurationError(
            "IMPODO_WORKER_CAPACITY must be between 1 and 256"
        )
    return capacity


def _web_bind_host(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        return ipaddress.ip_address(value)
    except ValueError as error:
        raise ServerConfigurationError(
            "IMPODO_WEB_BIND_HOST must be an IPv4 or IPv6 address"
        ) from error


def _bounded_integer(
    value: str,
    *,
    key: str,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ServerConfigurationError(f"{key} must be an integer") from error
    if not minimum <= parsed <= maximum:
        raise ServerConfigurationError(f"{key} must be between {minimum} and {maximum}")
    return parsed


def _assert_distinct_data_paths(settings: ServerSettings) -> None:
    paths = (
        settings.artifact_root,
        settings.runtime_root,
        settings.temporary_root,
    )
    if len(set(paths)) != len(paths):
        raise ServerConfigurationError(
            "Artifact, runtime, and temporary roots must be distinct"
        )
    secret_paths = tuple(
        path
        for path in (
            settings.postgres_dsn_file,
            settings.secret_provider.root,
            settings.secret_provider.configuration_file,
            settings.odoo_network.proxy_url_file,
        )
        if path is not None
    )
    for secret_path in secret_paths:
        if any(secret_path == root or root in secret_path.parents for root in paths):
            raise ServerConfigurationError(
                "Secret references may not be stored below an Impodo data root"
            )


def _assert_file_secret_references(
    secret_provider: SecretProviderSettings,
    references: tuple[PurePosixPath, ...],
) -> None:
    if secret_provider.kind is not SecretProviderKind.FILES:
        return
    secret_root = secret_provider.root
    if secret_root is None:
        raise ServerConfigurationError(
            "The files secret provider requires a secret root"
        )
    if any(reference.parent != secret_root for reference in references):
        raise ServerConfigurationError(
            "File secret references must be direct children of IMPODO_SECRET_ROOT"
        )


def _deployment_id(values: Mapping[str, str]) -> str:
    deployment_id = _required(values, "IMPODO_DEPLOYMENT_ID").lower()
    if _DEPLOYMENT_ID_PATTERN.fullmatch(deployment_id) is None:
        raise ServerConfigurationError(
            "IMPODO_DEPLOYMENT_ID must be a bounded lowercase DNS label"
        )
    return deployment_id


def _validate_setting_names(
    values: Mapping[str, str],
    *,
    required_keys: frozenset[str],
    allowed_keys: frozenset[str],
) -> None:
    unknown = sorted(
        key for key in values if key.startswith("IMPODO_") and key not in allowed_keys
    )
    if unknown:
        raise ServerConfigurationError(
            "Unknown server setting(s): " + ", ".join(unknown)
        )
    missing = sorted(key for key in required_keys if not values.get(key, "").strip())
    if missing:
        raise ServerConfigurationError(
            "Required server setting(s) missing: " + ", ".join(missing)
        )


def load_server_settings(
    environment: Mapping[str, str] | None = None,
) -> ServerSettings:
    """Parse server settings and reject unknown or secret-bearing variables."""

    values = os.environ if environment is None else environment
    _validate_setting_names(
        values,
        required_keys=_REQUIRED_KEYS,
        allowed_keys=_ALLOWED_KEYS,
    )
    log_level_value = _optional(values, "IMPODO_LOG_LEVEL") or ServerLogLevel.INFO
    settings = ServerSettings(
        deployment_id=_deployment_id(values),
        public_url=_public_url(_required(values, "IMPODO_PUBLIC_URL")),
        trusted_proxy_networks=_trusted_proxy_networks(
            _required(values, "IMPODO_TRUSTED_PROXY_CIDRS")
        ),
        web_bind_host=_web_bind_host(_required(values, "IMPODO_WEB_BIND_HOST")),
        web_bind_port=_bounded_integer(
            _required(values, "IMPODO_WEB_BIND_PORT"),
            key="IMPODO_WEB_BIND_PORT",
            minimum=1024,
            maximum=65535,
        ),
        drain_timeout_seconds=_bounded_integer(
            _required(values, "IMPODO_DRAIN_TIMEOUT_SECONDS"),
            key="IMPODO_DRAIN_TIMEOUT_SECONDS",
            minimum=1,
            maximum=3600,
        ),
        postgres_dsn_file=_posix_path(
            _required(values, "IMPODO_POSTGRES_DSN_FILE"),
            key="IMPODO_POSTGRES_DSN_FILE",
        ),
        artifact_root=_posix_path(
            _required(values, "IMPODO_ARTIFACT_ROOT"),
            key="IMPODO_ARTIFACT_ROOT",
        ),
        runtime_root=_posix_path(
            _required(values, "IMPODO_RUNTIME_ROOT"),
            key="IMPODO_RUNTIME_ROOT",
        ),
        temporary_root=_posix_path(
            _required(values, "IMPODO_TEMP_ROOT"),
            key="IMPODO_TEMP_ROOT",
        ),
        secret_provider=_secret_provider(values),
        worker_capacity=_worker_capacity(_required(values, "IMPODO_WORKER_CAPACITY")),
        log_level=_enum_value(
            ServerLogLevel,
            str(log_level_value).upper(),
            key="IMPODO_LOG_LEVEL",
        ),
        odoo_network=_odoo_network_policy(values),
    )
    _assert_distinct_data_paths(settings)
    _assert_file_secret_references(
        settings.secret_provider,
        tuple(
            reference
            for reference in (
                settings.postgres_dsn_file,
                settings.odoo_network.proxy_url_file,
            )
            if reference is not None
        ),
    )
    return settings


def load_server_migration_settings(
    environment: Mapping[str, str] | None = None,
) -> ServerMigrationSettings:
    """Parse the narrow migration-role settings and reject runtime settings."""

    values = os.environ if environment is None else environment
    _validate_setting_names(
        values,
        required_keys=_MIGRATION_REQUIRED_KEYS,
        allowed_keys=_MIGRATION_ALLOWED_KEYS,
    )
    settings = ServerMigrationSettings(
        deployment_id=_deployment_id(values),
        postgres_migration_dsn_file=_posix_path(
            _required(values, "IMPODO_POSTGRES_MIGRATION_DSN_FILE"),
            key="IMPODO_POSTGRES_MIGRATION_DSN_FILE",
        ),
        secret_provider=_secret_provider(values),
    )
    _assert_file_secret_references(
        settings.secret_provider,
        (settings.postgres_migration_dsn_file,),
    )
    return settings


__all__ = [
    "SERVER_GID",
    "SERVER_UID",
    "OdooEgressMode",
    "OdooNetworkPolicy",
    "SecretProviderKind",
    "SecretProviderSettings",
    "ServerConfigurationError",
    "ServerLogLevel",
    "ServerMigrationSettings",
    "ServerSettings",
    "load_server_migration_settings",
    "load_server_settings",
]
