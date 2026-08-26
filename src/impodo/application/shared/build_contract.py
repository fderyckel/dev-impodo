"""Bind one running Impodo process to its exact local application build."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from impodo.domain.project.foundation import require_hash
from impodo.domain.shared.schema import (
    WORKSPACE_SCHEMA_GENERATION,
    WORKSPACE_SCHEMA_VERSION,
)


BUILD_CONTRACT_VERSION = 1
_BUILD_FILE_SUFFIXES = frozenset({".css", ".html", ".js", ".py"})


class ApplicationBuildMismatchError(RuntimeError):
    """Stop work when the application and worker loaded different builds."""

    failure_code = "IMPODO_BUILD_CHANGED"


@dataclass(frozen=True, slots=True)
class ApplicationBuildContract:
    """Identify the application files and workspace contract loaded together."""

    application_build_id: str
    workspace_schema_generation: str
    workspace_schema_version: int
    contract_version: int = BUILD_CONTRACT_VERSION

    def __post_init__(self) -> None:
        require_hash(self.application_build_id, "application_build_id")
        if not self.workspace_schema_generation.strip():
            raise ValueError("workspace_schema_generation is required")
        if self.workspace_schema_version < 1:
            raise ValueError("workspace_schema_version is invalid")
        if self.contract_version != BUILD_CONTRACT_VERSION:
            raise ValueError("application build contract is unsupported")


def calculate_application_build_contract() -> ApplicationBuildContract:
    """Hash the installed code and browser assets without reading project data."""

    package_root = Path(__file__).resolve().parents[2]
    digest = sha256()
    for path in sorted(
        (
            item
            for item in package_root.rglob("*")
            if item.is_file()
            and item.suffix.casefold() in _BUILD_FILE_SUFFIXES
            and "__pycache__" not in item.parts
        ),
        key=lambda item: item.relative_to(package_root).as_posix(),
    ):
        relative = path.relative_to(package_root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        payload = path.read_bytes()
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return ApplicationBuildContract(
        application_build_id=f"sha256:{digest.hexdigest()}",
        workspace_schema_generation=WORKSPACE_SCHEMA_GENERATION,
        workspace_schema_version=WORKSPACE_SCHEMA_VERSION,
    )


# A process captures this once. A spawned worker imports the package again and
# captures its own value, so a changed editable checkout cannot be mistaken for
# the build that accepted the browser request.
PROCESS_BUILD_CONTRACT = calculate_application_build_contract()


def require_same_application_build(
    expected: ApplicationBuildContract,
    actual: ApplicationBuildContract | None = None,
) -> None:
    """Reject a split build before a worker opens workspace evidence."""

    observed = actual or PROCESS_BUILD_CONTRACT
    if observed != expected:
        raise ApplicationBuildMismatchError(
            "Impodo was updated while it was open. Restart Impodo before "
            "continuing this work. Your saved work is unchanged, and no Odoo "
            "records were changed by this attempt."
        )
