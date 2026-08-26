"""Share bounded source-file HTTP commands across workspace journeys.

The intake service owns file validation, protected storage, revision checks,
catalogue cleanup, and audit evidence. This module only adapts browser upload
handles to that service and keeps potentially expensive file work off the
event loop.
"""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile

from impodo.application.data_version.intake import SourceIntakeError
from impodo.domain.workspace.workbench import SourceFile, WorkspaceStateError
from .context import WebContext
from .forms import _revision


async def accept_source_uploads(
    context: WebContext,
    workspace_id: str,
    form: FormData,
    *,
    allow_multiple: bool = True,
) -> tuple[SourceFile, ...]:
    """Accept all selected files through the canonical intake service."""

    upload_handles = tuple(
        item for item in form.getlist("source_file") if isinstance(item, UploadFile)
    )
    uploads = tuple(item for item in upload_handles if item.filename)
    added: list[SourceFile] = []
    try:
        if not uploads:
            raise SourceIntakeError("Choose a CSV or XLSX file")
        if not allow_multiple and len(uploads) != 1:
            raise SourceIntakeError("Choose one CSV or XLSX file")
        expected_revision = _revision(form)
        for upload in uploads:
            added.append(
                await run_in_threadpool(
                    context.intake.accept,
                    workspace_id,
                    actor=context.actor,
                    expected_revision=expected_revision,
                    display_name=upload.filename,
                    stream=upload.file,
                )
            )
            expected_revision += 1
    except WorkspaceStateError as error:
        if added:
            raise SourceIntakeError(
                f"Added {len(added)} file{'s' if len(added) != 1 else ''}. "
                f"The next file could not be added: {error}"
            ) from error
        raise
    finally:
        for upload in upload_handles:
            await upload.close()
    return tuple(added)


async def remove_source_file(
    context: WebContext,
    workspace_id: str,
    file_id: str,
    *,
    expected_revision: int,
) -> SourceFile:
    """Remove one unfrozen source file through the canonical intake service."""

    return await run_in_threadpool(
        context.intake.remove,
        workspace_id,
        file_id,
        actor=context.actor,
        expected_revision=expected_revision,
    )
