"""Govern Stage B/D preparation plans for related logical datasets.

Layer: domain contracts plus application workspace service. The browser can
author bounded previews for lookup extraction and parent/child dataset splits.
Both rule types participate in the effective mapping selection and are repeated
over every source row by readiness staging without changing the frozen source.

See ``docs/architecture/python-code-map.md``,
``docs/user/guides/related-tables.md``, and
``tests/test_derived_entities.py``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Iterable, Protocol
import unicodedata
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .access import Actor, AuthorizationPolicy, Capability
from .inspection import SourceFileCatalog
from .domain.source_binding import DerivedSourceBinding, require_file_source
from .workspace_contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from .workspace_errors import WorkspaceError
from .domain.structural import (
    ExactJoinRule,
    GroupAggregateRule,
    StructuralRule,
    UnionAllRule,
    structural_mapping_selection,
)


DERIVED_ENTITY_CONTRACT_VERSION = 4
_DATASET_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_EXTERNAL_ID_NAMESPACE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_TECHNICAL_NAME = re.compile(r"^[a-z_][a-z0-9_.]{0,127}$")
_TECHNICAL_FIELD = re.compile(r"^[a-z_][a-z0-9_]{0,127}$")
_SUPPORTED_BLANK_POLICIES = frozenset({"block", "quarantine"})


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


SourcePreparationRule = DerivedEntityRule | RelatedDatasetRule | StructuralRule


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
        if self.contract_version != DERIVED_ENTITY_CONTRACT_VERSION:
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
    """One category-owned or otherwise related entity in a bounded preview."""

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


class DerivedSourceRepository(Protocol):
    """Provide the exact frozen source evidence used to author and preview rules."""

    def get_source_selection(self, workspace_id: str) -> SourceSelection | None:
        """Return the current frozen physical selection."""
        ...

    def get_source_catalogs(
        self, workspace_id: str
    ) -> tuple[SourceFileCatalog, ...]:
        """Return bounded previews and field labels for the frozen sources."""
        ...


class DerivedEntityRepository(Protocol):
    """Persist immutable source-preparation plan revisions and a current pointer."""

    def get_derived_entity_plan(
        self, workspace_id: str
    ) -> DerivedEntityPlan | None:
        """Return the current source-preparation plan, if one exists."""
        ...

    def save_derived_entity_plan(
        self,
        workspace_id: str,
        plan: DerivedEntityPlan,
        *,
        expected_parent_version: int | None,
        actor: Actor,
    ) -> None:
        """Append a plan at the expected parent version and invalidate mapping."""
        ...


class DerivedEntityWorkspaceService:
    """Author and preview deterministic derived-entity rules.

    Saved rules are bound to the current frozen physical selection and use an
    optimistic plan version. Previews are bounded authoring evidence only; the
    staging evaluator later repeats accepted rules across every frozen row.
    """

    def __init__(
        self,
        sources: DerivedSourceRepository,
        derived_entities: DerivedEntityRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.sources = sources
        self.derived_entities = derived_entities
        self.authorization = authorization

    def save_rule(
        self,
        workspace_id: str,
        *,
        output_dataset_name: str,
        source_dataset_id: str,
        source_column_key: str,
        target_model: str,
        target_name_field: str,
        external_id_namespace: str,
        parent_separator: str | None,
        blank_policy: str,
        expected_parent_version: int | None,
        actor: Actor,
    ) -> tuple[DerivedEntityPlan, DerivedEntityRule]:
        """Append one reviewed lookup-extraction rule to the current plan."""

        self.authorization.require(
            actor,
            Capability.NORMALIZATION_DECIDE,
            workspace_id=workspace_id,
        )
        selection = self.sources.get_source_selection(workspace_id)
        if selection is None:
            raise WorkspaceError("Freeze source datasets before deriving entities")
        current = self.derived_entities.get_derived_entity_plan(workspace_id)
        actual_parent = current.version if current else None
        if expected_parent_version != actual_parent:
            raise WorkspaceError(
                "The derived-entity plan was modified by another request; reload it"
            )
        if current and current.source_selection_hash != selection.content_hash:
            raise WorkspaceError(
                "The derived-entity plan is stale; rebuild it from the frozen datasets"
            )

        rule = self._lookup_rule(
            selection,
            output_dataset_name=output_dataset_name,
            source_dataset_id=source_dataset_id,
            source_column_key=source_column_key,
            target_model=target_model,
            target_name_field=target_name_field,
            external_id_namespace=external_id_namespace,
            parent_separator=parent_separator,
            blank_policy=blank_policy,
        )
        self._validate_lookup_rule_availability(rule, selection, current)

        now = datetime.now(timezone.utc)
        plan = DerivedEntityPlan(
            plan_id=current.plan_id if current else str(uuid4()),
            version=(current.version + 1 if current else 1),
            workspace_id=workspace_id,
            source_selection_hash=selection.content_hash,
            rules=(*(current.rules if current else ()), rule),
            updated_at=now,
            updated_by=actor.identity.display_name,
        )
        self.derived_entities.save_derived_entity_plan(
            workspace_id,
            plan,
            expected_parent_version=actual_parent,
            actor=actor,
        )
        return plan, rule

    def preview_lookup(
        self,
        workspace_id: str,
        *,
        output_dataset_name: str,
        source_dataset_id: str,
        source_column_key: str,
        target_model: str,
        target_name_field: str,
        external_id_namespace: str,
        parent_separator: str | None,
        blank_policy: str,
    ) -> tuple[DerivedEntityRule, DerivedEntityPreview]:
        """Validate and preview related-record extraction without saving it."""

        selection = self.sources.get_source_selection(workspace_id)
        if selection is None:
            raise WorkspaceError("Freeze source datasets before deriving entities")
        current = self.derived_entities.get_derived_entity_plan(workspace_id)
        rule = self._lookup_rule(
            selection,
            output_dataset_name=output_dataset_name,
            source_dataset_id=source_dataset_id,
            source_column_key=source_column_key,
            target_model=target_model,
            target_name_field=target_name_field,
            external_id_namespace=external_id_namespace,
            parent_separator=parent_separator,
            blank_policy=blank_policy,
        )
        self._validate_lookup_rule_availability(rule, selection, current)
        return (
            rule,
            preview_derived_entities(
                rule,
                selection,
                self.sources.get_source_catalogs(workspace_id),
            ),
        )

    @staticmethod
    def _lookup_rule(
        selection: SourceSelection,
        *,
        output_dataset_name: str,
        source_dataset_id: str,
        source_column_key: str,
        target_model: str,
        target_name_field: str,
        external_id_namespace: str,
        parent_separator: str | None,
        blank_policy: str,
    ) -> DerivedEntityRule:
        source_dataset = _source_dataset(
            selection,
            source_dataset_id,
            source_column_key,
        )
        return DerivedEntityRule(
            rule_id=str(uuid4()),
            output_dataset_name=output_dataset_name,
            source_dataset_id=source_dataset.dataset_id,
            source_column_key=source_column_key,
            target_model=target_model,
            target_name_field=target_name_field,
            external_id_namespace=external_id_namespace,
            parent_separator=parent_separator,
            blank_policy=blank_policy,
        )

    @staticmethod
    def _validate_lookup_rule_availability(
        rule: DerivedEntityRule,
        selection: SourceSelection,
        current: DerivedEntityPlan | None,
    ) -> None:
        existing_names = {item.name for item in selection.datasets}
        existing_names.update(_rule_dataset_names(current.rules if current else ()))
        if rule.output_dataset_name in existing_names:
            raise WorkspaceError("Derived dataset names must be unique in the workspace")

    def preview_related_split(
        self,
        workspace_id: str,
        *,
        source_dataset_id: str,
        parent_dataset_name: str,
        child_dataset_name: str,
        parent_key_column_key: str,
        child_key_column_key: str,
        scope_column_key: str | None,
        blank_policy: str,
    ) -> tuple[RelatedDatasetRule, RelatedDatasetPreview]:
        """Validate and preview a split without changing the workspace plan."""

        selection = self.sources.get_source_selection(workspace_id)
        if selection is None:
            raise WorkspaceError("Freeze source datasets before preparing related data")
        rule = self._related_rule(
            selection,
            source_dataset_id=source_dataset_id,
            parent_dataset_name=parent_dataset_name,
            child_dataset_name=child_dataset_name,
            parent_key_column_key=parent_key_column_key,
            child_key_column_key=child_key_column_key,
            scope_column_key=scope_column_key,
            blank_policy=blank_policy,
        )
        self._validate_related_rule_availability(
            rule,
            selection,
            self.derived_entities.get_derived_entity_plan(workspace_id),
        )
        return (
            rule,
            preview_related_datasets(
                rule,
                selection,
                self.sources.get_source_catalogs(workspace_id),
            ),
        )

    def save_related_split(
        self,
        workspace_id: str,
        *,
        source_dataset_id: str,
        parent_dataset_name: str,
        child_dataset_name: str,
        parent_key_column_key: str,
        child_key_column_key: str,
        scope_column_key: str | None,
        blank_policy: str,
        expected_parent_version: int | None,
        actor: Actor,
    ) -> tuple[DerivedEntityPlan, RelatedDatasetRule]:
        """Persist one reviewed parent/child split as an immutable plan revision."""

        self.authorization.require(
            actor,
            Capability.NORMALIZATION_DECIDE,
            workspace_id=workspace_id,
        )
        selection = self.sources.get_source_selection(workspace_id)
        if selection is None:
            raise WorkspaceError("Freeze source datasets before preparing related data")
        current = self.derived_entities.get_derived_entity_plan(workspace_id)
        actual_parent = current.version if current else None
        if expected_parent_version != actual_parent:
            raise WorkspaceError(
                "The source-preparation plan was modified by another request; reload it"
            )
        if current and current.source_selection_hash != selection.content_hash:
            raise WorkspaceError(
                "The source-preparation plan is stale; rebuild it from the frozen datasets"
            )

        rule = self._related_rule(
            selection,
            source_dataset_id=source_dataset_id,
            parent_dataset_name=parent_dataset_name,
            child_dataset_name=child_dataset_name,
            parent_key_column_key=parent_key_column_key,
            child_key_column_key=child_key_column_key,
            scope_column_key=scope_column_key,
            blank_policy=blank_policy,
        )
        self._validate_related_rule_availability(rule, selection, current)

        plan = DerivedEntityPlan(
            plan_id=current.plan_id if current else str(uuid4()),
            version=(current.version + 1 if current else 1),
            workspace_id=workspace_id,
            source_selection_hash=selection.content_hash,
            rules=(*(current.rules if current else ()), rule),
            updated_at=datetime.now(timezone.utc),
            updated_by=actor.identity.display_name,
        )
        self.derived_entities.save_derived_entity_plan(
            workspace_id,
            plan,
            expected_parent_version=actual_parent,
            actor=actor,
        )
        return plan, rule

    @staticmethod
    def _related_rule(
        selection: SourceSelection,
        *,
        source_dataset_id: str,
        parent_dataset_name: str,
        child_dataset_name: str,
        parent_key_column_key: str,
        child_key_column_key: str,
        scope_column_key: str | None,
        blank_policy: str,
    ) -> RelatedDatasetRule:
        rule = RelatedDatasetRule(
            rule_id=str(uuid4()),
            source_dataset_id=source_dataset_id,
            parent_dataset_name=parent_dataset_name,
            child_dataset_name=child_dataset_name,
            parent_key_column_key=parent_key_column_key,
            child_key_column_key=child_key_column_key,
            scope_column_key=scope_column_key,
            blank_policy=blank_policy,
        )
        _related_source_dataset(selection, rule)
        return rule

    @staticmethod
    def _validate_related_rule_availability(
        rule: RelatedDatasetRule,
        selection: SourceSelection,
        current: DerivedEntityPlan | None,
    ) -> None:
        existing_names = {item.name for item in selection.datasets}
        existing_names.update(_rule_dataset_names(current.rules if current else ()))
        if {rule.parent_dataset_name, rule.child_dataset_name}.intersection(
            existing_names
        ):
            raise WorkspaceError("Related dataset names must be unique in the workspace")
        if any(
            isinstance(item, RelatedDatasetRule)
            and item.source_dataset_id == rule.source_dataset_id
            for item in (current.rules if current else ())
        ):
            raise WorkspaceError(
                "This source dataset already has a parent/child split; remove it before creating another"
            )

    def delete_rule(
        self,
        workspace_id: str,
        rule_id: str,
        *,
        expected_parent_version: int | None,
        actor: Actor,
    ) -> DerivedEntityPlan:
        """Append a new plan revision without the requested existing rule."""

        self.authorization.require(
            actor,
            Capability.NORMALIZATION_DECIDE,
            workspace_id=workspace_id,
        )
        current = self.derived_entities.get_derived_entity_plan(workspace_id)
        if current is None:
            raise WorkspaceError("No derived-entity plan exists")
        if expected_parent_version != current.version:
            raise WorkspaceError(
                "The derived-entity plan was modified by another request; reload it"
            )
        remaining = tuple(item for item in current.rules if item.rule_id != rule_id)
        if len(remaining) == len(current.rules):
            raise WorkspaceError("Derived-entity rule not found")
        plan = DerivedEntityPlan(
            plan_id=current.plan_id,
            version=current.version + 1,
            workspace_id=current.workspace_id,
            source_selection_hash=current.source_selection_hash,
            rules=remaining,
            updated_at=datetime.now(timezone.utc),
            updated_by=actor.identity.display_name,
        )
        self.derived_entities.save_derived_entity_plan(
            workspace_id,
            plan,
            expected_parent_version=current.version,
            actor=actor,
        )
        return plan

    def preview(
        self,
        workspace_id: str,
        rule: DerivedEntityRule,
    ) -> DerivedEntityPreview:
        """Build a bounded lookup-extraction preview from current source catalogs."""

        selection = self.sources.get_source_selection(workspace_id)
        if selection is None:
            raise WorkspaceError("Freeze source datasets before deriving entities")
        return preview_derived_entities(
            rule,
            selection,
            self.sources.get_source_catalogs(workspace_id),
        )

    def preview_related(
        self,
        workspace_id: str,
        rule: RelatedDatasetRule,
    ) -> RelatedDatasetPreview:
        """Build a bounded parent/child preview from current source catalogs."""

        selection = self.sources.get_source_selection(workspace_id)
        if selection is None:
            raise WorkspaceError("Freeze source datasets before preparing related data")
        return preview_related_datasets(
            rule,
            selection,
            self.sources.get_source_catalogs(workspace_id),
        )


def preview_derived_entities(
    rule: DerivedEntityRule,
    selection: SourceSelection,
    catalogs: Iterable[SourceFileCatalog],
) -> DerivedEntityPreview:
    """Build a deterministic preview from the bounded inspection evidence."""

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
    profile = next(
        item for item in table.columns if item.ordinal == column.ordinal
    )

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
        parent_entity_id = (
            _identifiers(rule, parent_path)[0] if parent_path else None
        )
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


def preview_related_datasets(
    rule: RelatedDatasetRule,
    selection: SourceSelection,
    catalogs: Iterable[SourceFileCatalog],
) -> RelatedDatasetPreview:
    """Preview grouping and line identity from bounded inspection evidence."""

    dataset = _related_source_dataset(selection, rule)
    table = _source_table(dataset, catalogs)
    columns = {item.stable_key: item for item in dataset.columns}
    profiles = {item.ordinal: item for item in table.columns}
    parent_column = columns[rule.parent_key_column_key]
    child_column = columns[rule.child_key_column_key]
    scope_column = (
        columns[rule.scope_column_key] if rule.scope_column_key else None
    )

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
        scope_raw = (
            _row_value(row, scope_column.ordinal) if scope_column else None
        )
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
    catalogs: Iterable[SourceFileCatalog] = (),
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
    lookup_rules: dict[str, list[DerivedEntityRule]] = {}
    for item in (plan.rules if plan else ()):
        if isinstance(item, DerivedEntityRule):
            lookup_rules.setdefault(item.source_dataset_id, []).append(item)
    if not split_rules and not lookup_rules:
        return prepared_selection

    catalog_set = tuple(catalogs)
    effective: list[SourceDataset] = []
    for dataset in prepared_selection.datasets:
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
                        rule_hash=_content_hash(
                            _rule_payload(lookup_rule)
                        ),
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
                        rule_hash=_content_hash(
                            _rule_payload(rule)
                        ),
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
                        rule_hash=_content_hash(
                            _rule_payload(rule)
                        ),
                        input_dataset_ids=(dataset.dataset_id,),
                        data_hash=dataset.source_evidence_hash,
                    ),
                ),
            )
        )

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
    for rule in (plan.rules if plan else ()):
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
        if isinstance(rule, DerivedEntityRule)
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
        link.canonical_key_column_key: tuple(
            item.canonical_key for item in candidates
        ),
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
        *(
            (rule.scope_column_key,)
            if rule.scope_column_key is not None
            else ()
        ),
    }
    if not required.issubset(available):
        raise WorkspaceError("Choose key fields from the selected frozen dataset")
    return dataset


def _source_table(
    dataset: SourceDataset,
    catalogs: Iterable[SourceFileCatalog],
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
    rule: DerivedEntityRule,
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
    model_token = re.sub(r"[^a-z0-9_]+", "_", rule.target_model.casefold()).strip(
        "_"
    )
    return (
        f"entity:{identity}",
        f"impodo_{rule.external_id_namespace}.{model_token}_{identity.hex}",
    )


def _derived_dataset_link(
    rule: DerivedEntityRule,
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
    return DerivedDatasetLink(
        derived_dataset_id=prefix,
        consumer_dataset_id=consumer_dataset_id,
        source_column_key=rule.source_column_key,
        canonical_key_column_key=f"{prefix}:canonical_key",
        name_column_key=f"{prefix}:name",
        parent_key_column_key=(
            f"{prefix}:parent_key" if rule.parent_separator else None
        ),
        target_model=rule.target_model,
        target_name_field=rule.target_name_field,
    )


def _derived_dataset_identity(rule: DerivedEntityRule) -> UUID:
    payload = _canonical_json(
        {
            "output_dataset_name": rule.output_dataset_name,
            "source_dataset_id": rule.source_dataset_id,
            "source_column_key": rule.source_column_key,
            "target_model": rule.target_model,
            "target_name_field": rule.target_name_field,
            "external_id_namespace": rule.external_id_namespace,
            "parent_separator": rule.parent_separator,
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
        if isinstance(rule, DerivedEntityRule):
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


def _content_hash(payload: object) -> str:
    return "sha256:" + sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
