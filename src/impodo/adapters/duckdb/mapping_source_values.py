"""Read reviewed joined values for generic mapping and categorical checks.

Only requested output columns and join keys reach the bounded DuckDB reader.
Every read verifies the physical snapshots against the accepted join review.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import ExitStack
from typing import TYPE_CHECKING, Protocol

from impodo.application.data_version.source_snapshots import (
    validate_snapshot_for_dataset,
    validate_source_snapshot_path,
)
from impodo.application.shared.artifacts import DataVersionSourceArtifactStore
from impodo.application.workspace.mapping.categorical_coverage import (
    CategoricalSourceRepository,
)
from impodo.application.workspace.mapping.logical_source_values import (
    LogicalSourceValues,
)
from impodo.application.workspace.preparation.grouped_exact_join_preparation import (
    bounded_exact_join_group,
)
from impodo.domain.serialization import content_hash
from impodo.domain.workspace.contracts import SourceSelection
from impodo.domain.workspace.derived_entities import DerivedEntityPlan
from impodo.domain.workspace.errors import WorkspaceError

from .grouped_exact_join_source import DuckDbGroupedExactJoinSource

if TYPE_CHECKING:
    import polars as pl


class MappingDerivationReader(Protocol):
    """Read the current accepted source derivation without changing it."""

    def get_derived_entity_plan(self, workspace_id: str) -> DerivedEntityPlan | None:
        """Return the workspace's current derivation evidence."""
        ...


class JoinedMappingSourceValues(LogicalSourceValues):
    """Resolve joined source columns independently of target models or fields."""

    def __init__(
        self,
        sources: CategoricalSourceRepository,
        derivations: MappingDerivationReader,
        artifacts: DataVersionSourceArtifactStore,
        executor: DuckDbGroupedExactJoinSource,
    ) -> None:
        self.sources = sources
        self.derivations = derivations
        self.artifacts = artifacts
        self.executor = executor

    def supports(self, workspace_id: str, selection: SourceSelection) -> bool:
        """Qualify the current grouped derivation using the preparation rules."""

        physical = self.sources.get_source_selection(workspace_id)
        plan = self.derivations.get_derived_entity_plan(workspace_id)
        return (
            physical is not None
            and bounded_exact_join_group(physical, selection, plan) is not None
        )

    def project(
        self,
        workspace_id: str,
        selection: SourceSelection,
        dataset_id: str,
        column_keys: Sequence[str],
    ) -> pl.DataFrame:
        """Return a narrow value frame in the final joined row order."""

        return self._read(workspace_id, selection, dataset_id, column_keys)

    def counts(
        self,
        workspace_id: str,
        selection: SourceSelection,
        dataset_id: str,
        column_key: str,
        maximum_values: int,
    ) -> dict[str, int]:
        """Aggregate inside DuckDB and return only bounded distinct choices."""

        frame = self._read(
            workspace_id,
            selection,
            dataset_id,
            (column_key,),
            maximum_values=maximum_values,
        )
        return {str(value): int(count) for value, count in frame.iter_rows()}

    def _read(
        self,
        workspace_id: str,
        selection: SourceSelection,
        dataset_id: str,
        column_keys: Sequence[str],
        *,
        maximum_values: int | None = None,
    ) -> pl.DataFrame:
        physical = self.sources.get_source_selection(workspace_id)
        plan = self.derivations.get_derived_entity_plan(workspace_id)
        group = (
            bounded_exact_join_group(physical, selection, plan) if physical else None
        )
        if physical is None or plan is None or group is None:
            raise WorkspaceError("This source derivation has no bounded value reader")
        if (
            plan.workspace_id != workspace_id
            or physical.data_version_id != selection.data_version_id
        ):
            raise WorkspaceError(
                "Joined value evidence belongs to another workspace or Data version"
            )
        dataset = next(
            (item for item in selection.datasets if item.dataset_id == dataset_id), None
        )
        if dataset is None or dataset_id != group.output_dataset_id:
            raise WorkspaceError("Choose a current joined dataset")
        snapshots = self.sources.get_current_source_snapshots(workspace_id)
        by_id = {item.dataset_id: item for item in snapshots}
        expected = {
            (item.dataset_id, item.logical_hash, item.parquet_sha256)
            for item in snapshots
        }
        if len(by_id) != len(snapshots) or set(by_id) != set(
            group.participant_dataset_ids
        ):
            raise WorkspaceError("Joined source snapshots are incomplete")
        rules = {rule.rule_id: rule for rule in group.rules}
        if any(
            review.rule_hash != content_hash(rules[review.rule_id].to_dict())
            or review.source_selection_hash != physical.content_hash
            or {
                (item.dataset_id, item.logical_hash, item.parquet_sha256)
                for item in review.input_snapshots
            }
            != expected
            for review in plan.join_reviews
        ):
            raise WorkspaceError(
                "The accepted grouped join review is stale; review the combined table again"
            )
        for item in physical.datasets:
            validate_snapshot_for_dataset(physical, item, by_id[item.dataset_id])
        with ExitStack() as stack:
            paths = {}
            for snapshot in snapshots:
                path = stack.enter_context(
                    self.artifacts.materialize_source_snapshot(
                        physical.data_version_id,
                        snapshot.parquet_storage_key,
                        expected_sha256=snapshot.parquet_sha256,
                    )
                )
                paths[snapshot.dataset_id] = validate_source_snapshot_path(
                    path, snapshot
                )
            return self.executor.project_values(
                group=group,
                physical_selection=physical,
                effective_dataset=dataset,
                snapshots=by_id,
                paths=paths,
                column_keys=column_keys,
                maximum_values=maximum_values,
            )
