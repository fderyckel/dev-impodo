"""Bind prepared projection metadata to one bounded canonical read.

Each context is created from the current database query and lives only for
that read's iterator. It retains no connection or materialized artifact path.
File-byte verification, canonical row counts, and lineage checks remain with
their existing readers; this context does not cache those checks.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from impodo.domain.prepared_snapshot import PreparedSnapshot
from impodo.domain.staging.preparation_session import (
    PreparedCanonicalEncodingUnsupported,
    PreparedCanonicalProjection,
)
from impodo.domain.workspace.errors import WorkspaceError


def _bound_integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise WorkspaceError("Prepared canonical projection metadata changed")
    return value


@dataclass(frozen=True, slots=True)
class PreparedDatasetReadContext:
    """Hold one dataset's checked metadata for its successive value batches."""

    ordinal_start: int
    row_count: int
    projection: PreparedCanonicalProjection
    snapshot: PreparedSnapshot


@dataclass(frozen=True, slots=True)
class PreparedProjectionReadContext:
    """Capture checked dataset ranges for one workspace and canonical run."""

    workspace_id: str
    run_id: str
    datasets: tuple[PreparedDatasetReadContext, ...]

    @classmethod
    def create(
        cls,
        workspace_id: str,
        run_id: str,
        rows: Sequence[Sequence[object]],
    ) -> PreparedProjectionReadContext:
        """Check every current metadata row before yielding canonical values.

        The projection parser verifies its program hash. Comparing that exact
        verified receipt with the snapshot avoids hashing the same program a
        second time. No successful check is reused by a later context.
        """

        datasets: list[PreparedDatasetReadContext] = []
        next_ordinal = 0
        for row in rows:
            if len(row) != 4:
                raise WorkspaceError("Prepared canonical projection is invalid")
            ordinal_start, row_count, projection_text, manifest_text = row
            try:
                payload = json.loads(str(projection_text))
                if not isinstance(payload, Mapping):
                    raise TypeError("Prepared projection must be an object")
                projection = PreparedCanonicalProjection.from_portable_dict(payload)
                snapshot = PreparedSnapshot.from_json(str(manifest_text))
            except PreparedCanonicalEncodingUnsupported as error:
                raise WorkspaceError(str(error)) from error
            except (KeyError, TypeError, ValueError) as error:
                raise WorkspaceError(
                    "Prepared canonical projection is invalid"
                ) from error
            start = _bound_integer(ordinal_start)
            count = _bound_integer(row_count)
            program_hash = payload.get("program_hash")
            if (
                not isinstance(program_hash, str)
                or _bound_integer(payload.get("ordinal_start")) != start
                or _bound_integer(payload.get("row_count")) != count
                or projection.ordinal_start != start
                or projection.row_count != count
                or snapshot.workspace_id != workspace_id
                or projection.dataset_id != snapshot.dataset_id
                or projection.dataset != snapshot.dataset_name
                or projection.row_count != snapshot.row_count
                or program_hash != snapshot.transformation_program_hash
                or projection.program.mapping_content_hash != snapshot.mapping_hash
                or projection.program.schema_hash != snapshot.schema_hash
            ):
                raise WorkspaceError("Prepared canonical projection metadata changed")
            if start < next_ordinal:
                raise WorkspaceError("Prepared canonical projection ordinals overlap")
            datasets.append(
                PreparedDatasetReadContext(start, count, projection, snapshot)
            )
            next_ordinal = start + count
        return cls(workspace_id, run_id, tuple(datasets))
