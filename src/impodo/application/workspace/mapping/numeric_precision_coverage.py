"""Predict target numeric precision loss while field matches are authored."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from impodo.application.data_version.source_snapshots import (
    validate_snapshot_for_dataset,
    validate_source_snapshot_path,
)
from impodo.application.shared.artifacts import (
    ArtifactStoreError,
    DataVersionSourceArtifactStore,
)
from impodo.application.shared.columnar_runtime import configure_columnar_runtime
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    MappingDefinition,
    MappingTargetMode,
    ScalarFieldMapping,
    ScalarValueSource,
)
from impodo.domain.mapping.row_inclusion import row_is_included
from impodo.domain.mapping.scalar_values import (
    ScalarValueError,
    evaluate_scalar_mapping_value,
)
from impodo.domain.mapping.source_conditions import SourceConditionValueError
from impodo.domain.mapping.validation.evidence import MappingValidationIssue
from impodo.domain.preparation.source import SourceLoadError
from impodo.domain.recipe.value_rules import (
    CompiledArithmeticFormula,
    compile_arithmetic_formula,
    validate_formula,
)
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.domain.target_numeric_precision import TargetNumericPrecisionLosses
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SourceDataset,
    SourceSelection,
)
from impodo.domain.workspace.errors import WorkspaceError

configure_columnar_runtime()

import polars as pl  # noqa: E402


class NumericPrecisionSourceRepository(Protocol):
    """Provide immutable physical source evidence for the Stage 3 scan."""

    def get_source_selection(self, workspace_id: str) -> SourceSelection | None:
        """Return the physical frozen source selection."""
        ...

    def get_current_source_snapshots(
        self,
        workspace_id: str,
    ) -> tuple[SourceSnapshot, ...]:
        """Return the current immutable source snapshots."""
        ...


@dataclass(frozen=True, slots=True)
class _PrecisionField:
    """Bind one writable numeric mapping to captured Odoo precision."""

    dataset_index: int
    field_index: int
    dataset: DatasetMapping
    source: SourceDataset
    mapping: ScalarFieldMapping
    digits: tuple[int, int]
    arithmetic_formula: CompiledArithmeticFormula | None


class NumericPrecisionCoverageService:
    """Warn when current Match data values can later block an exact Odoo write."""

    def __init__(
        self,
        sources: NumericPrecisionSourceRepository,
        artifacts: DataVersionSourceArtifactStore,
    ) -> None:
        self.sources = sources
        self.artifacts = artifacts

    def collect(
        self,
        workspace_id: str,
        definition: MappingDefinition,
        selection: SourceSelection,
        schema: OdooSchemaCatalog,
    ) -> tuple[MappingValidationIssue, ...]:
        """Return aggregate, acknowledgement-required precision warnings.

        Match data does not yet know the final CREATE/UPDATE intent set, so this
        scan is deliberately predictive.  It evaluates current source-backed
        scalar rules exactly, while Final review and Load into Odoo retain the
        authoritative intended-write check.
        """

        physical = self.sources.get_source_selection(workspace_id)
        if physical is None:
            return ()
        physical_by_id = {item.dataset_id: item for item in physical.datasets}
        schema_fields = {
            (model.name, field.name): field
            for model in schema.models
            for field in model.fields
        }
        fields_by_dataset: dict[str, list[_PrecisionField]] = {}
        for dataset_index, dataset in enumerate(definition.datasets):
            source = physical_by_id.get(dataset.dataset_id)
            if source is None or dataset.mode is MappingTargetMode.REFERENCE:
                continue
            allowed_formula_names = {
                f"column_{column.ordinal}" for column in source.columns
            }
            for field_index, mapping in enumerate(dataset.fields):
                metadata = schema_fields.get(
                    (dataset.target_model, mapping.target_field)
                )
                digits = getattr(metadata, "digits", None)
                if (
                    digits is None
                    or mapping.validate_only
                    or mapping.value_source is ScalarValueSource.ODOO_DEFAULT
                    or mapping.reference_lookup is not None
                ):
                    continue
                arithmetic_formula = None
                if mapping.transform.formula.strip():
                    try:
                        arithmetic_formula = compile_arithmetic_formula(
                            mapping.transform.formula,
                            allowed_names=allowed_formula_names,
                        )
                    except ValueError:
                        # Semantic validation owns invalid formula feedback.
                        continue
                fields_by_dataset.setdefault(dataset.dataset_id, []).append(
                    _PrecisionField(
                        dataset_index=dataset_index,
                        field_index=field_index,
                        dataset=dataset,
                        source=source,
                        mapping=mapping,
                        digits=(int(digits[0]), int(digits[1])),
                        arithmetic_formula=arithmetic_formula,
                    )
                )
        if not fields_by_dataset:
            return ()

        issues: list[MappingValidationIssue] = []
        for dataset_id in sorted(fields_by_dataset):
            fields = fields_by_dataset[dataset_id]
            source = fields[0].source
            source_keys = _required_source_keys(source, fields)
            frame = self._scan_dataset(
                workspace_id,
                physical,
                dataset_id,
                source_keys,
            )
            losses = {
                (item.dataset_index, item.field_index): (
                    item,
                    TargetNumericPrecisionLosses(),
                )
                for item in fields
            }
            ordinal_keys = {
                column.ordinal: column.stable_key for column in source.columns
            }
            for row in frame.iter_rows(named=True):
                try:
                    if not row_is_included(fields[0].dataset.row_inclusion, row):
                        continue
                except SourceConditionValueError as error:
                    raise WorkspaceError(
                        "The saved row-inclusion rule cannot be evaluated on "
                        "the current frozen source"
                    ) from error
                source_values_by_ordinal = {
                    ordinal: row.get(key) for ordinal, key in ordinal_keys.items()
                }
                for item in fields:
                    raw = _raw_scalar_value(item.mapping, row)
                    try:
                        proposed = evaluate_scalar_mapping_value(
                            item.mapping,
                            raw,
                            source_values_by_ordinal=source_values_by_ordinal,
                            source_values_by_key=row,
                            arithmetic_formula=item.arithmetic_formula,
                        )
                    except ScalarValueError:
                        # Existing value/type checks own invalid-row feedback.
                        continue
                    if proposed is None:
                        continue
                    losses[(item.dataset_index, item.field_index)][1].add(
                        item.dataset.target_model,
                        item.mapping.target_field,
                        item.digits,
                        proposed,
                    )
            for item, collector in losses.values():
                summary = collector.first_summary()
                if summary is None:
                    continue
                issues.append(_precision_issue(item, summary))
        return tuple(
            sorted(
                issues,
                key=lambda item: (item.path, item.code, item.message),
            )
        )

    def _scan_dataset(
        self,
        workspace_id: str,
        selection: SourceSelection,
        dataset_id: str,
        source_column_keys: Sequence[str],
    ) -> pl.DataFrame:
        dataset = next(
            (item for item in selection.datasets if item.dataset_id == dataset_id),
            None,
        )
        snapshot = next(
            (
                item
                for item in self.sources.get_current_source_snapshots(workspace_id)
                if item.dataset_id == dataset_id
            ),
            None,
        )
        if dataset is None or snapshot is None:
            raise WorkspaceError("Frozen source snapshot is incomplete")
        try:
            validate_snapshot_for_dataset(selection, dataset, snapshot)
            value_columns = {
                item.stable_key: item.value_column
                for item in snapshot.schema.columns
            }
            if any(key not in value_columns for key in source_column_keys):
                raise SourceLoadError("Source snapshot projection is incomplete")
            with self.artifacts.materialize_source_snapshot(
                selection.data_version_id,
                snapshot.parquet_storage_key,
                expected_sha256=snapshot.parquet_sha256,
            ) as path:
                snapshot_path = validate_source_snapshot_path(path, snapshot)
                return (
                    pl.scan_parquet(snapshot_path)
                    .select(
                        pl.col(value_columns[key]).alias(key)
                        for key in source_column_keys
                    )
                    .collect(engine="streaming")
                )
        except (
            ArtifactStoreError,
            OSError,
            SourceLoadError,
            pl.exceptions.PolarsError,
        ) as error:
            raise WorkspaceError(
                "The frozen source snapshot could not be verified"
            ) from error


def _required_source_keys(
    source: SourceDataset,
    fields: Sequence[_PrecisionField],
) -> tuple[str, ...]:
    keys = {
        condition.source_column_key
        for condition in fields[0].dataset.row_inclusion.conditions
    }
    columns_by_ordinal = {column.ordinal: column.stable_key for column in source.columns}
    allowed_formula_names = {
        f"column_{column.ordinal}" for column in source.columns
    }
    for item in fields:
        mapping = item.mapping
        if mapping.source_column_key is not None:
            keys.add(mapping.source_column_key)
        if mapping.concatenation is not None:
            keys.update(mapping.concatenation.source_column_keys)
        if mapping.selection_rules is not None:
            keys.update(
                condition.source_column_key
                for rule in mapping.selection_rules.rules
                for condition in rule.conditions
            )
        if mapping.transform.formula.strip():
            parsed = validate_formula(
                mapping.transform.formula,
                allowed_names=allowed_formula_names,
            )
            for node in ast.walk(parsed):
                if not isinstance(node, ast.Name):
                    continue
                name = node.id
                if not name.startswith("column_") or not name[7:].isdigit():
                    continue
                stable_key = columns_by_ordinal.get(int(name[7:]))
                if stable_key is not None:
                    keys.add(stable_key)
    if not keys and source.columns:
        keys.add(source.columns[0].stable_key)
    return tuple(sorted(keys))


def _raw_scalar_value(
    mapping: ScalarFieldMapping,
    row: dict[str, object],
) -> object:
    if mapping.concatenation is not None:
        return tuple(row.get(key) for key in mapping.concatenation.source_column_keys)
    if mapping.source_column_key is not None:
        return row.get(mapping.source_column_key)
    return None


def _precision_issue(item: _PrecisionField, summary) -> MappingValidationIssue:
    precision, scale = summary.digits
    requirement = (
        f"They need at least {summary.required_places} decimal places."
        if summary.required_places > scale
        else "They exceed the total number of digits Odoo permits."
    )
    zero_warning = (
        f" Rounding to {scale} places would turn "
        f"{summary.zeroed_positive_count:,} positive value(s) into zero."
        if summary.zeroed_positive_count
        else ""
    )
    return MappingValidationIssue(
        code="MAPPING_TARGET_NUMERIC_PRECISION_LOSS",
        severity="warning",
        path=(
            f"/datasets/{item.dataset_index}/fields/{item.field_index}"
            "/transform/decimal_places"
        ),
        message=(
            f"{item.dataset.target_model}.{item.mapping.target_field}: "
            f"{summary.count:,} mapped value(s) cannot be stored exactly at "
            f"Odoo precision ({precision}, {scale}). {requirement}{zero_warning} "
            "If an affected value must be created or updated, Load into Odoo "
            "will stop rather than change it silently."
        ),
        remediation=(
            "Open Decimal rounding for this field and select Round to this Odoo "
            "precision only if the business accepts the change; otherwise change "
            "the Odoo field precision, refresh Odoo data, and check matches again."
        ),
        dataset_id=item.dataset.dataset_id,
        source_column_key=item.mapping.source_column_key,
        target_model=item.dataset.target_model,
        target_field=item.mapping.target_field,
    )
