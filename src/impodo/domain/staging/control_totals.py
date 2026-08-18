"""Extracted control totals domain behavior."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from typing import Mapping

from ..mapping.contracts import BusinessControlTotal, MappingDefinition
from ...models import PreparedRecord, canonical_json_bytes
from ...source import PreparedBundle
from ...staging_contracts import CanonicalControlTotal
from ...workspace_contracts import SourceSelection
from ..errors import ReadinessError


@dataclass(slots=True)
class _ControlFieldState:
    """Mutable constant-size accumulator shared by controls on one field."""

    first_control: BusinessControlTotal
    actual: Decimal = Decimal("0")
    included: int = 0
    empty: int = 0


@dataclass(slots=True)
class CompiledControlTotalAccumulator:
    """Accumulate declared totals one prepared row at a time."""

    definition: MappingDefinition
    dataset_name_by_id: Mapping[str, str]
    states: dict[str, dict[str, _ControlFieldState]]

    @classmethod
    def compile(
        cls,
        definition: MappingDefinition,
        selection: SourceSelection,
    ) -> "CompiledControlTotalAccumulator":
        dataset_name_by_id = {item.dataset_id: item.name for item in selection.datasets}
        states: dict[str, dict[str, _ControlFieldState]] = {}
        for dataset in definition.datasets:
            dataset_name = dataset_name_by_id[dataset.dataset_id]
            by_field: dict[str, _ControlFieldState] = {}
            for control in dataset.effective_control_totals:
                by_field.setdefault(
                    control.target_field,
                    _ControlFieldState(first_control=control),
                )
            states[dataset_name] = by_field
        return cls(
            definition=definition,
            dataset_name_by_id=dataset_name_by_id,
            states=states,
        )

    def add(self, record: PreparedRecord) -> None:
        """Add one row's prepared numeric values to configured field totals."""

        self.add_values(record.dataset, record.scalar_values)

    def add_values(
        self,
        dataset: str,
        scalar_values: Mapping[str, object],
    ) -> None:
        """Add native columnar values without constructing a prepared record."""

        for target_field, state in self.states.get(dataset, {}).items():
            value = scalar_values.get(target_field)
            if value is None:
                state.empty += 1
                continue
            if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
                control = state.first_control
                raise ReadinessError(
                    f"The named total {control.name!r} did not produce "
                    "numeric prepared values"
                )
            state.actual += Decimal(value)
            state.included += 1

    def target_fields(self, dataset: str) -> tuple[str, ...]:
        """Return the stable fields needed by a native aggregate plan."""

        return tuple(sorted(self.states.get(dataset, {})))

    def add_precomputed(
        self,
        dataset: str,
        *,
        target_field: str,
        actual_total: str,
        included_rows: int,
        empty_rows: int,
    ) -> None:
        """Merge one exact set-based total without receiving row values."""

        state = self.states.get(dataset, {}).get(target_field)
        if state is None or included_rows < 0 or empty_rows < 0:
            raise ReadinessError("Native control-total evidence is invalid")
        state.actual += Decimal(actual_total)
        state.included += included_rows
        state.empty += empty_rows

    def report(self) -> tuple[CanonicalControlTotal, ...]:
        """Build the existing deterministic portable total evidence."""

        results: list[CanonicalControlTotal] = []
        for dataset in self.definition.datasets:
            dataset_name = self.dataset_name_by_id[dataset.dataset_id]
            states = self.states[dataset_name]
            for control in dataset.effective_control_totals:
                state = states[control.target_field]
                control_id = (
                    "sha256:"
                    + sha256(
                        canonical_json_bytes(
                            {
                                "mapping_hash": self.definition.content_hash,
                                "dataset": dataset_name,
                                "name": control.name,
                                "target_field": control.target_field,
                                "expected_total": control.expected_total,
                                "unit": control.unit,
                                "tolerance": control.tolerance,
                            }
                        )
                    ).hexdigest()
                )
                results.append(
                    CanonicalControlTotal(
                        control_id=control_id,
                        name=control.name,
                        dataset=dataset_name,
                        target_field=control.target_field,
                        expected_total=control.expected_total,
                        actual_total=format(state.actual, "f"),
                        tolerance=control.tolerance,
                        unit=control.unit,
                        included_rows=state.included,
                        empty_rows=state.empty,
                    )
                )
        return tuple(sorted(results, key=lambda item: item.control_id))


def _evaluate_control_totals(
    definition: MappingDefinition,
    selection: SourceSelection,
    prepared: PreparedBundle,
) -> tuple[CanonicalControlTotal, ...]:
    """Evaluate only explicitly declared sums over canonical numeric values."""

    accumulator = CompiledControlTotalAccumulator.compile(definition, selection)
    for record in prepared.records:
        accumulator.add(record)
    return accumulator.report()


evaluate_control_totals = _evaluate_control_totals
