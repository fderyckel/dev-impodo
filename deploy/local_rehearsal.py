"""Create fictional file secrets for the local empty-server rehearsal."""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path
from urllib.parse import quote

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SECRET_DIRECTORY = REPOSITORY_ROOT / "deploy" / ".local" / "secrets"
SECRET_FILENAMES = (
    "postgres-bootstrap-password",
    "postgres-runtime-password",
    "postgres-migration-password",
    "postgres-runtime-dsn",
    "postgres-migration-dsn",
    "platform-admin-password",
    "identifier-digest-key",
)


def _new_password() -> str:
    return secrets.token_urlsafe(32)


def _dsn(role: str, password: str) -> str:
    encoded_password = quote(password, safe="")
    return (
        f"postgresql://{role}:{encoded_password}@postgres:5432/impodo?sslmode=disable"
    )


def _prepare_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("The local secret directory may not be a symbolic link")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError("The local secret path must be a directory")
    if os.name == "posix":
        path.chmod(0o700)


def _write_secret(path: Path, value: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if no_follow := getattr(os, "O_NOFOLLOW", 0):
        flags |= no_follow
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    if os.name == "posix":
        path.chmod(0o600)


def prepare_local_secrets(directory: Path) -> tuple[Path, ...]:
    """Create one internally consistent fictional secret set without overwrite."""

    _prepare_directory(directory)
    targets = tuple(directory / filename for filename in SECRET_FILENAMES)
    existing = tuple(path.name for path in targets if path.exists())
    if existing:
        names = ", ".join(existing)
        raise FileExistsError(
            "Local rehearsal secrets already exist; refusing to overwrite: " + names
        )

    postgres_bootstrap_password = _new_password()
    runtime_password = _new_password()
    migration_password = _new_password()
    platform_admin_password = _new_password()
    identifier_digest_key = secrets.token_urlsafe(48)
    values = (
        postgres_bootstrap_password,
        runtime_password,
        migration_password,
        _dsn("impodo_runtime", runtime_password),
        _dsn("impodo_migrator", migration_password),
        platform_admin_password,
        identifier_digest_key,
    )
    created: list[Path] = []
    try:
        for path, value in zip(targets, values, strict=True):
            _write_secret(path, value)
            created.append(path)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return targets


def prepare_platform_admin_secret(directory: Path) -> Path:
    """Add only the fictional admin password to an older rehearsal safely."""

    _prepare_directory(directory)
    target = directory / "platform-admin-password"
    if target.exists():
        raise FileExistsError(
            "Local rehearsal Platform Administrator secret already exists; "
            "refusing to overwrite it"
        )
    _write_secret(target, _new_password())
    return target


def prepare_identifier_digest_key(directory: Path) -> Path:
    """Add only the deployment-keyed identifier digest secret safely."""

    _prepare_directory(directory)
    target = directory / "identifier-digest-key"
    if target.exists():
        raise FileExistsError(
            "Local rehearsal identifier digest key already exists; "
            "refusing to overwrite it"
        )
    _write_secret(target, secrets.token_urlsafe(48))
    return target


def build_parser() -> argparse.ArgumentParser:
    """Build the bounded local-rehearsal secret command."""

    parser = argparse.ArgumentParser(
        description="Create fictional secrets for the empty local server rehearsal."
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=DEFAULT_SECRET_DIRECTORY,
        help="ignored directory that will receive the fictional secret files",
    )
    parser.add_argument(
        "--identifier-digest-key-only",
        action="store_true",
        help="add only the missing identifier digest key to an older rehearsal",
    )
    parser.add_argument(
        "--platform-admin-only",
        action="store_true",
        help="add only the missing fictional admin password to an older rehearsal",
    )
    return parser


def main() -> int:
    """Create the local secret set without printing its values."""

    arguments = build_parser().parse_args()
    try:
        if arguments.platform_admin_only and arguments.identifier_digest_key_only:
            raise ValueError("Choose only one single-secret preparation option")
        if arguments.platform_admin_only:
            created = (prepare_platform_admin_secret(arguments.directory),)
        elif arguments.identifier_digest_key_only:
            created = (prepare_identifier_digest_key(arguments.directory),)
        else:
            created = prepare_local_secrets(arguments.directory)
    except (FileExistsError, OSError, ValueError) as error:
        print(f"Local rehearsal secret preparation refused: {error}")
        return 1
    print(f"Created {len(created)} fictional local rehearsal secret files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
