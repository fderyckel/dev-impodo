"""Build bounded immutable categorical evidence from frozen source snapshots."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from impodo.artifacts import DataVersionSourceArtifactStore, ArtifactStoreError
from impodo.columnar_runtime import configure_columnar_runtime
from impodo.domain.mapping.contracts import (
    MAX_VALUE_MAPPINGS,
    CategoricalCoveragePolicy,
    DatasetMapping,
    MappingDefinition,
    RelationshipMapping,
    ScalarFieldMapping,
    ScalarValueSource,
)
from impodo.domain.mapping.scalar_values import (
    ScalarValueError,
    evaluate_scalar_mapping_value,
)
from impodo.domain.mapping.validation.evidence import (
    CategoricalCoverageEvidence,
    CategoricalFieldResult,
    CategoricalValueCount,
    MappingValidationIssue,
)
from impodo.domain.serialization import content_hash
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.source import SourceLoadError
from impodo.source_snapshot_io import (
    validate_snapshot_for_dataset,
    validate_source_snapshot_path,
)
from impodo.workspace_contracts import OdooSchemaCatalog, SourceSelection
from impodo.workspace_errors import WorkspaceError


configure_columnar_runtime()

import polars as pl  # noqa: E402


CATEGORICAL_SCAN_CONTRACT_HASH = content_hash(
    {
        "contract_version": 2,
        "input": "source_snapshot_value_columns",
        "blank": "trimmed_empty_excluded",
        "grouping": "exact_utf8_tuple",
        "maximum_distinct_values_per_field": MAX_VALUE_MAPPINGS,
        "dataset_reads": "one_projected_scan",
    }
)
CATEGORICAL_PROVIDER_SEMANTICS_HASH = content_hash(
    {
        "contract_version": 2,
        "explicit_match_choice": "str(raw).strip()",
        "exact_target_value": "evaluate_scalar_mapping_value",
        "relationship_choice": "str(raw).strip()",
        "blank_policy": "governed_separately",
        "target_reference_resolution": "deferred_to_preparation",
        "conditional_selection": "ordered_first_match_with_typed_inputs",
        "conditional_blank_domain": "included",
    }
)


class CategoricalSourceRepository(Protocol):
    """Read frozen source selections and their current immutable snapshots."""

    def get_source_selection(self, workspace_id: str) -> SourceSelection | None: ...

    def get_mapping_source_selection(
        self, workspace_id: str
    ) -> SourceSelection | None: ...

    def get_current_source_snapshots(
        self, workspace_id: str
    ) -> tuple[SourceSnapshot, ...]: ...


@dataclass(frozen=True, slots=True)
class CategoricalCoverageCollection:
    """Evidence plus deterministic validation issues produced by one scan."""

    evidence: CategoricalCoverageEvidence
    issues: tuple[MappingValidationIssue, ...]


@dataclass(frozen=True, slots=True)
class _CoverageField:
    path: str
    dataset: DatasetMapping
    target_field: str
    policy: CategoricalCoveragePolicy
    source_column_keys: tuple[str, ...]
    scalar: ScalarFieldMapping | None = None
    relationship: RelationshipMapping | None = None
    target_values: frozenset[str] = frozenset()
    unsupported_reason: str | None = None


class CategoricalCoverageService:
    """Scan each affected source dataset once and close every v11 domain."""

    def __init__(
        self,
        sources: CategoricalSourceRepository,
        artifacts: DataVersionSourceArtifactStore,
    ) -> None:
        self.sources = sources
        self.artifacts = artifacts

    def source_value_choices(
        self,
        workspace_id: str,
        dataset_id: str,
        source_column_key: str,
    ) -> tuple[dict[str, object], ...]:
        """Return bounded choices without materializing rich source rows."""

        selection = self.sources.get_source_selection(workspace_id)
        if selection is None:
            raise WorkspaceError("Frozen source evidence is incomplete")
        dataset = next(
            (item for item in selection.datasets if item.dataset_id == dataset_id),
            None,
        )
        if dataset is None:
            raise WorkspaceError(
                "Value matching is available for original frozen datasets"
            )
        if source_column_key not in {
            item.stable_key for item in dataset.columns
        }:
            raise WorkspaceError("Choose one current source column")
        frame = self._scan_dataset(
            workspace_id,
            selection,
            dataset_id,
            (source_column_key,),
        )
        counts = _single_column_counts(frame, source_column_key)
        if len(counts) > MAX_VALUE_MAPPINGS:
            raise WorkspaceError(
                "This column has too many distinct choices for quick matching"
            )
        return tuple(
            {"value": value, "count": count}
            for value, count in sorted(
                counts.items(),
                key=lambda item: (item[0].casefold(), item[0]),
            )
        )

    def collect(
        self,
        workspace_id: str,
        definition: MappingDefinition,
        selection: SourceSelection,
        schema: OdooSchemaCatalog,
    ) -> CategoricalCoverageCollection:
        """Build hash-bound evidence and blocking issues for the current mapping."""
        fields = _coverage_fields(definition, schema)
        by_dataset: dict[str, list[_CoverageField]] = {}
        for item in fields:
            by_dataset.setdefault(item.dataset.dataset_id, []).append(item)
        results: list[CategoricalFieldResult] = []
        issues: list[MappingValidationIssue] = []
        physical_selection = None
        selection_dataset_ids: set[str] = set()
        if by_dataset:
            physical_selection = self.sources.get_source_selection(workspace_id)
            if physical_selection is None:
                raise WorkspaceError("Frozen source evidence is incomplete")
            selection_dataset_ids = {
                item.dataset_id for item in physical_selection.datasets
            }
        for dataset_id in sorted(by_dataset):
            dataset_fields = by_dataset[dataset_id]
            supported_fields: list[_CoverageField] = []
            for item in dataset_fields:
                if item.unsupported_reason is None:
                    supported_fields.append(item)
                    continue
                results.append(_unsupported_result(item))
                issues.append(
                    _coverage_issue(
                        item,
                        "MAPPING_CATEGORICAL_RECIPE_UNSUPPORTED",
                        item.unsupported_reason,
                        "Use a supported set-based provider before publishing a recipe.",
                        severity="warning",
                    )
                )
            scan_fields: list[_CoverageField] = []
            for item in supported_fields:
                if item.source_column_keys:
                    scan_fields.append(item)
                    continue
                result, issue = _evaluate_field(item, pl.DataFrame())
                results.append(result)
                if issue is not None:
                    issues.append(issue)
            source_keys = tuple(
                sorted(
                    {
                        key
                        for item in scan_fields
                        for key in item.source_column_keys
                    }
                )
            )
            if dataset_id not in selection_dataset_ids:
                for item in scan_fields:
                    results.append(_unsupported_result(item))
                    issues.append(
                        _coverage_issue(
                            item,
                            "MAPPING_CATEGORICAL_RECIPE_UNSUPPORTED",
                            "Categorical evidence cannot yet scan this derived source provider.",
                            "Materialize it through a supported set-based provider before recipe use.",
                            severity="warning",
                        )
                    )
                continue
            if not scan_fields:
                continue
            assert physical_selection is not None
            frame = self._scan_dataset(
                workspace_id,
                physical_selection,
                dataset_id,
                source_keys,
            )
            for item in scan_fields:
                result, issue = _evaluate_field(item, frame)
                results.append(result)
                if issue is not None:
                    issues.append(issue)

        snapshots = (
            tuple(
                sorted(
                    self.sources.get_current_source_snapshots(workspace_id),
                    key=lambda item: item.dataset_id,
                )
            )
            if by_dataset
            else ()
        )
        evidence = CategoricalCoverageEvidence(
            mapping_content_hash=definition.content_hash,
            effective_source_selection_hash=selection.content_hash,
            source_snapshot_hashes=tuple(
                {
                    "dataset_id": item.dataset_id,
                    "logical_hash": item.logical_hash,
                    "parquet_sha256": item.parquet_sha256,
                }
                for item in snapshots
                if item.dataset_id in by_dataset
            ),
            scan_contract_hash=CATEGORICAL_SCAN_CONTRACT_HASH,
            provider_and_normalization_semantics_hash=(
                CATEGORICAL_PROVIDER_SEMANTICS_HASH
            ),
            target_schema_dependency_hash=_target_dependency_hash(fields),
            # Target existence/uniqueness remains the existing, explicit
            # REFERENCE_RESOLUTION preparation check in Phase 1.
            target_reference_evidence=None,
            field_results=tuple(
                sorted(results, key=lambda item: (item.dataset_id, item.path))
            ),
        )
        return CategoricalCoverageCollection(
            evidence=evidence,
            issues=tuple(
                sorted(
                    issues,
                    key=lambda item: (item.path, item.code, item.message),
                )
            ),
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
        except (ArtifactStoreError, OSError, SourceLoadError, pl.exceptions.PolarsError) as error:
            raise WorkspaceError(
                "The frozen source snapshot could not be verified"
            ) from error


def _coverage_fields(
    definition: MappingDefinition,
    schema: OdooSchemaCatalog,
) -> tuple[_CoverageField, ...]:
    schema_fields = {
        (model.name, field.name): field
        for model in schema.models
        for field in model.fields
    }
    fields: list[_CoverageField] = []
    for dataset_index, dataset in enumerate(definition.datasets):
        for field_index, scalar in enumerate(dataset.fields):
            if scalar.categorical_policy is None:
                continue
            metadata = schema_fields.get((dataset.target_model, scalar.target_field))
            keys = (
                (scalar.source_column_key,)
                if scalar.source_column_key is not None
                and scalar.value_source
                in {ScalarValueSource.SOURCE, ScalarValueSource.SOURCE_WITH_FALLBACK}
                else ()
            )
            if (
                scalar.value_source is ScalarValueSource.CONDITIONAL_RULES
                and scalar.selection_rules is not None
            ):
                keys = tuple(
                    sorted(
                        {
                            condition.source_column_key
                            for rule in scalar.selection_rules.rules
                            for condition in rule.conditions
                        }
                    )
                )
            fields.append(
                _CoverageField(
                    path=f"/datasets/{dataset_index}/fields/{field_index}",
                    dataset=dataset,
                    target_field=scalar.target_field,
                    policy=scalar.categorical_policy,
                    source_column_keys=keys,
                    scalar=scalar,
                    target_values=frozenset(
                        str(value)
                        for value, _label in (
                            metadata.selection if metadata is not None else ()
                        )
                    ),
                    unsupported_reason=(
                        "This scalar provider depends on a formula or reference lookup that the categorical set evaluator does not support."
                        if scalar.reference_lookup is not None
                        or bool(scalar.transform.formula)
                        else None
                    ),
                )
            )
        for relation_index, relation in enumerate(dataset.relationships):
            if relation.categorical_policy is None:
                continue
            fields.append(
                _CoverageField(
                    path=(
                        f"/datasets/{dataset_index}/relationships/{relation_index}"
                    ),
                    dataset=dataset,
                    target_field=relation.target_field,
                    policy=relation.categorical_policy,
                    source_column_keys=relation.source_column_keys,
                    relationship=relation,
                )
            )
    return tuple(fields)


def _single_column_counts(frame: pl.DataFrame, key: str) -> Counter[str]:
    return Counter(
        value
        for raw in frame.get_column(key).to_list()
        if raw is not None and (value := str(raw).strip())
    )


def _tuple_counts(
    frame: pl.DataFrame,
    keys: tuple[str, ...],
    *,
    trim_values: bool,
    include_blank: bool = False,
) -> Counter[tuple[str, ...]]:
    if not keys:
        return Counter()
    counts: Counter[tuple[str, ...]] = Counter()
    for row in frame.select(keys).iter_rows():
        values = tuple(
            ""
            if value is None
            else (str(value).strip() if trim_values else str(value))
            for value in row
        )
        if include_blank or any(value.strip() for value in values):
            counts[values] += 1
    return counts


def _evaluate_field(
    item: _CoverageField,
    frame: pl.DataFrame,
) -> tuple[CategoricalFieldResult, MappingValidationIssue | None]:
    if item.scalar is not None and not item.source_column_keys:
        raw_counts: Counter[tuple[str, ...]] = Counter()
        uncovered = _constant_scalar_uncovered(item)
        distinct = ()
    else:
        raw_counts = _tuple_counts(
            frame,
            item.source_column_keys,
            trim_values=(
                item.policy is not CategoricalCoveragePolicy.EXACT_TARGET_VALUE
            ),
            include_blank=(
                item.scalar is not None
                and item.scalar.value_source
                is ScalarValueSource.CONDITIONAL_RULES
            ),
        )
        if len(raw_counts) > MAX_VALUE_MAPPINGS:
            return (
                _unsupported_result(item),
                _coverage_issue(
                    item,
                    "MAPPING_CATEGORICAL_DOMAIN_TOO_LARGE",
                    f"This field has {len(raw_counts)} distinct source choices; the evidence limit is {MAX_VALUE_MAPPINGS}.",
                    "Reduce or govern the source domain before confirming this mapping.",
                ),
            )
        distinct = tuple(
            CategoricalValueCount(values=values, count=count)
            for values, count in sorted(raw_counts.items())
        )
        uncovered = _uncovered_values(item, raw_counts)
    status = "UNCOVERED" if uncovered else "COVERED"
    result = CategoricalFieldResult(
        path=item.path,
        dataset_id=item.dataset.dataset_id,
        target_field=item.target_field,
        policy=item.policy.value,
        source_column_keys=item.source_column_keys,
        distinct_values=distinct,
        uncovered_values=uncovered,
        status=status,
    )
    issue = (
        _coverage_issue(
            item,
            "MAPPING_CATEGORICAL_COVERAGE_INCOMPLETE",
            f"{len(uncovered)} source choice(s) are not covered by the declared categorical policy.",
            "Match every source choice or correct it to an exact current target value.",
        )
        if uncovered
        else None
    )
    return result, issue


def _constant_scalar_uncovered(item: _CoverageField) -> tuple[tuple[str, ...], ...]:
    assert item.scalar is not None
    try:
        proposed = evaluate_scalar_mapping_value(item.scalar, None)
    except ScalarValueError:
        return (("<invalid-provider-value>",),)
    if proposed is None:
        return ()
    value = str(proposed)
    return () if value in item.target_values else ((value,),)


def _uncovered_values(
    item: _CoverageField,
    raw_counts: Mapping[tuple[str, ...], int],
) -> tuple[tuple[str, ...], ...]:
    if item.policy in {
        CategoricalCoveragePolicy.EXPLICIT_VALUE_MATCH,
        CategoricalCoveragePolicy.EXPLICIT_KEY_MATCH,
    }:
        mappings = (
            item.scalar.value_mappings
            if item.scalar is not None
            else item.relationship.resolver.value_mappings  # type: ignore[union-attr]
        )
        covered = {mapping.source_value for mapping in mappings}
        uncovered = tuple(
            values
            for values in sorted(raw_counts)
            if len(values) != 1 or values[0] not in covered
        )
        return (*uncovered, *_fallback_uncovered(item))
    if item.policy is CategoricalCoveragePolicy.EXACT_BUSINESS_KEY:
        return ()
    assert item.scalar is not None
    uncovered: list[tuple[str, ...]] = []
    for values in sorted(raw_counts):
        try:
            proposed = evaluate_scalar_mapping_value(
                item.scalar,
                values[0] if values else None,
                source_values_by_key=dict(
                    zip(item.source_column_keys, values, strict=True)
                ),
            )
        except ScalarValueError:
            uncovered.append(values)
            continue
        if proposed is not None and str(proposed) not in item.target_values:
            uncovered.append(values)
    return (*uncovered, *_fallback_uncovered(item))


def _fallback_uncovered(
    item: _CoverageField,
) -> tuple[tuple[str, ...], ...]:
    scalar = item.scalar
    if (
        scalar is None
        or scalar.value_source is not ScalarValueSource.SOURCE_WITH_FALLBACK
    ):
        return ()
    try:
        proposed = evaluate_scalar_mapping_value(scalar, None)
    except ScalarValueError:
        return (("<invalid-fallback-value>",),)
    if proposed is None or str(proposed) in item.target_values:
        return ()
    return ((f"<fallback:{proposed}>",),)


def _unsupported_result(item: _CoverageField) -> CategoricalFieldResult:
    return CategoricalFieldResult(
        path=item.path,
        dataset_id=item.dataset.dataset_id,
        target_field=item.target_field,
        policy=item.policy.value,
        source_column_keys=item.source_column_keys,
        distinct_values=(),
        uncovered_values=(),
        status="UNSUPPORTED",
    )


def _coverage_issue(
    item: _CoverageField,
    code: str,
    message: str,
    remediation: str,
    *,
    severity: str = "error",
) -> MappingValidationIssue:
    return MappingValidationIssue(
        code=code,
        severity=severity,
        path=f"{item.path}/categorical_policy",
        message=message,
        remediation=remediation,
        dataset_id=item.dataset.dataset_id,
        target_model=item.dataset.target_model,
        target_field=item.target_field,
    )


def _target_dependency_hash(fields: Sequence[_CoverageField]) -> str:
    return content_hash(
        [
            {
                "dataset_id": item.dataset.dataset_id,
                "target_model": item.dataset.target_model,
                "target_field": item.target_field,
                "policy": item.policy.value,
                "target_values": sorted(item.target_values),
                "resolver_key_fields": (
                    [
                        mapping.target_field
                        for mapping in item.relationship.resolver.key_mappings
                    ]
                    if item.relationship is not None
                    else []
                ),
                "resolver_scope_fields": (
                    [
                        mapping.target_field
                        for mapping in item.relationship.resolver.scope_mappings
                    ]
                    if item.relationship is not None
                    else []
                ),
                "relationship_kind": (
                    item.relationship.kind
                    if item.relationship is not None
                    else None
                ),
                "related_model": (
                    item.relationship.resolver.model
                    if item.relationship is not None
                    else None
                ),
            }
            for item in fields
        ]
    )
