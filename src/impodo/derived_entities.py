"""Governed plans for extracting reusable entities from denormalized fields.

This module deliberately implements the authoring boundary only.  It derives a
bounded preview from the already-inspected source catalog; the future staging
compiler must repeat the same deterministic rules over every source row before
an export can be certified.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Iterable, Protocol
import unicodedata
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .access import Actor, AuthorizationPolicy, Capability
from .inspection import SourceFileCatalog
from .projects import MigrationProject
from .workspace import SourceDataset, SourceSelection, WorkspaceError


DERIVED_ENTITY_CONTRACT_VERSION = 1
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
class DerivedEntityPlan:
    """Immutable revision of all derived-entity authoring rules in a project."""

    plan_id: str
    version: int
    project_id: str
    source_selection_hash: str
    rules: tuple[DerivedEntityRule, ...]
    updated_at: datetime
    updated_by: str
    contract_version: int = DERIVED_ENTITY_CONTRACT_VERSION

    @property
    def content_hash(self) -> str:
        return _content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "plan_id": self.plan_id,
            "version": self.version,
            "project_id": self.project_id,
            "source_selection_hash": self.source_selection_hash,
            "rules": [
                asdict(item)
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
        return _canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "DerivedEntityPlan":
        payload = json.loads(value)
        result = cls(
            plan_id=str(payload["plan_id"]),
            version=int(payload["version"]),
            project_id=str(payload["project_id"]),
            source_selection_hash=str(payload["source_selection_hash"]),
            rules=tuple(
                DerivedEntityRule(**item) for item in payload.get("rules", ())
            ),
            updated_at=datetime.fromisoformat(str(payload["updated_at"])),
            updated_by=str(payload["updated_by"]),
            contract_version=int(
                payload.get("contract_version", DERIVED_ENTITY_CONTRACT_VERSION)
            ),
        )
        if result.contract_version != DERIVED_ENTITY_CONTRACT_VERSION:
            raise ValueError("Derived-entity plan contract version is unsupported")
        if payload.get("content_hash") != result.content_hash:
            raise ValueError("Derived-entity plan content hash is invalid")
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


class DerivedEntityRepository(Protocol):
    def get(self, project_id: str) -> MigrationProject: ...

    def get_source_selection(self, project_id: str) -> SourceSelection | None: ...

    def get_source_catalogs(
        self, project_id: str
    ) -> tuple[SourceFileCatalog, ...]: ...

    def get_derived_entity_plan(
        self, project_id: str
    ) -> DerivedEntityPlan | None: ...

    def save_derived_entity_plan(
        self,
        project_id: str,
        plan: DerivedEntityPlan,
        *,
        expected_parent_version: int | None,
        actor: Actor,
    ) -> None: ...


class DerivedEntityWorkspaceService:
    """Author and preview deterministic derived-entity rules."""

    def __init__(
        self,
        repository: DerivedEntityRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.authorization = authorization

    def save_rule(
        self,
        project_id: str,
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
        self.authorization.require(
            actor,
            Capability.NORMALIZATION_DECIDE,
            project_id=project_id,
        )
        selection = self.repository.get_source_selection(project_id)
        if selection is None:
            raise WorkspaceError("Freeze source datasets before deriving entities")
        source_dataset = _source_dataset(
            selection,
            source_dataset_id,
            source_column_key,
        )
        current = self.repository.get_derived_entity_plan(project_id)
        actual_parent = current.version if current else None
        if expected_parent_version != actual_parent:
            raise WorkspaceError(
                "The derived-entity plan was modified by another request; reload it"
            )
        if current and current.source_selection_hash != selection.content_hash:
            raise WorkspaceError(
                "The derived-entity plan is stale; rebuild it from the frozen datasets"
            )

        rule = DerivedEntityRule(
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
        existing_names = {item.name for item in selection.datasets}
        existing_names.update(
            item.output_dataset_name for item in (current.rules if current else ())
        )
        if rule.output_dataset_name in existing_names:
            raise WorkspaceError("Derived dataset names must be unique in the project")

        now = datetime.now(timezone.utc)
        plan = DerivedEntityPlan(
            plan_id=current.plan_id if current else str(uuid4()),
            version=(current.version + 1 if current else 1),
            project_id=project_id,
            source_selection_hash=selection.content_hash,
            rules=(*(current.rules if current else ()), rule),
            updated_at=now,
            updated_by=actor.identity.display_name,
        )
        self.repository.save_derived_entity_plan(
            project_id,
            plan,
            expected_parent_version=actual_parent,
            actor=actor,
        )
        return plan, rule

    def delete_rule(
        self,
        project_id: str,
        rule_id: str,
        *,
        expected_parent_version: int | None,
        actor: Actor,
    ) -> DerivedEntityPlan:
        self.authorization.require(
            actor,
            Capability.NORMALIZATION_DECIDE,
            project_id=project_id,
        )
        current = self.repository.get_derived_entity_plan(project_id)
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
            project_id=current.project_id,
            source_selection_hash=current.source_selection_hash,
            rules=remaining,
            updated_at=datetime.now(timezone.utc),
            updated_by=actor.identity.display_name,
        )
        self.repository.save_derived_entity_plan(
            project_id,
            plan,
            expected_parent_version=current.version,
            actor=actor,
        )
        return plan

    def preview(
        self,
        project_id: str,
        rule: DerivedEntityRule,
    ) -> DerivedEntityPreview:
        selection = self.repository.get_source_selection(project_id)
        if selection is None:
            raise WorkspaceError("Freeze source datasets before deriving entities")
        return preview_derived_entities(
            rule,
            selection,
            self.repository.get_source_catalogs(project_id),
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
    catalog = next(
        (
            item
            for item in catalogs
            if item.file_id == dataset.file_id
            and item.source_sha256 == dataset.source_sha256
            and item.content_hash == dataset.catalog_hash
        ),
        None,
    )
    table = next(
        (
            item
            for item in (catalog.tables if catalog else ())
            if item.table_key == dataset.table_key
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


def _content_hash(payload: object) -> str:
    return "sha256:" + sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
