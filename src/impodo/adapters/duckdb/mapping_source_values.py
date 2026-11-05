"""Read reviewed joined and extracted values for generic mapping checks.

Only requested output columns and join keys reach the bounded DuckDB reader.
Every read verifies its frozen snapshot and current preparation-rule bindings.
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
from impodo.domain.preparation.source import SourceLoadError
from impodo.domain.serialization import content_hash
from impodo.domain.source_binding import DerivedSourceBinding
from impodo.domain.workspace.contracts import SourceSelection
from impodo.domain.workspace.derived_entities import (
    DerivedDatasetLink,
    DerivedEntityPlan,
    DerivedEntityRule,
    HierarchicalLookupRule,
    _rule_payload,
    derived_dataset_links,
)
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
    """Resolve joined and flat lookup columns independently of Odoo models."""

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

    def supports(
        self, workspace_id: str, selection: SourceSelection, dataset_id: str
    ) -> bool:
        """Qualify the selected joined or flat lookup output."""

        physical = self.sources.get_source_selection(workspace_id)
        plan = self.derivations.get_derived_entity_plan(workspace_id)
        return (
            physical is not None
            and (self._lookup(plan, dataset_id) is not None or bounded_exact_join_group(
                physical, selection, plan, dataset_id=dataset_id
            ) is not None)
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
        lookup = self._lookup(plan, dataset_id)
        if lookup is not None:
            return self._read_lookup(
                workspace_id, selection, dataset_id, column_keys, maximum_values,
                physical, plan, lookup,
            )
        group = (
            bounded_exact_join_group(physical, selection, plan, dataset_id=dataset_id)
            if physical else None
        )
        if physical is None or plan is None or group is None:
            raise WorkspaceError(
                "Impodo cannot read choices for this table's current preparation rules. "
                "Review the combined table in Source data."
            )
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
        if len(by_id) != len(snapshots) or set(by_id) != {
            item.dataset_id for item in physical.datasets
        }:
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
            if review.rule_id in rules
        ):
            raise WorkspaceError(
                "The accepted grouped join review is stale; review the combined table again"
            )
        for item in physical.datasets:
            validate_snapshot_for_dataset(physical, item, by_id[item.dataset_id])
        with ExitStack() as stack:
            paths = {}
            for snapshot in snapshots:
                if snapshot.dataset_id not in group.participant_dataset_ids:
                    continue
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

    @staticmethod
    def _lookup(
        plan: DerivedEntityPlan | None,
        dataset_id: str,
    ) -> tuple[DerivedEntityRule, DerivedDatasetLink] | None:
        """Select only the same flat lookup shape supported by preparation."""

        if plan is None:
            return None
        return next(
            (
                (rule, link)
                for rule, link in zip(
                    (
                        item
                        for item in plan.rules
                        if isinstance(item, (DerivedEntityRule, HierarchicalLookupRule))
                    ),
                    derived_dataset_links(plan),
                    strict=True,
                )
                if link.derived_dataset_id == dataset_id
                and isinstance(rule, DerivedEntityRule)
                and rule.parent_separator is None
            ),
            None,
        )

    def _read_lookup(
        self,
        workspace_id: str,
        selection: SourceSelection,
        dataset_id: str,
        column_keys: Sequence[str],
        maximum_values: int | None,
        physical: SourceSelection | None,
        plan: DerivedEntityPlan | None,
        lookup: tuple[DerivedEntityRule, DerivedDatasetLink],
    ) -> pl.DataFrame:
        """Reuse the bounded preparation producer, including Unicode grouping."""

        import polars as pl

        rule, link = lookup
        output = next(
            (item for item in selection.datasets if item.dataset_id == dataset_id), None
        )
        source = (
            next(
                (
                    item
                    for item in physical.datasets
                    if item.dataset_id == rule.source_dataset_id
                ),
                None,
            )
            if physical
            else None
        )
        if (
            physical is None
            or plan is None
            or source is None
            or output is None
            or plan.workspace_id != workspace_id
            or plan.source_selection_hash != physical.content_hash
            or not isinstance(output.source, DerivedSourceBinding)
            or output.source.rule_hash != content_hash(_rule_payload(rule))
            or output.source.input_dataset_ids != (source.dataset_id,)
            or output.source.data_hash != source.source_evidence_hash
        ):
            raise WorkspaceError(
                "The reusable table's source evidence changed; review Source data again"
            )
        columns = {item.stable_key: item.source_name for item in output.columns}
        if not column_keys or any(key not in columns for key in column_keys):
            raise WorkspaceError("Choose current reusable-table columns")
        snapshot = next(
            (
                item
                for item in self.sources.get_current_source_snapshots(workspace_id)
                if item.dataset_id == source.dataset_id
            ),
            None,
        )
        if snapshot is None:
            raise WorkspaceError("The reusable table's frozen source is incomplete")
        validate_snapshot_for_dataset(physical, source, snapshot)
        rows = []
        try:
            with self.artifacts.materialize_source_snapshot(
                physical.data_version_id,
                snapshot.parquet_storage_key,
                expected_sha256=snapshot.parquet_sha256,
            ) as path:
                path = validate_source_snapshot_path(path, snapshot)
                for batch in self.executor.iter_lookup_batches(
                    rule=rule,
                    link=link,
                    effective_dataset=output,
                    snapshot=snapshot,
                    path=path,
                    batch_size=1000,
                ):
                    rows.extend(
                        tuple(row.values[columns[key]] for key in column_keys)
                        for row in batch.rows
                    )
        except (OSError, SourceLoadError) as error:
            raise WorkspaceError(
                "The reusable table's frozen source could not be verified"
            ) from error
        frame = pl.DataFrame(
            rows, schema={key: pl.String for key in column_keys}, orient="row"
        )
        if maximum_values is not None:
            return (
                frame.select(pl.col(column_keys[0]).str.strip_chars())
                .filter(
                    pl.col(column_keys[0]).is_not_null()
                    & (pl.col(column_keys[0]) != "")
                )
                .group_by(column_keys[0])
                .len()
                .head(maximum_values + 1)
            )
        return frame
