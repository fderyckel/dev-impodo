"""Share bounded source-file HTTP commands across workspace journeys.

The intake service owns file validation, protected storage, revision checks,
catalogue cleanup, and audit evidence. This module only adapts browser upload
handles to that service and keeps potentially expensive file work off the
event loop.
"""

from __future__ import annotations

from starlette.datastructures import FormData, UploadFile

from impodo.application.data_version.intake import SourceIntakeError
from impodo.domain.shared.access import Actor
from impodo.domain.workspace.workbench import SourceFile, WorkspaceStateError

from .composition.page_reads import run_local_operation
from .context import WebContext
from .forms import _revision


async def accept_source_uploads(
    context: WebContext,
    workspace_id: str,
    form: FormData,
    *,
    actor: Actor,
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

        def accept_all() -> None:
            current_revision = expected_revision
            for upload in uploads:
                added.append(
                    context.intake.accept(
                        workspace_id,
                        actor=actor,
                        expected_revision=current_revision,
                        display_name=upload.filename,
                        stream=upload.file,
                    )
                )
                current_revision += 1

        await run_local_operation(accept_all)
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
    actor: Actor,
    expected_revision: int,
) -> SourceFile:
    """Remove one unfrozen source file through the canonical intake service."""

    return await run_local_operation(
        context.intake.remove,
        workspace_id,
        file_id,
        actor=actor,
        expected_revision=expected_revision,
    )
