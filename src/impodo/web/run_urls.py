"""Keep run setup URLs aligned with the owning Test or Production purpose."""

from enum import StrEnum


class RunSetupKind(StrEnum):
    TEST = "test-runs"
    PRODUCTION = "production-runs"

    @property
    def purpose(self) -> str:
        return "TEST" if self is RunSetupKind.TEST else "PRODUCTION"


def fresh_data_url(project_id: str, migration_run_id: str, *, run_kind: RunSetupKind = RunSetupKind.TEST) -> str:
    return f"/projects/{project_id}/{run_kind}/{migration_run_id}/fresh-data"
