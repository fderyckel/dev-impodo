"""Keep spawned preparation fault targets independent of browser fixtures.

Multiprocessing imports a target's module before it enters the target. Browser
test modules import native adapters, so importing one there would freeze the
Polars pool before the child could apply its preparation thread setting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from impodo.application.shared.build_contract import ApplicationBuildContract
    from impodo.application.workspace.preparation.job_models import PreparationWorkspace
    from impodo.domain.shared.access import Actor


class _EventQueue(Protocol):
    def put(self, event: tuple[object, ...]) -> None: ...


class _Cancellation(Protocol):
    def is_set(self) -> bool: ...


class _WithoutTerminalEvents:
    def __init__(self, events: _EventQueue) -> None:
        self.events = events

    def put(self, event: tuple[object, ...]) -> None:
        if event[0] not in {"succeeded", "review_required", "failed", "cancelled"}:
            self.events.put(event)


def worker_without_final_notification(
    root: str,
    workspace_id: str,
    workspace: PreparationWorkspace,
    build: ApplicationBuildContract,
    actor: Actor,
    events: _EventQueue,
    cancel: _Cancellation,
) -> None:
    """Commit in a real spawned child while dropping terminal notifications."""

    from impodo.application.shared.preparation_runtime import (
        configure_preparation_runtime,
    )

    configure_preparation_runtime()

    from impodo.web.composition.preparation_job_manager import _run_preparation_worker

    _run_preparation_worker(
        root, workspace_id, workspace, build, actor,
        _WithoutTerminalEvents(events), cancel,
    )
