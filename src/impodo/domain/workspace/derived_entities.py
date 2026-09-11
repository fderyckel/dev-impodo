"""Govern Stage B/D preparation plans for related logical datasets.

Layer: domain contracts plus application workspace service. The browser can
author bounded previews for lookup extraction, multi-column hierarchies, and
parent/child dataset splits. Every rule type participates in the effective
mapping selection and is repeated over every source row by readiness staging
without changing the frozen source.

See ``docs/architecture/python-code-map.md``,
``docs/user/guides/related-tables.md``, and
``tests/application/workspace/test_derived_entities.py``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Iterable, Mapping, Protocol
import unicodedata
from uuid import NAMESPACE_URL, UUID, uuid5

from impodo.domain.source_binding import DerivedSourceBinding, require_file_source
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.structural import (
    ExactJoinRule,
    GroupAggregateRule,
    StructuralRule,
    UnionAllRule,
    structural_mapping_selection,
)


DERIVED_ENTITY_CONTRACT_VERSION = 5
_SUPPORTED_DERIVED_ENTITY_CONTRACT_VERSIONS = frozenset({4, 5})
_DATASET_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_EXTERNAL_ID_NAMESPACE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_TECHNICAL_NAME = re.compile(r"^[a-z_][a-z0-9_.]{0,127}$")
_TECHNICAL_FIELD = re.compile(r"^[a-z_][a-z0-9_]{0,127}$")
_SUPPORTED_BLANK_POLICIES = frozenset({"block", "quarantine"})
_SUPPORTED_MISSING_PARENT_MODES = frozenset(
    {"block", "quarantine", "fixed", "promote"}
)
_SUPPORTED_MISSING_LEAF_MODES = frozenset(
    {"use_deepest", "block", "quarantine"}
)
_SUPPORTED_ALL_BLANK_MODES = frozenset(
    {"emit_null_reference", "fixed", "block", "quarantine"}
)


class SourceTableCatalogView(Protocol):
    """Minimum detected-table evidence needed by portable preview logic."""

    table_key: str


class SourceFileCatalogView(Protocol):
    """Minimum immutable catalogue evidence needed by portable preview logic."""

    file_id: str
    source_sha256: str
    tables: tuple[SourceTableCatalogView, ...]

    @property
    def content_hash(self) -> str: ...


@dataclass(frozen=True, slots=True)
class DerivedEntityRule:
    """Extract one related entity dataset from one frozen source column."""

    rule_id: str
    output_dataset_name: str
    source_dataset_id: str
    source_column_key: str
    target_model: str
    target_name_field: str
    external_id_namespace: str
    parent_separator: str | None = None
    blank_policy: str = "block"

    def __post_init__(self) -> None:
        try:
            canonical_rule_id = str(UUID(self.rule_id))
        except (ValueError, AttributeError) as error:
            raise ValueError("Derived-entity rule identifier is invalid") from error
        object.__setattr__(self, "rule_id", canonical_rule_id)

        dataset_name = self.output_dataset_name.strip()
        if not _DATASET_NAME.fullmatch(dataset_name):
            raise ValueError(
                "Derived dataset names must use lowercase letters, digits, "
                "and underscores"
            )
        object.__setattr__(self, "output_dataset_name", dataset_name)

        source_dataset_id = self.source_dataset_id.strip()
        source_column_key = self.source_column_key.strip()
        if not source_dataset_id or len(source_dataset_id) > 200:
            raise ValueError("Source dataset identifier is invalid")
        if not source_column_key or len(source_column_key) > 500:
            raise ValueError("Source column identifier is invalid")
        object.__setattr__(self, "source_dataset_id", source_dataset_id)
        object.__setattr__(self, "source_column_key", source_column_key)

        target_model = self.target_model.strip()
        target_name_field = self.target_name_field.strip()
        if not _TECHNICAL_NAME.fullmatch(target_model):
            raise ValueError("Target model must be a valid Odoo technical name")
        if not _TECHNICAL_FIELD.fullmatch(target_name_field):
            raise ValueError("Target name field must be a valid Odoo field name")
        object.__setattr__(self, "target_model", target_model)
        object.__setattr__(self, "target_name_field", target_name_field)

        namespace = self.external_id_namespace.strip()
        if not _EXTERNAL_ID_NAMESPACE.fullmatch(namespace):
            raise ValueError(
                "External-ID namespace must start with a lowercase letter and "
                "use only lowercase letters, digits, and underscores"
            )
        object.__setattr__(self, "external_id_namespace", namespace)

        separator = self.parent_separator
        if separator is not None:
            separator = separator.strip()
            if not separator:
                separator = None
            elif len(separator) > 10:
                raise ValueError("Parent separator must not exceed 10 characters")
        object.__setattr__(self, "parent_separator", separator)

        blank_policy = self.blank_policy.strip().casefold()
        if blank_policy not in _SUPPORTED_BLANK_POLICIES:
            raise ValueError("Blank policy must be block or quarantine")
        object.__setattr__(self, "blank_policy", blank_policy)


@dataclass(frozen=True, slots=True)
class HierarchyValuePolicy:
    """One explicit decision for a missing hierarchy value."""

    mode: str
    value: str | None = None

    def __post_init__(self) -> None:
        mode = self.mode.strip().casefold()
        object.__setattr__(self, "mode", mode)
        value = self.value
        if value is not None:
            value = " ".join(unicodedata.normalize("NFKC", value).split()) or None
            if value is not None and len(value) > 500:
                raise ValueError(
                    "A fixed hierarchy value must not exceed 500 characters"
                )
        object.__setattr__(self, "value", value)


@dataclass(frozen=True, slots=True)
class HierarchicalLookupRule:
    """Extract one related hierarchy from ordered columns in one dataset."""

    rule_id: str
    output_dataset_name: str
    source_dataset_id: str
    source_level_column_keys: tuple[str, ...]
    target_model: str
    target_name_field: str
    external_id_namespace: str
    missing_parent: HierarchyValuePolicy
    missing_leaf: str = "use_deepest"
    all_blank: HierarchyValuePolicy = HierarchyValuePolicy(
        mode="emit_null_reference"
    )

    def __post_init__(self) -> None:
        try:
            canonical_rule_id = str(UUID(self.rule_id))
        except (ValueError, AttributeError) as error:
            raise ValueError("Hierarchy rule identifier is invalid") from error
        object.__setattr__(self, "rule_id", canonical_rule_id)

        object.__setattr__(
            self,
            "output_dataset_name",
            _validated_dataset_name(self.output_dataset_name, "Derived dataset"),
        )
        source_dataset_id = self.source_dataset_id.strip()
        if not source_dataset_id or len(source_dataset_id) > 200:
            raise ValueError("Source dataset identifier is invalid")
        object.__setattr__(self, "source_dataset_id", source_dataset_id)

        source_keys = tuple(
            _validated_column_key(value, "Hierarchy level")
            for value in self.source_level_column_keys
        )
        if not 2 <= len(source_keys) <= 5:
            raise ValueError("Choose between two and five hierarchy fields")
        if len(set(source_keys)) != len(source_keys):
            raise ValueError("Each hierarchy level must use a different field")
        object.__setattr__(self, "source_level_column_keys", source_keys)

        target_model = self.target_model.strip()
        target_name_field = self.target_name_field.strip()
        if not _TECHNICAL_NAME.fullmatch(target_model):
            raise ValueError("Target model must be a valid Odoo technical name")
        if not _TECHNICAL_FIELD.fullmatch(target_name_field):
            raise ValueError("Target name field must be a valid Odoo field name")
        object.__setattr__(self, "target_model", target_model)
        object.__setattr__(self, "target_name_field", target_name_field)

        namespace = self.external_id_namespace.strip()
        if not _EXTERNAL_ID_NAMESPACE.fullmatch(namespace):
            raise ValueError(
                "External-ID namespace must start with a lowercase letter and "
                "use only lowercase letters, digits, and underscores"
            )
        object.__setattr__(self, "external_id_namespace", namespace)

        if self.missing_parent.mode not in _SUPPORTED_MISSING_PARENT_MODES:
            raise ValueError("Missing-parent behavior is unsupported")
        _require_fixed_policy_value(self.missing_parent, "Missing-parent")
        missing_leaf = self.missing_leaf.strip().casefold()
        if missing_leaf not in _SUPPORTED_MISSING_LEAF_MODES:
            raise ValueError("Missing-leaf behavior is unsupported")
        object.__setattr__(self, "missing_leaf", missing_leaf)
        if self.all_blank.mode not in _SUPPORTED_ALL_BLANK_MODES:
            raise ValueError("All-blank behavior is unsupported")
        _require_fixed_policy_value(self.all_blank, "All-blank")


@dataclass(frozen=True, slots=True)
class RelatedDatasetRule:
    """Project one repeated parent and its child rows from a frozen dataset."""

    rule_id: str
    source_dataset_id: str
    parent_dataset_name: str
    child_dataset_name: str
    parent_key_column_key: str
    child_key_column_key: str
    scope_column_key: str | None = None
    blank_policy: str = "block"

    def __post_init__(self) -> None:
        try:
            canonical_rule_id = str(UUID(self.rule_id))
        except (ValueError, AttributeError) as error:
            raise ValueError("Related-dataset rule identifier is invalid") from error
        object.__setattr__(self, "rule_id", canonical_rule_id)

        source_dataset_id = self.source_dataset_id.strip()
        if not source_dataset_id or len(source_dataset_id) > 200:
            raise ValueError("Source dataset identifier is invalid")
        object.__setattr__(self, "source_dataset_id", source_dataset_id)

        parent_name = _validated_dataset_name(
            self.parent_dataset_name,
            "Parent dataset",
        )
        child_name = _validated_dataset_name(
            self.child_dataset_name,
            "Child dataset",
        )
        if parent_name == child_name:
            raise ValueError("Parent and child dataset names must be different")
        object.__setattr__(self, "parent_dataset_name", parent_name)
        object.__setattr__(self, "child_dataset_name", child_name)

        parent_key = _validated_column_key(
            self.parent_key_column_key,
            "Parent key",
        )
        child_key = _validated_column_key(
            self.child_key_column_key,
            "Line key",
        )
        if parent_key == child_key:
            raise ValueError("Parent key and line key must use different fields")
        object.__setattr__(self, "parent_key_column_key", parent_key)
        object.__setattr__(self, "child_key_column_key", child_key)

        scope = self.scope_column_key
        if scope is not None:
            scope = scope.strip() or None
            if scope is not None:
                scope = _validated_column_key(scope, "Scope")
                if scope in {parent_key, child_key}:
                    raise ValueError(
                        "Scope must use a different field from the parent and line keys"
                    )
        object.__setattr__(self, "scope_column_key", scope)

        blank_policy = self.blank_policy.strip().casefold()
        if blank_policy not in _SUPPORTED_BLANK_POLICIES:
            raise ValueError("Blank policy must be block or quarantine")
        object.__setattr__(self, "blank_policy", blank_policy)


LookupRule = DerivedEntityRule | HierarchicalLookupRule
SourcePreparationRule = LookupRule | RelatedDatasetRule | StructuralRule


@dataclass(frozen=True, slots=True)
class DerivedEntityPlan:
    """Immutable revision of all derived-entity authoring rules in a workspace."""

    plan_id: str
    version: int
    workspace_id: str
    source_selection_hash: str
    rules: tuple[SourcePreparationRule, ...]
    updated_at: datetime
    updated_by: str
    contract_version: int = DERIVED_ENTITY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version not in _SUPPORTED_DERIVED_ENTITY_CONTRACT_VERSIONS:
            raise ValueError("Source-preparation plan contract version is unsupported")

    @property
    def content_hash(self) -> str:
        """Return the semantic identity of the complete ordered rule revision."""

        return _content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        """Return stable portable rule and provenance evidence."""

        payload: dict[str, object] = {
            "plan_id": self.plan_id,
            "version": self.version,
            "workspace_id": self.workspace_id,
            "source_selection_hash": self.source_selection_hash,
            "rules": [
                _rule_payload(item)
                for item in sorted(self.rules, key=lambda rule: rule.rule_id)
            ],
            "updated_at": self.updated_at.isoformat(),
            "updated_by": self.updated_by,
            "contract_version": self.contract_version,
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def to_json(self) -> str:
        """Serialize the immutable plan revision with its content hash."""

        return _canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "DerivedEntityPlan":
        """Restore a supported plan revision and reject hash tampering."""

        payload = json.loads(value)
        content_hash = payload.get("content_hash")
        unhashed = dict(payload)
        unhashed.pop("content_hash", None)
        if content_hash != _content_hash(unhashed):
            raise ValueError("Derived-entity plan content hash is invalid")
        contract_version = int(payload["contract_version"])
        result = cls(
            plan_id=str(payload["plan_id"]),
            version=int(payload["version"]),
            workspace_id=str(payload["workspace_id"]),
            source_selection_hash=str(payload["source_selection_hash"]),
            rules=tuple(_rule_from_payload(item) for item in payload.get("rules", ())),
            updated_at=datetime.fromisoformat(str(payload["updated_at"])),
            updated_by=str(payload["updated_by"]),
            contract_version=contract_version,
        )
        return result


@dataclass(frozen=True, slots=True)
class DerivedEntityCandidate:
    """One related hierarchy record in a bounded preview."""

    entity_id: str
    odoo_external_id: str
    canonical_key: str
    name: str
    parent_entity_id: str | None
    aliases: tuple[str, ...]
    sampled_source_row_count: int
    requires_alias_review: bool


@dataclass(frozen=True, slots=True)
class DerivedEntityPreview:
    """Bounded evidence; never a claim that full-row extraction is complete."""

    source_dataset_name: str
    source_column_name: str
    sampled_source_rows: int
    full_distinct_count: int
    full_distinct_count_is_exact: bool
    blank_sample_rows: int
    invalid_path_sample_rows: int
    candidates: tuple[DerivedEntityCandidate, ...]
    fixed_parent_sample_rows: int = 0
    promoted_root_sample_rows: int = 0
    deepest_level_sample_rows: int = 0
    blank_reference_sample_rows: int = 0
    quarantined_sample_rows: int = 0
    blocked_sample_rows: int = 0


@dataclass(frozen=True, slots=True)
class RelatedParentSample:
    """One parent group shown as bounded, human-readable evidence."""

    parent_key: str
    scope: str | None
    sampled_child_rows: int
    sampled_child_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RelatedDatasetPreview:
    """Bounded evidence for one proposed parent/child split."""

    source_dataset_name: str
    source_rows: int
    parent_candidate_count: int
    parent_candidate_count_is_exact: bool
    child_rows: int
    sampled_source_rows: int
    sampled_parent_groups: int
    blank_parent_sample_rows: int
    blank_scope_sample_rows: int
    blank_child_key_sample_rows: int
    duplicate_child_key_sample_rows: int
    normalized_key_sample_rows: int
    parent_samples: tuple[RelatedParentSample, ...]


@dataclass(frozen=True, slots=True)
class RelatedDatasetLink:
    """Mapping guidance from a generated child dataset to its parent."""

    parent_dataset_id: str
    child_dataset_id: str
    reference_column_keys: tuple[str, ...]
    child_identity_column_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DerivedDatasetLink:
    """Mapping guidance between one extracted dataset and its source rows."""

    derived_dataset_id: str
    consumer_dataset_id: str
    source_column_key: str
    canonical_key_column_key: str
    name_column_key: str
    parent_key_column_key: str | None
    target_model: str
    target_name_field: str
    source_level_column_keys: tuple[str, ...] = ()

    @property
    def hierarchical(self) -> bool:
        """Return whether the consumer key is generated from ordered columns."""

        return bool(self.source_level_column_keys)


@dataclass(frozen=True, slots=True)
class HierarchyPathEvaluation:
    """One canonical path decision shared by preview and full preparation."""

    display_parts: tuple[str, ...]
    canonical_parts: tuple[str, ...]
    outcomes: tuple[str, ...]

    @property
    def has_path(self) -> bool:
        return bool(self.canonical_parts)


def preview_derived_entities(
    rule: LookupRule,
    selection: SourceSelection,
    catalogs: Iterable[SourceFileCatalogView],
) -> DerivedEntityPreview:
    """Build a deterministic preview from the bounded inspection evidence."""

    if isinstance(rule, HierarchicalLookupRule):
        return _preview_hierarchical_entities(rule, selection, catalogs)

    dataset = _source_dataset(
        selection,
        rule.source_dataset_id,
        rule.source_column_key,
    )
    column = next(
        item for item in dataset.columns if item.stable_key == rule.source_column_key
    )
    binding = require_file_source(dataset.source)
    catalog = next(
        (
            item
            for item in catalogs
            if item.file_id == binding.file_id
            and f"sha256:{item.source_sha256.removeprefix('sha256:')}"
            == binding.source_sha256
            and item.content_hash == binding.catalog_hash
        ),
        None,
    )
    table = next(
        (
            item
            for item in (catalog.tables if catalog else ())
            if item.table_key == binding.table_key
        ),
        None,
    )
    if table is None:
        raise WorkspaceError("The frozen dataset no longer matches its source catalog")
    profile = next(item for item in table.columns if item.ordinal == column.ordinal)

    accumulated: dict[tuple[str, ...], dict[str, object]] = {}
    blank_rows = 0
    invalid_rows = 0
    for row in table.preview_rows:
        raw = row[column.ordinal - 1] if column.ordinal <= len(row) else None
        path = _normalized_path(raw, rule.parent_separator)
        if path is None:
            blank_rows += 1
            continue
        display_parts, key_parts = path
        if not display_parts:
            invalid_rows += 1
            continue
        for depth in range(1, len(key_parts) + 1):
            key_path = key_parts[:depth]
            display_path = display_parts[:depth]
            entry = accumulated.setdefault(
                key_path,
                {
                    "name": display_path[-1],
                    "aliases": set(),
                    "count": 0,
                },
            )
            aliases = entry["aliases"]
            assert isinstance(aliases, set)
            aliases.add(_display_path(display_path, rule.parent_separator))
            entry["count"] = int(entry["count"]) + 1

    candidates: list[DerivedEntityCandidate] = []
    for key_path, entry in accumulated.items():
        parent_path = key_path[:-1]
        aliases = tuple(sorted(str(item) for item in entry["aliases"]))
        entity_id, external_id = _identifiers(rule, key_path)
        parent_entity_id = _identifiers(rule, parent_path)[0] if parent_path else None
        candidates.append(
            DerivedEntityCandidate(
                entity_id=entity_id,
                odoo_external_id=external_id,
                canonical_key=" / ".join(key_path),
                name=str(entry["name"]),
                parent_entity_id=parent_entity_id,
                aliases=aliases,
                sampled_source_row_count=int(entry["count"]),
                requires_alias_review=len(aliases) > 1,
            )
        )

    return DerivedEntityPreview(
        source_dataset_name=dataset.name,
        source_column_name=column.source_name,
        sampled_source_rows=len(table.preview_rows),
        full_distinct_count=profile.distinct_count,
        full_distinct_count_is_exact=profile.distinct_count_is_exact,
        blank_sample_rows=blank_rows,
        invalid_path_sample_rows=invalid_rows,
        candidates=tuple(
            sorted(
                candidates,
                key=lambda item: (item.canonical_key.count(" / "), item.canonical_key),
            )
        ),
    )


def _preview_hierarchical_entities(
    rule: HierarchicalLookupRule,
    selection: SourceSelection,
    catalogs: Iterable[SourceFileCatalogView],
) -> DerivedEntityPreview:
    """Build bounded evidence for one hierarchy assembled from source fields."""

    dataset = _hierarchy_source_dataset(selection, rule)
    table = _source_table(dataset, catalogs)
    columns = {
        item.stable_key: item
        for item in dataset.columns
        if item.stable_key in rule.source_level_column_keys
    }
    ordered_columns = tuple(columns[key] for key in rule.source_level_column_keys)
    accumulated: dict[tuple[str, ...], dict[str, object]] = {}
    outcome_counts: dict[str, int] = {}
    for row in table.preview_rows:
        evaluation = evaluate_hierarchy_path(
            rule,
            {
                column.stable_key: _row_value(row, column.ordinal)
                for column in ordered_columns
            },
        )
        for outcome in evaluation.outcomes:
            outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
        if not evaluation.has_path:
            continue
        for depth in range(1, len(evaluation.canonical_parts) + 1):
            key_path = evaluation.canonical_parts[:depth]
            display_path = evaluation.display_parts[:depth]
            entry = accumulated.setdefault(
                key_path,
                {
                    "name": display_path[-1],
                    "aliases": set(),
                    "count": 0,
                },
            )
            aliases = entry["aliases"]
            assert isinstance(aliases, set)
            aliases.add(_display_path(display_path, "/"))
            entry["count"] = int(entry["count"]) + 1

    candidates: list[DerivedEntityCandidate] = []
    for key_path, entry in accumulated.items():
        aliases = tuple(sorted(str(item) for item in entry["aliases"]))
        entity_id, external_id = _identifiers(rule, key_path)
        parent_path = key_path[:-1]
        candidates.append(
            DerivedEntityCandidate(
                entity_id=entity_id,
                odoo_external_id=external_id,
                canonical_key=" / ".join(key_path),
                name=str(entry["name"]),
                parent_entity_id=(
                    _identifiers(rule, parent_path)[0] if parent_path else None
                ),
                aliases=aliases,
                sampled_source_row_count=int(entry["count"]),
                requires_alias_review=len(aliases) > 1,
            )
        )

    exact = len(table.preview_rows) == table.row_count
    return DerivedEntityPreview(
        source_dataset_name=dataset.name,
        source_column_name=" → ".join(
            item.source_name for item in ordered_columns
        ),
        sampled_source_rows=len(table.preview_rows),
        full_distinct_count=len(candidates),
        full_distinct_count_is_exact=exact,
        blank_sample_rows=(
            outcome_counts.get("emit_null_reference", 0)
            + outcome_counts.get("block_all_blank", 0)
            + outcome_counts.get("quarantine_all_blank", 0)
        ),
        invalid_path_sample_rows=(
            outcome_counts.get("block_missing_parent", 0)
            + outcome_counts.get("quarantine_missing_parent", 0)
            + outcome_counts.get("block_missing_leaf", 0)
            + outcome_counts.get("quarantine_missing_leaf", 0)
        ),
        candidates=tuple(
            sorted(
                candidates,
                key=lambda item: (item.canonical_key.count(" / "), item.canonical_key),
            )
        ),
        fixed_parent_sample_rows=outcome_counts.get("fixed_parent", 0),
        promoted_root_sample_rows=outcome_counts.get("promote", 0),
        deepest_level_sample_rows=outcome_counts.get("use_deepest", 0),
        blank_reference_sample_rows=outcome_counts.get("emit_null_reference", 0),
        quarantined_sample_rows=sum(
            count
            for outcome, count in outcome_counts.items()
            if outcome.startswith("quarantine_")
        ),
        blocked_sample_rows=sum(
            count
            for outcome, count in outcome_counts.items()
            if outcome.startswith("block_")
        ),
    )


def evaluate_hierarchy_path(
    rule: HierarchicalLookupRule,
    values: dict[str, object] | Mapping[str, object],
) -> HierarchyPathEvaluation:
    """Apply one reviewed missing-value policy and return a canonical path."""

    normalized = tuple(
        _normalized_display_value(values.get(key))
        for key in rule.source_level_column_keys
    )
    if not any(normalized):
        if rule.all_blank.mode == "fixed":
            assert rule.all_blank.value is not None
            display = (rule.all_blank.value,)
            return HierarchyPathEvaluation(
                display_parts=display,
                canonical_parts=tuple(item.casefold() for item in display),
                outcomes=("fixed_all_blank",),
            )
        return HierarchyPathEvaluation(
            display_parts=(),
            canonical_parts=(),
            outcomes=(
                {
                    "emit_null_reference": "emit_null_reference",
                    "block": "block_all_blank",
                    "quarantine": "quarantine_all_blank",
                }[rule.all_blank.mode],
            ),
        )

    last_populated = max(
        index for index, value in enumerate(normalized) if value is not None
    )
    working = list(normalized[: last_populated + 1])
    outcomes: list[str] = []
    if last_populated < len(normalized) - 1:
        if rule.missing_leaf in {"block", "quarantine"}:
            return HierarchyPathEvaluation(
                display_parts=(),
                canonical_parts=(),
                outcomes=(f"{rule.missing_leaf}_missing_leaf",),
            )
        outcomes.append("use_deepest")

    if any(value is None for value in working):
        if rule.missing_parent.mode in {"block", "quarantine"}:
            return HierarchyPathEvaluation(
                display_parts=(),
                canonical_parts=(),
                outcomes=(f"{rule.missing_parent.mode}_missing_parent",),
            )
        if rule.missing_parent.mode == "fixed":
            assert rule.missing_parent.value is not None
            working = [
                rule.missing_parent.value if value is None else value
                for value in working
            ]
            outcomes.append("fixed_parent")
        else:
            working = [value for value in working if value is not None]
            outcomes.append("promote")

    display_parts = tuple(str(value) for value in working if value is not None)
    return HierarchyPathEvaluation(
        display_parts=display_parts,
        canonical_parts=tuple(item.casefold() for item in display_parts),
        outcomes=tuple(outcomes) or ("source",),
    )


def preview_related_datasets(
    rule: RelatedDatasetRule,
    selection: SourceSelection,
    catalogs: Iterable[SourceFileCatalogView],
) -> RelatedDatasetPreview:
    """Preview grouping and line identity from bounded inspection evidence."""

    dataset = _related_source_dataset(selection, rule)
    table = _source_table(dataset, catalogs)
    columns = {item.stable_key: item for item in dataset.columns}
    profiles = {item.ordinal: item for item in table.columns}
    parent_column = columns[rule.parent_key_column_key]
    child_column = columns[rule.child_key_column_key]
    scope_column = columns[rule.scope_column_key] if rule.scope_column_key else None

    grouped: dict[tuple[str | None, str], list[str]] = {}
    blank_parent = 0
    blank_scope = 0
    blank_child = 0
    duplicate_children = 0
    normalized_rows = 0
    seen_children: set[tuple[str | None, str, str]] = set()
    for row in table.preview_rows:
        parent_raw = _row_value(row, parent_column.ordinal)
        child_raw = _row_value(row, child_column.ordinal)
        scope_raw = _row_value(row, scope_column.ordinal) if scope_column else None
        parent, parent_changed = _normalized_key(parent_raw)
        child, child_changed = _normalized_key(child_raw)
        scope, scope_changed = (
            _normalized_key(scope_raw) if scope_column else (None, False)
        )
        if parent is None:
            blank_parent += 1
        if scope_column and scope is None:
            blank_scope += 1
        if child is None:
            blank_child += 1
        if parent_changed or child_changed or scope_changed:
            normalized_rows += 1
        if parent is None or child is None or (scope_column and scope is None):
            continue
        identity = (scope, parent, child)
        if identity in seen_children:
            duplicate_children += 1
        else:
            seen_children.add(identity)
        grouped.setdefault((scope, parent), []).append(child)

    parent_profile = profiles[parent_column.ordinal]
    scope_profile = profiles[scope_column.ordinal] if scope_column else None
    parent_count_exact = bool(parent_profile.distinct_count_is_exact)
    if scope_profile is not None:
        parent_count_exact = bool(
            parent_count_exact
            and scope_profile.distinct_count_is_exact
            and scope_profile.distinct_count <= 1
        )
    parent_count = max(parent_profile.distinct_count, len(grouped))
    samples = tuple(
        RelatedParentSample(
            parent_key=parent,
            scope=scope,
            sampled_child_rows=len(children),
            sampled_child_keys=tuple(children[:5]),
        )
        for (scope, parent), children in list(grouped.items())[:5]
    )
    return RelatedDatasetPreview(
        source_dataset_name=dataset.name,
        source_rows=table.row_count,
        parent_candidate_count=parent_count,
        parent_candidate_count_is_exact=parent_count_exact,
        child_rows=table.row_count,
        sampled_source_rows=len(table.preview_rows),
        sampled_parent_groups=len(grouped),
        blank_parent_sample_rows=blank_parent,
        blank_scope_sample_rows=blank_scope,
        blank_child_key_sample_rows=blank_child,
        duplicate_child_key_sample_rows=duplicate_children,
        normalized_key_sample_rows=normalized_rows,
        parent_samples=samples,
    )


def mapping_source_selection(
    selection: SourceSelection,
    plan: DerivedEntityPlan | None,
    catalogs: Iterable[SourceFileCatalogView] = (),
) -> SourceSelection:
    """Expose every prepared logical dataset to the mapping workflow."""

    structural_rules = tuple(
        item
        for item in (plan.rules if plan else ())
        if isinstance(item, (ExactJoinRule, UnionAllRule, GroupAggregateRule))
    )
    prepared_selection = structural_mapping_selection(selection, structural_rules)
    split_rules = {
        item.source_dataset_id: item
        for item in (plan.rules if plan else ())
        if isinstance(item, RelatedDatasetRule)
    }
    lookup_rules: dict[str, list[LookupRule]] = {}
    for item in plan.rules if plan else ():
        if isinstance(item, (DerivedEntityRule, HierarchicalLookupRule)):
            lookup_rules.setdefault(item.source_dataset_id, []).append(item)
    if not split_rules and not lookup_rules:
        return prepared_selection

    catalog_set = tuple(catalogs)
    effective: list[SourceDataset] = []
    # WorkspaceSourceProjection canonicalizes Data-version datasets by identity.
    # The workspace-only preparation worker must derive the same effective
    # selection from its local frozen copy, regardless of the authored display
    # order preserved in that copy.
    for dataset in sorted(
        prepared_selection.datasets,
        key=lambda item: item.dataset_id,
    ):
        for lookup_rule in sorted(
            lookup_rules.get(dataset.dataset_id, ()),
            key=lambda item: item.output_dataset_name,
        ):
            preview = preview_derived_entities(
                lookup_rule,
                selection,
                catalog_set,
            )
            link = _derived_dataset_link(lookup_rule, plan)
            columns = [
                SourceDatasetColumn(
                    ordinal=1,
                    source_name=f"{preview.source_column_name} matching key",
                    stable_key=link.canonical_key_column_key,
                    candidate_type="string",
                ),
                SourceDatasetColumn(
                    ordinal=2,
                    source_name=preview.source_column_name,
                    stable_key=link.name_column_key,
                    candidate_type="string",
                ),
            ]
            if link.parent_key_column_key is not None:
                columns.append(
                    SourceDatasetColumn(
                        ordinal=3,
                        source_name=f"Parent {preview.source_column_name} key",
                        stable_key=link.parent_key_column_key,
                        candidate_type="string",
                    )
                )
            effective.append(
                replace(
                    dataset,
                    dataset_id=link.derived_dataset_id,
                    name=lookup_rule.output_dataset_name,
                    source=DerivedSourceBinding(
                        rule_hash=_content_hash(_rule_payload(lookup_rule)),
                        input_dataset_ids=(dataset.dataset_id,),
                        data_hash=dataset.source_evidence_hash,
                    ),
                    row_count=max(
                        preview.full_distinct_count,
                        len(preview.candidates),
                    ),
                    columns=tuple(columns),
                )
            )

        rule = split_rules.get(dataset.dataset_id)
        if rule is None:
            effective.append(dataset)
            continue
        parent_rows = preview_related_datasets(
            rule,
            selection,
            catalog_set,
        ).parent_candidate_count
        parent_columns = tuple(
            item
            for key in (
                rule.parent_key_column_key,
                rule.scope_column_key,
            )
            if key is not None
            for item in dataset.columns
            if item.stable_key == key
        )
        parent_id, child_id = _related_dataset_ids(rule)
        effective.extend(
            (
                replace(
                    dataset,
                    dataset_id=parent_id,
                    name=rule.parent_dataset_name,
                    source=DerivedSourceBinding(
                        rule_hash=_content_hash(_rule_payload(rule)),
                        input_dataset_ids=(dataset.dataset_id,),
                        data_hash=dataset.source_evidence_hash,
                    ),
                    row_count=parent_rows,
                    columns=parent_columns,
                ),
                replace(
                    dataset,
                    dataset_id=child_id,
                    name=rule.child_dataset_name,
                    source=DerivedSourceBinding(
                        rule_hash=_content_hash(_rule_payload(rule)),
                        input_dataset_ids=(dataset.dataset_id,),
                        data_hash=dataset.source_evidence_hash,
                    ),
                ),
            )
        )

    hierarchy_links = tuple(
        _derived_dataset_link(rule, plan)
        for rule in (plan.rules if plan else ())
        if isinstance(rule, HierarchicalLookupRule)
    )
    hierarchy_names = {
        _derived_dataset_link(rule, plan).source_column_key: rule.output_dataset_name
        for rule in (plan.rules if plan else ())
        if isinstance(rule, HierarchicalLookupRule)
    }
    if hierarchy_links:
        augmented: list[SourceDataset] = []
        for dataset in effective:
            links = tuple(
                link
                for link in hierarchy_links
                if link.consumer_dataset_id == dataset.dataset_id
            )
            if not links:
                augmented.append(dataset)
                continue
            columns = list(dataset.columns)
            known_keys = {item.stable_key for item in columns}
            for link in links:
                if link.source_column_key in known_keys:
                    continue
                columns.append(
                    SourceDatasetColumn(
                        ordinal=len(columns) + 1,
                        source_name=(
                            f"Selected {hierarchy_names[link.source_column_key]} path"
                        ),
                        stable_key=link.source_column_key,
                        candidate_type="string",
                    )
                )
                known_keys.add(link.source_column_key)
            augmented.append(replace(dataset, columns=tuple(columns)))
        effective = augmented

    effective_hash = _content_hash(
        {
            "source_selection_hash": prepared_selection.content_hash,
            "source_preparation_hash": plan.content_hash if plan else None,
            "datasets": [
                {
                    "dataset_id": item.dataset_id,
                    "name": item.name,
                    "row_count": item.row_count,
                    "columns": [column.stable_key for column in item.columns],
                }
                for item in effective
            ],
        }
    )
    return replace(
        selection,
        datasets=tuple(effective),
        content_hash=effective_hash,
    )


def related_dataset_links(
    plan: DerivedEntityPlan | None,
) -> tuple[RelatedDatasetLink, ...]:
    """Return safe UI suggestions for inverse many2one mapping."""

    result: list[RelatedDatasetLink] = []
    for rule in plan.rules if plan else ():
        if not isinstance(rule, RelatedDatasetRule):
            continue
        parent_id, child_id = _related_dataset_ids(rule)
        result.append(
            RelatedDatasetLink(
                parent_dataset_id=parent_id,
                child_dataset_id=child_id,
                reference_column_keys=tuple(
                    item
                    for item in (
                        rule.parent_key_column_key,
                        rule.scope_column_key,
                    )
                    if item is not None
                ),
                child_identity_column_keys=tuple(
                    item
                    for item in (
                        rule.parent_key_column_key,
                        rule.scope_column_key,
                        rule.child_key_column_key,
                    )
                    if item is not None
                ),
            )
        )
    return tuple(result)


def derived_dataset_links(
    plan: DerivedEntityPlan | None,
) -> tuple[DerivedDatasetLink, ...]:
    """Return mapping-ready links for extracted related-record datasets."""

    return tuple(
        _derived_dataset_link(rule, plan)
        for rule in (plan.rules if plan else ())
        if isinstance(rule, (DerivedEntityRule, HierarchicalLookupRule))
    )


def derived_mapping_samples(
    link: DerivedDatasetLink,
    preview: DerivedEntityPreview,
) -> dict[str, tuple[str | None, ...]]:
    """Present bounded generated-row evidence in the normal mapping UI."""

    canonical_by_entity = {
        item.entity_id: item.canonical_key for item in preview.candidates
    }
    candidates = preview.candidates[:3]
    result: dict[str, tuple[str | None, ...]] = {
        link.canonical_key_column_key: tuple(item.canonical_key for item in candidates),
        link.name_column_key: tuple(item.name for item in candidates),
    }
    if link.parent_key_column_key is not None:
        result[link.parent_key_column_key] = tuple(
            canonical_by_entity.get(item.parent_entity_id)
            if item.parent_entity_id is not None
            else None
            for item in candidates
        )
    return result


def _source_dataset(
    selection: SourceSelection,
    dataset_id: str,
    column_key: str,
) -> SourceDataset:
    dataset = next(
        (item for item in selection.datasets if item.dataset_id == dataset_id),
        None,
    )
    if dataset is None:
        raise WorkspaceError("Choose a dataset from the frozen source selection")
    if column_key not in {item.stable_key for item in dataset.columns}:
        raise WorkspaceError("Choose a column from the selected frozen dataset")
    return dataset


def _hierarchy_source_dataset(
    selection: SourceSelection,
    rule: HierarchicalLookupRule,
) -> SourceDataset:
    dataset = next(
        (
            item
            for item in selection.datasets
            if item.dataset_id == rule.source_dataset_id
        ),
        None,
    )
    if dataset is None:
        raise WorkspaceError("Choose a dataset from the frozen source selection")
    available = {item.stable_key for item in dataset.columns}
    if not set(rule.source_level_column_keys).issubset(available):
        raise WorkspaceError("Choose hierarchy fields from the selected frozen dataset")
    return dataset


def _related_source_dataset(
    selection: SourceSelection,
    rule: RelatedDatasetRule,
) -> SourceDataset:
    dataset = next(
        (
            item
            for item in selection.datasets
            if item.dataset_id == rule.source_dataset_id
        ),
        None,
    )
    if dataset is None:
        raise WorkspaceError("Choose a dataset from the frozen source selection")
    available = {item.stable_key for item in dataset.columns}
    required = {
        rule.parent_key_column_key,
        rule.child_key_column_key,
        *((rule.scope_column_key,) if rule.scope_column_key is not None else ()),
    }
    if not required.issubset(available):
        raise WorkspaceError("Choose key fields from the selected frozen dataset")
    return dataset


def _source_table(
    dataset: SourceDataset,
    catalogs: Iterable[SourceFileCatalogView],
):
    binding = require_file_source(dataset.source)
    catalog = next(
        (
            item
            for item in catalogs
            if item.file_id == binding.file_id
            and f"sha256:{item.source_sha256.removeprefix('sha256:')}"
            == binding.source_sha256
            and item.content_hash == binding.catalog_hash
        ),
        None,
    )
    table = next(
        (
            item
            for item in (catalog.tables if catalog else ())
            if item.table_key == binding.table_key
        ),
        None,
    )
    if table is None:
        raise WorkspaceError("The frozen dataset no longer matches its source catalog")
    return table


def _normalized_path(
    raw: object,
    separator: str | None,
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    if raw is None:
        return None
    text = unicodedata.normalize("NFKC", str(raw))
    text = " ".join(text.split())
    if not text:
        return None
    raw_parts = text.split(separator) if separator else [text]
    display_parts = tuple(" ".join(item.split()) for item in raw_parts)
    if any(not item for item in display_parts):
        return (), ()
    return display_parts, tuple(item.casefold() for item in display_parts)


def _display_path(parts: tuple[str, ...], separator: str | None) -> str:
    return (f" {separator} " if separator else "").join(parts)


def _identifiers(
    rule: LookupRule,
    canonical_path: tuple[str, ...],
) -> tuple[str, str]:
    identity_payload = _canonical_json(
        {
            "namespace": rule.external_id_namespace,
            "model": rule.target_model,
            "canonical_path": canonical_path,
        }
    )
    identity = uuid5(NAMESPACE_URL, f"urn:impodo:derived:{identity_payload}")
    model_token = re.sub(r"[^a-z0-9_]+", "_", rule.target_model.casefold()).strip("_")
    return (
        f"entity:{identity}",
        f"impodo_{rule.external_id_namespace}.{model_token}_{identity.hex}",
    )


def _derived_dataset_link(
    rule: LookupRule,
    plan: DerivedEntityPlan | None,
) -> DerivedDatasetLink:
    identity = _derived_dataset_identity(rule)
    split = next(
        (
            item
            for item in (plan.rules if plan else ())
            if isinstance(item, RelatedDatasetRule)
            and item.source_dataset_id == rule.source_dataset_id
        ),
        None,
    )
    consumer_dataset_id = (
        _related_dataset_ids(split)[1] if split else rule.source_dataset_id
    )
    prefix = f"derived:{identity}"
    hierarchical = isinstance(rule, HierarchicalLookupRule)
    return DerivedDatasetLink(
        derived_dataset_id=prefix,
        consumer_dataset_id=consumer_dataset_id,
        source_column_key=(
            f"{prefix}:selected_path"
            if hierarchical
            else rule.source_column_key
        ),
        canonical_key_column_key=f"{prefix}:canonical_key",
        name_column_key=f"{prefix}:name",
        parent_key_column_key=(
            f"{prefix}:parent_key"
            if hierarchical or rule.parent_separator
            else None
        ),
        target_model=rule.target_model,
        target_name_field=rule.target_name_field,
        source_level_column_keys=(
            rule.source_level_column_keys if hierarchical else ()
        ),
    )


def _derived_dataset_identity(rule: LookupRule) -> UUID:
    source_identity = (
        {"source_level_column_keys": rule.source_level_column_keys}
        if isinstance(rule, HierarchicalLookupRule)
        else {
            "source_column_key": rule.source_column_key,
            "parent_separator": rule.parent_separator,
        }
    )
    payload = _canonical_json(
        {
            "output_dataset_name": rule.output_dataset_name,
            "source_dataset_id": rule.source_dataset_id,
            "target_model": rule.target_model,
            "target_name_field": rule.target_name_field,
            "external_id_namespace": rule.external_id_namespace,
            **source_identity,
        }
    )
    return uuid5(NAMESPACE_URL, f"urn:impodo:derived-dataset:{payload}")


def _related_dataset_ids(rule: RelatedDatasetRule) -> tuple[str, str]:
    payload = _canonical_json(
        {
            "source_dataset_id": rule.source_dataset_id,
            "parent_dataset_name": rule.parent_dataset_name,
            "child_dataset_name": rule.child_dataset_name,
            "parent_key_column_key": rule.parent_key_column_key,
            "scope_column_key": rule.scope_column_key,
            "child_key_column_key": rule.child_key_column_key,
        }
    )
    identity = uuid5(NAMESPACE_URL, f"urn:impodo:related-datasets:{payload}")
    return (
        f"related:{identity}:parent",
        f"related:{identity}:child",
    )


def _rule_dataset_names(rules: Iterable[SourcePreparationRule]) -> set[str]:
    names: set[str] = set()
    for rule in rules:
        if isinstance(rule, (DerivedEntityRule, HierarchicalLookupRule)):
            names.add(rule.output_dataset_name)
        elif isinstance(rule, RelatedDatasetRule):
            names.update((rule.parent_dataset_name, rule.child_dataset_name))
        else:
            names.add(rule.output_dataset_name)
    return names


def _rule_payload(
    rule: SourcePreparationRule,
) -> dict[str, object]:
    payload = (
        rule.to_dict()
        if isinstance(rule, (ExactJoinRule, UnionAllRule, GroupAggregateRule))
        else asdict(rule)
    )
    if isinstance(rule, DerivedEntityRule):
        kind = "lookup"
    elif isinstance(rule, HierarchicalLookupRule):
        kind = "hierarchical_lookup"
    elif isinstance(rule, RelatedDatasetRule):
        kind = "parent_child"
    elif isinstance(rule, ExactJoinRule):
        kind = "exact_join"
    elif isinstance(rule, UnionAllRule):
        kind = "union_all"
    else:
        kind = "group_aggregate"
    return {
        "kind": kind,
        **payload,
    }


def _rule_from_payload(payload: dict[str, object]) -> SourcePreparationRule:
    values = dict(payload)
    kind = str(values.pop("kind"))
    if kind == "lookup":
        return DerivedEntityRule(**values)
    if kind == "hierarchical_lookup":
        values["source_level_column_keys"] = tuple(
            str(item) for item in values.get("source_level_column_keys", ())
        )
        values["missing_parent"] = HierarchyValuePolicy(
            **dict(values.get("missing_parent", {}))
        )
        values["all_blank"] = HierarchyValuePolicy(
            **dict(values.get("all_blank", {}))
        )
        return HierarchicalLookupRule(**values)
    if kind == "parent_child":
        return RelatedDatasetRule(**values)
    if kind == "exact_join":
        return ExactJoinRule.from_dict(values)
    if kind == "union_all":
        return UnionAllRule.from_dict(values)
    if kind == "group_aggregate":
        return GroupAggregateRule.from_dict(values)
    raise ValueError("Source-preparation rule kind is unsupported")


def _validated_dataset_name(value: str, label: str) -> str:
    canonical = value.strip()
    if not _DATASET_NAME.fullmatch(canonical):
        raise ValueError(
            f"{label} names must use lowercase letters, digits, and underscores"
        )
    return canonical


def _validated_column_key(value: str, label: str) -> str:
    canonical = value.strip()
    if not canonical or len(canonical) > 500:
        raise ValueError(f"{label} field is invalid")
    return canonical


def _row_value(row: tuple[object, ...], ordinal: int) -> object:
    return row[ordinal - 1] if 0 < ordinal <= len(row) else None


def _normalized_key(raw: object) -> tuple[str | None, bool]:
    if raw is None:
        return None, False
    original = str(raw)
    canonical = " ".join(unicodedata.normalize("NFKC", original).split())
    if not canonical:
        return None, bool(original)
    return canonical, canonical != original


def _normalized_display_value(raw: object) -> str | None:
    if raw is None:
        return None
    return " ".join(unicodedata.normalize("NFKC", str(raw)).split()) or None


def _require_fixed_policy_value(
    policy: HierarchyValuePolicy,
    label: str,
) -> None:
    if policy.mode == "fixed" and policy.value is None:
        raise ValueError(f"{label} fixed behavior requires a value")
    if policy.mode != "fixed" and policy.value is not None:
        raise ValueError(f"{label} behavior must not retain a hidden fixed value")


def _content_hash(payload: object) -> str:
    return "sha256:" + sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
