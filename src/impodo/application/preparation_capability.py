"""Compile truthful full-pipeline capacity before preparation starts.

The transformation engine is only one part of preparation.  This module keeps
the route decision honest by reporting the behavior and capacity of every
required Stage E-G step and by using the smallest stage capacity for admission.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from ..derived_entities import DerivedEntityPlan
from ..domain.coverage import ReferenceBundle
from ..domain.mapping.contracts import MappingDefinition
from ..domain.source_snapshot import SourceSnapshot
from ..domain.staging.scale import (
    COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
    MATERIALIZED_BROWSER_EVALUATION_ROW_LIMIT,
)
from ..quality import (
    MANDATORY_QUALITY_FAMILIES,
    QualityRuleFamily,
    QualityRuleSet,
    QualityRuleSource,
)
from ..workspace_contracts import SourceSelection
from ..domain.errors import ReadinessError
from .bounded_preparation import (
    direct_preparation_row_limit,
    supports_bounded_direct_preparation,
)


class PreparationRouteBehavior(str, Enum):
    """Observable execution behavior for one preparation stage."""

    NATIVE_COLUMNAR = "NATIVE_COLUMNAR"
    BOUNDED_PYTHON = "BOUNDED_PYTHON"
    BOUNDED_DURABLE = "BOUNDED_DURABLE"
    BOUNDED_RUNTIME_GUARDED = "BOUNDED_RUNTIME_GUARDED"
    MATERIALIZING = "MATERIALIZING"
    DEFERRED = "DEFERRED"


@dataclass(frozen=True, slots=True)
class PreparationStageCapability:
    """One stage's selected route, capacity, and sanitized explanation."""

    stage: str
    behavior: PreparationRouteBehavior
    supported_rows: int | None
    reason_codes: tuple[str, ...] = ()

    @property
    def constrains_admission(self) -> bool:
        """Whether this stage executes as part of the current preparation."""

        return self.supported_rows is not None

    def to_portable_dict(self) -> dict[str, object]:
        """Return telemetry containing no source values or business data."""

        return {
            "stage": self.stage,
            "behavior": self.behavior.value,
            "supported_rows": self.supported_rows,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class PreparationCapabilityManifest:
    """Run-wide preparation admission derived from all required stages."""

    physical_rows: int
    supported_rows: int
    stages: tuple[PreparationStageCapability, ...]

    @property
    def admitted(self) -> bool:
        """Whether the complete current Stage E-G path is supported."""

        return self.physical_rows <= self.supported_rows

    @property
    def permits_materialized_fallback(self) -> bool:
        """Allow oracle fallback only inside its explicitly bounded envelope."""

        return self.physical_rows <= MATERIALIZED_BROWSER_EVALUATION_ROW_LIMIT

    def require_supported(self) -> None:
        """Fail before loading rows when any required stage is over capacity."""

        if self.admitted:
            return
        limiting = tuple(
            item
            for item in self.stages
            if item.supported_rows == self.supported_rows
        )
        stage_names = ", ".join(item.stage for item in limiting)
        reason_codes = sorted(
            {reason for item in limiting for reason in item.reason_codes}
        )
        reason_suffix = (
            f" ({', '.join(reason_codes)})" if reason_codes else ""
        )
        raise ReadinessError(
            f"This project contains {self.physical_rows:,} source rows, but "
            f"the complete preparation route can safely check up to "
            f"{self.supported_rows:,} rows. The limiting stage is "
            f"{stage_names}{reason_suffix}. Split the source into smaller "
            "projects or remove the unsupported setup; no data was changed."
        )

    def to_portable_dict(self) -> dict[str, object]:
        """Return a sanitized route report for benchmark evidence."""

        return {
            "physical_rows": self.physical_rows,
            "supported_rows": self.supported_rows,
            "admitted": self.admitted,
            "permits_materialized_fallback": self.permits_materialized_fallback,
            "stages": [item.to_portable_dict() for item in self.stages],
        }


def compile_preparation_capability(
    *,
    definition: MappingDefinition,
    physical_selection: SourceSelection,
    effective_selection: SourceSelection,
    source_snapshots: Iterable[SourceSnapshot],
    derived_plan: DerivedEntityPlan | None,
    current_ruleset: QualityRuleSet | None,
    reference_bundle: ReferenceBundle | None,
) -> PreparationCapabilityManifest:
    """Compile the current route without opening or materializing source rows."""

    physical_rows = sum(item.row_count for item in physical_selection.datasets)
    direct = supports_bounded_direct_preparation(
        physical_selection,
        effective_selection,
        derived_plan,
    )
    if direct:
        transformation_limit = direct_preparation_row_limit(
            definition,
            effective_selection,
            source_snapshots,
        )
        native = (
            transformation_limit
            == COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT
        )
        transformation = PreparationStageCapability(
            stage="transformation",
            behavior=(
                PreparationRouteBehavior.NATIVE_COLUMNAR
                if native
                else PreparationRouteBehavior.BOUNDED_PYTHON
            ),
            supported_rows=transformation_limit,
            reason_codes=() if native else ("COLUMNAR_MAPPING_UNSUPPORTED",),
        )
        canonical = PreparationStageCapability(
            stage="canonical_adaptation",
            behavior=PreparationRouteBehavior.BOUNDED_DURABLE,
            supported_rows=transformation_limit,
            reason_codes=(
                "PREPARED_SNAPSHOT_VALUE_PROJECTION"
                if native
                else "ROW_JSON_COMPATIBILITY_PATH",
            ),
        )
    else:
        transformation_limit = MATERIALIZED_BROWSER_EVALUATION_ROW_LIMIT
        transformation = PreparationStageCapability(
            stage="transformation",
            behavior=PreparationRouteBehavior.MATERIALIZING,
            supported_rows=transformation_limit,
            reason_codes=("DERIVED_OR_NON_DIRECT_DATASET",),
        )
        canonical = PreparationStageCapability(
            stage="canonical_adaptation",
            behavior=PreparationRouteBehavior.MATERIALIZING,
            supported_rows=transformation_limit,
            reason_codes=("DERIVED_OR_NON_DIRECT_DATASET",),
        )

    quality_reasons = _bounded_quality_reasons(
        definition=definition,
        physical_selection=physical_selection,
        effective_selection=effective_selection,
        current_ruleset=current_ruleset,
        reference_bundle=reference_bundle,
        direct=direct,
    )
    if quality_reasons:
        quality_limit = MATERIALIZED_BROWSER_EVALUATION_ROW_LIMIT
        quality = PreparationStageCapability(
            stage="quality",
            behavior=PreparationRouteBehavior.MATERIALIZING,
            supported_rows=quality_limit,
            reason_codes=quality_reasons,
        )
    else:
        quality_limit = transformation_limit
        quality = PreparationStageCapability(
            stage="quality",
            behavior=PreparationRouteBehavior.BOUNDED_RUNTIME_GUARDED,
            supported_rows=quality_limit,
            reason_codes=("RUNTIME_DATA_SHAPE_GUARD",),
        )

    normalization_limit = min(transformation_limit, quality_limit)
    normalization = PreparationStageCapability(
        stage="normalization",
        behavior=(
            PreparationRouteBehavior.BOUNDED_RUNTIME_GUARDED
            if not quality_reasons
            else PreparationRouteBehavior.MATERIALIZING
        ),
        supported_rows=(
            normalization_limit
            if not quality_reasons
            else MATERIALIZED_BROWSER_EVALUATION_ROW_LIMIT
        ),
        reason_codes=(
            ("RUNTIME_DATA_SHAPE_GUARD",)
            if not quality_reasons
            else quality_reasons
        ),
    )
    relationships = PreparationStageCapability(
        stage="relationships",
        behavior=(
            PreparationRouteBehavior.BOUNDED_DURABLE
            if direct and not quality_reasons
            else PreparationRouteBehavior.MATERIALIZING
        ),
        supported_rows=(
            normalization_limit
            if direct and not quality_reasons
            else MATERIALIZED_BROWSER_EVALUATION_ROW_LIMIT
        ),
        reason_codes=(
            ()
            if direct and not quality_reasons
            else ("SET_BASED_RELATIONSHIP_ROUTE_UNAVAILABLE",)
        ),
    )
    deferred = tuple(
        PreparationStageCapability(
            stage=stage,
            behavior=PreparationRouteBehavior.DEFERRED,
            supported_rows=None,
            reason_codes=("NOT_EXECUTED_DURING_PREPARATION",),
        )
        for stage in ("reporting", "preflight")
    )
    stages = (
        transformation,
        canonical,
        quality,
        normalization,
        relationships,
        *deferred,
    )
    supported_rows = min(
        item.supported_rows
        for item in stages
        if item.constrains_admission and item.supported_rows is not None
    )
    return PreparationCapabilityManifest(
        physical_rows=physical_rows,
        supported_rows=supported_rows,
        stages=stages,
    )


def _bounded_quality_reasons(
    *,
    definition: MappingDefinition,
    physical_selection: SourceSelection,
    effective_selection: SourceSelection,
    current_ruleset: QualityRuleSet | None,
    reference_bundle: ReferenceBundle | None,
    direct: bool,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not direct:
        reasons.append("NON_DIRECT_QUALITY_INPUT")
    if len(physical_selection.datasets) != 1:
        reasons.append("MULTI_DATASET_QUALITY_MATERIALIZES")
    if reference_bundle is not None:
        reasons.append("REFERENCE_RESOLUTION_MATERIALIZES")
    if (
        current_ruleset is not None
        and current_ruleset.mapping_hash == definition.content_hash
        and current_ruleset.schema_hash == definition.schema_hash
        and not _is_mapping_derived_ruleset(
            current_ruleset,
            datasets=(item.name for item in effective_selection.datasets),
        )
    ):
        reasons.append("ADVANCED_QUALITY_RULES_MATERIALIZE")
    return tuple(sorted(set(reasons)))


def _is_mapping_derived_ruleset(
    ruleset: QualityRuleSet,
    *,
    datasets: Iterable[str],
) -> bool:
    expected = {
        (dataset, QualityRuleFamily(family))
        for dataset in datasets
        for family in MANDATORY_QUALITY_FAMILIES
    }
    actual = {(rule.dataset, rule.family) for rule in ruleset.rules}
    return (
        ruleset.reference_bundle_hash is None
        and actual == expected
        and len(ruleset.rules) == len(expected)
        and all(
            rule.source is QualityRuleSource.MAPPING_DERIVED
            for rule in ruleset.rules
        )
    )
