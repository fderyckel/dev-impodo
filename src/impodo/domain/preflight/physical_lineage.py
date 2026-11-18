"""Resolve saved prepared inputs to exact physical cells without replaying rows.

Column rules describe projections; canonical row lineage supplies the physical
rows. Resolution fails as a whole when flattened lineage cannot distinguish a
join role or a union branch. The index contains metadata only and performs no
repository, artifact, or value access.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from impodo.domain.preparation.staging_contracts import CanonicalLineage
from impodo.domain.source_binding import SourceOriginKind
from impodo.domain.structural import (
    ExactJoinRule,
    GroupAggregateRule,
    UnionAllRule,
    structural_dataset_id,
)
from impodo.domain.workspace.contracts import SourceSelection
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    DerivedEntityRule,
    HierarchicalLookupRule,
    RelatedDatasetRule,
    derived_dataset_links,
    hierarchy_consumer_bindings,
    related_dataset_links,
)

from .original_values import OriginalSourceCoordinate

_Pointer = tuple[str, str]


@dataclass(frozen=True, slots=True)
class _ColumnRule:
    inputs: tuple[_Pointer, ...]
    choice: bool = False
    multiple: bool = False


class PhysicalColumnIndex:
    """Index saved column projections once for one bounded workbook operation."""

    def __init__(
        self,
        effective: SourceSelection,
        physical: SourceSelection,
        plan: DerivedEntityPlan | None,
    ) -> None:
        if effective.data_version_id != physical.data_version_id:
            raise ValueError("Saved source selections identify different Data versions")
        if plan is not None and plan.source_selection_hash != physical.content_hash:
            raise ValueError(
                "Saved preparation plan identifies another physical selection"
            )
        self._selection_hash = effective.content_hash
        self._plan_hash = plan.content_hash if plan is not None else None
        self._effective = {item.dataset_id: item for item in effective.datasets}
        self._physical = {item.dataset_id: item for item in physical.datasets}
        if len(self._effective) != len(effective.datasets) or len(
            self._physical
        ) != len(physical.datasets):
            raise ValueError("Saved source dataset identities are not unique")
        if any(
            len({column.stable_key for column in item.columns}) != len(item.columns)
            for item in effective.datasets
        ):
            raise ValueError("Saved effective source columns are not unique")
        self._physical_keys = {
            item.dataset_id: frozenset(column.stable_key for column in item.columns)
            for item in physical.datasets
            if item.origin is SourceOriginKind.FILE
        }
        for dataset_id, keys in self._physical_keys.items():
            original = self._physical[dataset_id]
            if len(keys) != len(original.columns):
                raise ValueError("Saved physical source columns are not unique")
            current = self._effective.get(dataset_id)
            if current is not None and (
                replace(current, columns=original.columns) != original
                or current.columns[: len(original.columns)] != original.columns
            ):
                raise ValueError("Saved physical source metadata changed")
        self._rules: dict[_Pointer, _ColumnRule] = {}
        self._parents: dict[str, tuple[str, ...]] = {}
        self._joins: dict[str, tuple[str, str]] = {}
        self._ancestor_cache: dict[str, frozenset[str]] = {}
        self._build(plan)
        self._ambiguous = {
            dataset_id: self._ancestors(left) & self._ancestors(right)
            for dataset_id, (left, right) in self._joins.items()
        }

    def resolve_direct(
        self, dataset_id: str, source_keys: tuple[str, ...], source_row: int
    ) -> tuple[OriginalSourceCoordinate, ...] | None:
        """Prove unchanged physical file cells when no row lineage was retained.

        This path cannot follow a generated or derived column. It preserves the
        direct-file proof without inventing a canonical row or physical parent.
        """

        if (
            type(source_row) is not int
            or source_row < 1
            or dataset_id not in self._effective
            or dataset_id not in self._physical_keys
            or any(key not in self._physical_keys[dataset_id] for key in source_keys)
        ):
            return None
        return tuple(
            OriginalSourceCoordinate(
                dataset_id, self._physical[dataset_id].name, source_row, key
            )
            for key in dict.fromkeys(source_keys)
        )

    def resolve(
        self,
        dataset_id: str,
        source_keys: tuple[str, ...],
        lineage: CanonicalLineage,
    ) -> tuple[OriginalSourceCoordinate, ...] | None:
        """Prove every logical input, deduplicate cells and preserve input order.

        Multiple physical rows are accepted only for explicit saved grouping or
        extraction operations. Missing outer-join inputs and ambiguous reused
        physical roles never authorize a partial group.
        """

        dataset = self._effective.get(dataset_id)
        if (
            dataset is None
            or lineage.dataset != dataset.name
            or lineage.source_selection_hash != self._selection_hash
            or lineage.derived_plan_hash != self._plan_hash
        ):
            return None
        if any(
            physical_id not in self._physical
            or any(type(row) is not int or row < 1 for row in rows)
            for physical_id, rows in lineage.physical_sources.items()
        ):
            return None
        available = {column.stable_key for column in dataset.columns}
        if any(key not in available for key in source_keys):
            return None
        cache: dict[
            tuple[_Pointer, bool], tuple[OriginalSourceCoordinate, ...] | None
        ] = {}
        result: list[OriginalSourceCoordinate] = []
        for key in source_keys:
            cells = self._resolve_column(
                (dataset_id, key), lineage, False, frozenset(), cache
            )
            if cells is None:
                return None
            result.extend(cells)
        return tuple(dict.fromkeys(result))

    def _resolve_column(
        self,
        pointer: _Pointer,
        lineage: CanonicalLineage,
        multiple: bool,
        visiting: frozenset[_Pointer],
        cache: dict[tuple[_Pointer, bool], tuple[OriginalSourceCoordinate, ...] | None],
    ) -> tuple[OriginalSourceCoordinate, ...] | None:
        identity = (pointer, multiple)
        if identity in cache:
            return cache[identity]
        if pointer in visiting:
            raise ValueError(
                "Saved source-column origins contain a cycle. Prepare the data again."
            )
        dataset_id, key = pointer
        if key in self._physical_keys.get(dataset_id, ()):
            rows = lineage.physical_sources.get(dataset_id, ())
            result = (
                tuple(
                    OriginalSourceCoordinate(
                        dataset_id, self._physical[dataset_id].name, row, key
                    )
                    for row in rows
                )
                if rows and (multiple or len(rows) == 1)
                else None
            )
        else:
            rule = self._rules.get(pointer)
            if rule is None:
                return None
            results = []
            branch_sources: list[frozenset[str]] = []
            for source in rule.inputs:
                if rule.choice and not self._ancestors(source[0]).intersection(
                    lineage.physical_sources
                ):
                    continue
                cells = self._resolve_column(
                    source,
                    lineage,
                    multiple or rule.multiple,
                    visiting | {pointer},
                    cache,
                )
                if cells is None:
                    cache[identity] = None
                    return None
                results.append(cells)
                if rule.choice:
                    branch_sources.append(self._ancestors(source[0]))
            if rule.choice:
                choices = set(results)
                distinct_branches = all(
                    not left.intersection(right)
                    for index, left in enumerate(branch_sources)
                    for right in branch_sources[index + 1 :]
                )
                result = (
                    tuple(sorted({cell for cells in results for cell in cells}))
                    if multiple and distinct_branches and results
                    else next(iter(choices))
                    if len(choices) == 1
                    else None
                )
            else:
                result = tuple(
                    dict.fromkeys(cell for cells in results for cell in cells)
                )
        if result is not None and any(
            cell.dataset_id in self._ambiguous.get(dataset_id, ()) for cell in result
        ):
            result = None
        cache[identity] = result
        return result

    def _ancestors(
        self, dataset_id: str, visiting: frozenset[str] = frozenset()
    ) -> frozenset[str]:
        if dataset_id in self._ancestor_cache:
            return self._ancestor_cache[dataset_id]
        if dataset_id in visiting:
            raise ValueError(
                "Saved source dataset origins contain a cycle. Prepare the data again."
            )
        result = (
            frozenset({dataset_id})
            if dataset_id in self._physical
            else frozenset(
                source
                for parent in self._parents.get(dataset_id, ())
                for source in self._ancestors(parent, visiting | {dataset_id})
            )
        )
        self._ancestor_cache[dataset_id] = result
        return result

    def _build(self, plan: DerivedEntityPlan | None) -> None:
        for rule in plan.rules if plan else ():
            if isinstance(rule, ExactJoinRule):
                output = structural_dataset_id(rule)
                self._parents[output] = (rule.left_dataset_id, rule.right_dataset_id)
                self._joins[output] = (rule.left_dataset_id, rule.right_dataset_id)
                for projection in rule.projections:
                    self._rules[(output, projection.output_column_key)] = _ColumnRule(
                        ((projection.source_dataset_id, projection.source_column_key),)
                    )
            elif isinstance(rule, UnionAllRule):
                output = structural_dataset_id(rule)
                self._parents[output] = tuple(
                    branch.source_dataset_id for branch in rule.branches
                )
                for column in rule.output_columns:
                    self._rules[(output, column.column_key)] = _ColumnRule(
                        tuple(
                            (projection.source_dataset_id, projection.source_column_key)
                            for branch in rule.branches
                            for projection in branch.projections
                            if projection.output_column_key == column.column_key
                        ),
                        choice=True,
                    )
            elif isinstance(rule, GroupAggregateRule):
                output = structural_dataset_id(rule)
                self._parents[output] = (rule.source_dataset_id,)
                for component in (*rule.group_keys, *rule.aggregates):
                    self._rules[(output, component.output_column_key)] = _ColumnRule(
                        ((rule.source_dataset_id, component.source_column_key),)
                        if component.source_column_key is not None
                        else (),
                        multiple=True,
                    )
        related_rules = (
            tuple(rule for rule in plan.rules if isinstance(rule, RelatedDatasetRule))
            if plan
            else ()
        )
        for rule, link in zip(related_rules, related_dataset_links(plan), strict=True):
            for dataset_id, multiple in (
                (link.parent_dataset_id, True),
                (link.child_dataset_id, False),
            ):
                self._parents[dataset_id] = (rule.source_dataset_id,)
                dataset = self._effective.get(dataset_id)
                for column in dataset.columns if dataset else ():
                    self._rules[(dataset_id, column.stable_key)] = _ColumnRule(
                        ((rule.source_dataset_id, column.stable_key),),
                        multiple=multiple,
                    )
        lookup_rules = (
            tuple(
                rule
                for rule in plan.rules
                if isinstance(rule, (DerivedEntityRule, HierarchicalLookupRule))
            )
            if plan
            else ()
        )
        for rule, link in zip(lookup_rules, derived_dataset_links(plan), strict=True):
            self._parents[link.derived_dataset_id] = (link.consumer_dataset_id,)
            if isinstance(rule, DerivedEntityRule):
                for key in (
                    link.canonical_key_column_key,
                    link.name_column_key,
                    link.parent_key_column_key,
                ):
                    if key is not None:
                        self._rules[(link.derived_dataset_id, key)] = _ColumnRule(
                            ((link.consumer_dataset_id, link.source_column_key),),
                            multiple=True,
                        )
            if link.company_column_key is not None:
                self._rules[(link.derived_dataset_id, link.company_column_key)] = (
                    _ColumnRule(())
                )
        for binding in hierarchy_consumer_bindings(plan):
            source = binding.rule.source_dataset_id
            self._rules[
                (binding.link.consumer_dataset_id, binding.link.source_column_key)
            ] = _ColumnRule(
                tuple((source, key) for key in binding.rule.source_level_column_keys)
            )
            for key, original in zip(
                binding.link.source_level_column_keys,
                binding.rule.source_level_column_keys,
                strict=True,
            ):
                if (binding.link.consumer_dataset_id, key) != (source, original):
                    self._rules[(binding.link.consumer_dataset_id, key)] = _ColumnRule(
                        ((source, original),)
                    )
            if binding.company_source_column_key is not None:
                self._rules[
                    (
                        binding.link.consumer_dataset_id,
                        binding.company_source_column_key,
                    )
                ] = _ColumnRule(())
