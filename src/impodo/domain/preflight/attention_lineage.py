"""Resolve displayed prepared columns to their saved physical source headings.

The graph describes column provenance only. It does not rerun source joins,
aggregation, row checks, or dependency closure.
"""

from collections import defaultdict

from impodo.domain.source_binding import DerivedSourceBinding
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.domain.structural import (
    ExactJoinRule,
    GroupAggregateRule,
    UnionAllRule,
    structural_dataset_id,
)
from impodo.domain.workspace.contracts import SourceSelection
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    derived_dataset_links,
    hierarchy_consumer_bindings,
)


def attention_column_origins(
    selection: SourceSelection,
    snapshots: tuple[SourceSnapshot, ...],
    plan: DerivedEntityPlan | None,
) -> dict[str, dict[str, tuple[str, ...]]]:
    """Index physical origins once, including chained renamed join projections."""

    physical = {
        (snapshot.dataset_id, column.stable_key)
        for snapshot in snapshots
        for column in snapshot.schema.columns
    }
    graph: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    parents = {
        dataset.dataset_id: dataset.source.input_dataset_ids
        for dataset in selection.datasets
        if isinstance(dataset.source, DerivedSourceBinding)
    }
    for rule in plan.rules if plan else ():
        if isinstance(rule, ExactJoinRule):
            output = structural_dataset_id(rule)
            for projection in rule.projections:
                graph[(output, projection.output_column_key)].append(
                    (projection.source_dataset_id, projection.source_column_key)
                )
        elif isinstance(rule, UnionAllRule):
            output = structural_dataset_id(rule)
            for branch in rule.branches:
                for projection in branch.projections:
                    graph[(output, projection.output_column_key)].append(
                        (projection.source_dataset_id, projection.source_column_key)
                    )
        elif isinstance(rule, GroupAggregateRule):
            output = structural_dataset_id(rule)
            for component in (*rule.group_keys, *rule.aggregates):
                graph[(output, component.output_column_key)]
                if component.source_column_key is not None:
                    graph[(output, component.output_column_key)].append(
                        (rule.source_dataset_id, component.source_column_key)
                    )
    for link in derived_dataset_links(plan):
        inputs = link.source_level_column_keys or (link.source_column_key,)
        origins = [(link.consumer_dataset_id, key) for key in inputs]
        for key in (
            link.canonical_key_column_key,
            link.name_column_key,
            link.parent_key_column_key,
        ):
            if key is not None:
                graph[(link.derived_dataset_id, key)].extend(origins)
    for binding in hierarchy_consumer_bindings(plan):
        graph[
            (binding.link.consumer_dataset_id, binding.link.source_column_key)
        ].extend(
            (binding.rule.source_dataset_id, key)
            for key in binding.rule.source_level_column_keys
        )

    memo: dict[tuple[str, str], tuple[str, ...]] = {}

    def resolve(
        pointer: tuple[str, str], visiting: frozenset[tuple[str, str]]
    ) -> tuple[str, ...]:
        if pointer in memo:
            return memo[pointer]
        if pointer in visiting:
            raise ValueError(
                "Saved source-column origins contain a cycle. Prepare the data again."
            )
        if pointer in physical:
            result = (pointer[1],)
        else:
            inputs = graph.get(pointer)
            if inputs is None:
                inputs = [
                    (parent, pointer[1]) for parent in parents.get(pointer[0], ())
                ]
            result = tuple(
                dict.fromkeys(
                    key
                    for parent in inputs
                    for key in resolve(parent, visiting | {pointer})
                )
            )
        memo[pointer] = result
        return result

    return {
        dataset.name: {
            column.stable_key: resolve(
                (dataset.dataset_id, column.stable_key), frozenset()
            )
            for column in dataset.columns
        }
        for dataset in selection.datasets
    }
