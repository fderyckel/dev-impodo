"""Browser mapping staging and row-level read-only readiness checks.

This module joins the submitted browser mapping to the existing preflight
engine. Source artifacts remain immutable, parent/child preparation is
repeated over every row, and Odoo requirements are planned in batches rather
than requested inside a source-row loop.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol
import unicodedata
from uuid import UUID, uuid4

from .access import Actor, AuthorizationPolicy, Capability
from .artifacts import ArtifactStore
from .connectors import (
    MetadataRequest,
    MetadataSnapshot,
    RecordRequest,
    RecordSnapshot,
)
from .derived_entities import DerivedEntityPlan, RelatedDatasetRule
from .engine import PreflightEngine
from .inspection import SourceFileCatalog
from .mapping_semantics import (
    DatasetMapping,
    MappingDefinition,
    MappingRevision,
    MappingSubmission,
    RelationshipResolver,
    ResolverOrigin,
    ScalarValueError,
    ScalarValueRuleError,
    ScalarValueSource,
    canonicalize_scalar_value,
)
from .models import (
    Classification,
    Decision,
    InvalidPreparedValue,
    PreflightResult,
    canonical_json_bytes,
    portable_value,
    target_identity_hash,
)
from .planner import plan_metadata_requests, plan_record_requests
from .profile import (
    DatasetSpec,
    FieldSpec,
    IdentityComponent,
    NormalizationSpec,
    ProfileDocument,
    ProfileIdentity,
    RelationSpec,
    ResolveSpec,
    SourceIdentitySpec,
    SourceSpec,
    TargetIdentitySpec,
    TargetSpec,
)
from .projects import MigrationProject, SourceFile
from .source import (
    PreparedBundle,
    SourceRow,
    SourceTable,
    load_selected_source_table,
    prepare_source_tables,
)
from .workspace import SourceDataset, SourceSelection, WorkspaceError


READINESS_CONTRACT_VERSION = 1
MANIFEST_NAME = "impodo_preflight_manifest.json"


class ReadinessError(WorkspaceError):
    """Raised when the current browser evidence cannot be checked safely."""


@dataclass(frozen=True, slots=True)
class ReadinessRow:
    dataset: str
    dataset_label: str
    source_row: int
    status: str
    classification: str
    identity: str
    reason: str
    field: str
    recommended_action: str
    technical_code: str
    issue_count: int = 0


@dataclass(frozen=True, slots=True)
class ReadinessDataset:
    dataset: str
    label: str
    target_model: str
    total: int
    ready: int
    needs_review: int
    blocked: int


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    run_id: str
    project_id: str
    mapping_id: str
    mapping_version: int
    mapping_content_hash: str
    target_hash: str
    checked_at: datetime
    checked_by: str
    datasets: tuple[ReadinessDataset, ...]
    rows: tuple[ReadinessRow, ...]
    contract_version: int = READINESS_CONTRACT_VERSION

    @property
    def ready_count(self) -> int:
        return sum(item.ready for item in self.datasets)

    @property
    def needs_review_count(self) -> int:
        return sum(item.needs_review for item in self.datasets)

    @property
    def blocked_count(self) -> int:
        return sum(item.blocked for item in self.datasets)

    @property
    def total_count(self) -> int:
        return sum(item.total for item in self.datasets)

    @property
    def status(self) -> str:
        if self.blocked_count:
            return "BLOCKED"
        if self.needs_review_count:
            return "NEEDS_REVIEW"
        return "READY"

    def to_json(self) -> str:
        payload = asdict(self)
        payload["checked_at"] = self.checked_at.isoformat()
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, value: str) -> "ReadinessReport":
        payload = json.loads(value)
        if int(payload.get("contract_version", 0)) != READINESS_CONTRACT_VERSION:
            raise ValueError("Readiness report contract version is unsupported")
        return cls(
            run_id=str(payload["run_id"]),
            project_id=str(payload["project_id"]),
            mapping_id=str(payload["mapping_id"]),
            mapping_version=int(payload["mapping_version"]),
            mapping_content_hash=str(payload["mapping_content_hash"]),
            target_hash=str(payload["target_hash"]),
            checked_at=datetime.fromisoformat(str(payload["checked_at"])),
            checked_by=str(payload["checked_by"]),
            datasets=tuple(
                ReadinessDataset(**item) for item in payload.get("datasets", ())
            ),
            rows=tuple(ReadinessRow(**item) for item in payload.get("rows", ())),
        )


class ReadinessRepository(Protocol):
    def get(self, project_id: str) -> MigrationProject: ...

    def get_source_selection(self, project_id: str) -> SourceSelection | None: ...

    def get_mapping_source_selection(
        self, project_id: str
    ) -> SourceSelection | None: ...

    def get_source_catalogs(
        self, project_id: str
    ) -> tuple[SourceFileCatalog, ...]: ...

    def get_derived_entity_plan(
        self, project_id: str
    ) -> DerivedEntityPlan | None: ...

    def get_mapping_revision(
        self, project_id: str, version: int | None = None
    ) -> MappingRevision | None: ...

    def get_mapping_submission(
        self, project_id: str, version: int | None = None
    ) -> MappingSubmission | None: ...

    def get_readiness_report(
        self,
        project_id: str,
        mapping_id: str,
        mapping_version: int,
        mapping_content_hash: str,
    ) -> ReadinessReport | None: ...

    def save_readiness_report(
        self,
        project_id: str,
        report: ReadinessReport,
        *,
        actor: Actor,
    ) -> None: ...

    def project_directory(self, project_id: str) -> Path: ...


ReadinessReader = Callable[
    [tuple[MetadataRequest, ...], tuple[RecordRequest, ...]],
    tuple[MetadataSnapshot, RecordSnapshot],
]


@dataclass(frozen=True, slots=True)
class StagedBrowserMapping:
    profile: ProfileDocument
    prepared: PreparedBundle
    dataset_labels: Mapping[str, str]
    source_field_labels: Mapping[tuple[str, str], str]


class BrowserReadinessService:
    """Run and persist one row-level check for the current submitted mapping."""

    def __init__(
        self,
        repository: ReadinessRepository,
        artifacts: ArtifactStore,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.authorization = authorization
        self.engine = PreflightEngine()

    def current_report(self, project_id: str) -> ReadinessReport | None:
        revision = self.repository.get_mapping_revision(project_id)
        if revision is None:
            return None
        submission = self.repository.get_mapping_submission(
            project_id, revision.version
        )
        if submission is None:
            return None
        return self.repository.get_readiness_report(
            project_id,
            revision.mapping_id,
            revision.version,
            revision.definition.content_hash,
        )

    def run(
        self,
        project_id: str,
        *,
        reader: ReadinessReader,
        actor: Actor,
    ) -> ReadinessReport:
        self.authorization.require(
            actor,
            Capability.MAPPING_SUBMIT,
            project_id=project_id,
        )
        project = self.repository.get(project_id)
        revision = self.repository.get_mapping_revision(project_id)
        if revision is None:
            raise ReadinessError("Submit the mapping before checking data")
        submission = self.repository.get_mapping_submission(
            project_id, revision.version
        )
        if (
            submission is None
            or submission.mapping_content_hash != revision.definition.content_hash
        ):
            raise ReadinessError("Submit the current mapping before checking data")
        physical_selection = self.repository.get_source_selection(project_id)
        effective_selection = self.repository.get_mapping_source_selection(project_id)
        if physical_selection is None or effective_selection is None:
            raise ReadinessError("Freeze the source datasets before checking data")

        staged = stage_browser_mapping(
            project,
            revision.definition,
            physical_selection,
            effective_selection,
            self.repository.get_derived_entity_plan(project_id),
            self.repository.get_source_catalogs(project_id),
            self.artifacts,
        )
        metadata_requests = plan_metadata_requests(staged.profile)
        record_requests = plan_record_requests(
            staged.profile,
            staged.prepared.records,
        )
        metadata, records = reader(metadata_requests, record_requests)
        expected_target = target_identity_hash(
            connection_mode=(
                project.odoo_connection_mode.value
                if project.odoo_connection_mode is not None
                else ""
            ),
            base_url=project.odoo_base_url,
            database=project.odoo_database,
        )
        if metadata.fingerprint.target_hash != expected_target:
            raise ReadinessError("Readiness data came from a different Odoo target")
        result = self.engine.run(
            staged.profile,
            staged.prepared,
            metadata,
            records,
        )
        run_id = str(uuid4())
        report = _readiness_report(
            run_id,
            project,
            revision,
            result,
            staged.dataset_labels,
            staged.source_field_labels,
            actor,
        )
        _write_manifest(self.repository, project_id, run_id, result)
        self.repository.save_readiness_report(
            project_id,
            report,
            actor=actor,
        )
        return report


def stage_browser_mapping(
    project: MigrationProject,
    definition: MappingDefinition,
    physical_selection: SourceSelection,
    effective_selection: SourceSelection,
    plan: DerivedEntityPlan | None,
    catalogs: Iterable[SourceFileCatalog],
    artifacts: ArtifactStore,
) -> StagedBrowserMapping:
    """Compile browser mapping meaning and apply it to every selected row."""

    if definition.source_selection_hash != effective_selection.content_hash:
        raise ReadinessError("The submitted mapping no longer matches its source data")
    effective_by_id = {item.dataset_id: item for item in effective_selection.datasets}
    mapping_by_id = {item.dataset_id: item for item in definition.datasets}
    if set(mapping_by_id) != set(effective_by_id):
        raise ReadinessError("The submitted mapping does not cover every dataset")
    physical_by_id = {item.dataset_id: item for item in physical_selection.datasets}
    catalog_by_file = {item.file_id: item for item in catalogs}
    source_file_by_id = {item.file_id: item for item in project.source_files}
    split_by_name = {
        name: (rule, role)
        for rule in (plan.rules if plan else ())
        if isinstance(rule, RelatedDatasetRule)
        for name, role in (
            (rule.parent_dataset_name, "parent"),
            (rule.child_dataset_name, "child"),
        )
    }

    loaded: dict[str, SourceTable] = {}
    with ExitStack() as stack:
        for physical in physical_selection.datasets:
            source_file = source_file_by_id.get(physical.file_id)
            catalog = catalog_by_file.get(physical.file_id)
            table_catalog = next(
                (
                    item
                    for item in (catalog.tables if catalog else ())
                    if item.table_key == physical.table_key
                ),
                None,
            )
            if source_file is None or catalog is None or table_catalog is None:
                raise ReadinessError("Frozen source evidence is incomplete")
            path = stack.enter_context(
                artifacts.materialize_source(project.project_id, source_file.stored_name)
            )
            named_range = (
                table_catalog.named_tables[0].cell_range
                if table_catalog.kind == "NAMED_TABLE"
                and table_catalog.named_tables
                else None
            )
            table = load_selected_source_table(
                path,
                dataset=physical.name,
                table_key=physical.table_key,
                encoding=physical.encoding,
                delimiter=physical.delimiter,
                header_row=physical.header_row,
                named_table_range=named_range,
            )
            expected_hash = physical.source_sha256.removeprefix("sha256:")
            if table.content_hash != f"sha256:{expected_hash}":
                raise ReadinessError("Stored source content changed after selection")
            loaded[physical.dataset_id] = table

        profile = _compile_profile(definition, effective_selection)
        staged_tables: list[SourceTable] = []
        source_labels: dict[tuple[str, str], str] = {}
        for dataset_spec in profile.datasets:
            effective = next(
                item
                for item in effective_selection.datasets
                if item.name == dataset_spec.name
            )
            mapping = mapping_by_id[effective.dataset_id]
            split = split_by_name.get(effective.name)
            if split is None:
                physical = physical_by_id.get(effective.dataset_id)
                role = "source"
                rule = None
            else:
                rule, role = split
                physical = physical_by_id.get(rule.source_dataset_id)
            if physical is None:
                raise ReadinessError("Prepared dataset no longer has a source")
            staged_tables.append(
                _stage_table(
                    effective,
                    physical,
                    mapping,
                    loaded[physical.dataset_id],
                    rule,
                    role,
                )
            )
            for column in effective.columns:
                source_labels[(effective.name, column.stable_key)] = column.source_name
            column_name_by_key = {
                column.stable_key: column.source_name
                for column in effective.columns
            }
            for index, field in enumerate(mapping.fields):
                if field.value_source is ScalarValueSource.ODOO_DEFAULT:
                    continue
                source_labels[(effective.name, _synthetic_field(index))] = (
                    column_name_by_key.get(field.source_column_key or "")
                    or field.target_field
                )

        prepared = prepare_source_tables(
            profile,
            staged_tables,
            source_hashes={
                item.name: f"sha256:{item.source_sha256.removeprefix('sha256:')}"
                for item in effective_selection.datasets
            },
        )
    return StagedBrowserMapping(
        profile=profile,
        prepared=prepared,
        dataset_labels={item.name: item.name.replace("_", " ").title() for item in effective_selection.datasets},
        source_field_labels=source_labels,
    )


def _compile_profile(
    definition: MappingDefinition,
    selection: SourceSelection,
) -> ProfileDocument:
    datasets = {item.dataset_id: item for item in selection.datasets}
    mappings = {item.dataset_id: item for item in definition.datasets}

    def resolver(value: RelationshipResolver) -> ResolveSpec:
        if value.origin is ResolverOrigin.DATASET:
            target_mapping = mappings.get(str(value.dataset_id))
            target_dataset = datasets.get(str(value.dataset_id))
            if target_mapping is None or target_dataset is None:
                raise ReadinessError("A mapped relationship dataset is missing")
            return ResolveSpec(
                dataset=target_dataset.name,
                target_source_fields=target_mapping.source_identity_column_keys,
            )
        return ResolveSpec(
            target_model=value.model,
            target_fields=tuple(item.target_field for item in value.key_mappings),
            target_scope_fields=tuple(
                item.target_field for item in value.scope_mappings
            ),
        )

    profile_datasets: list[DatasetSpec] = []
    for mapping in definition.datasets:
        source_dataset = datasets[mapping.dataset_id]
        scalar_fields = {}
        for index, field in enumerate(mapping.fields):
            if field.value_source is ScalarValueSource.ODOO_DEFAULT:
                continue
            scalar_fields[field.target_field] = FieldSpec(
                source=_synthetic_field(index),
                type=field.value_type,
                required=field.required,
                required_on_create=field.required_on_create,
                compare=field.compare,
                validate_only=field.validate_only,
                normalize=NormalizationSpec(empty_as_null=True),
                null_policy=field.null_policy,
            )
        relations = {
            item.target_field: RelationSpec(
                kind=item.kind,
                source_fields=item.source_column_keys,
                resolve=resolver(item.resolver),
                compare=item.compare,
                validate_only=item.validate_only,
                required=item.required,
                required_on_create=item.required_on_create,
                on_missing=item.on_missing,
                on_ambiguous=item.on_ambiguous,
                operation=item.operation,
                separator=item.separator,
                null_policy=item.null_policy,
            )
            for item in mapping.relationships
        }
        identity_normalization = NormalizationSpec(
            trim=True,
            collapse_whitespace=True,
            empty_as_null=True,
        )
        profile_datasets.append(
            DatasetSpec(
                name=source_dataset.name,
                source=SourceSpec(file=f"{source_dataset.name}.csv"),
                target=TargetSpec(
                    model=mapping.target_model,
                    mode=mapping.mode.value,
                    on_existing=mapping.on_existing,
                ),
                source_identity=SourceIdentitySpec(
                    fields=mapping.source_identity_column_keys
                ),
                target_identity=TargetIdentitySpec(
                    components=tuple(
                        IdentityComponent(
                            source_fields=item.source_column_keys,
                            target_fields=item.target_fields,
                            type=item.value_type,
                            normalize=identity_normalization,
                            resolve=(
                                resolver(item.resolver)
                                if item.resolver is not None
                                else None
                            ),
                        )
                        for item in mapping.target_identity
                    ),
                    scope=tuple(
                        IdentityComponent(
                            source_fields=item.source_column_keys,
                            target_fields=item.target_fields,
                            type=item.value_type,
                            normalize=identity_normalization,
                            resolve=(
                                resolver(item.resolver)
                                if item.resolver is not None
                                else None
                            ),
                        )
                        for item in mapping.target_scope
                    ),
                ),
                fields=scalar_fields,
                relations=relations,
            )
        )
    token = definition.content_hash.removeprefix("sha256:")[:24]
    return ProfileDocument(
        profile=ProfileIdentity(
            id=f"browser_{token}",
            description="Compiled from a submitted Impodo browser mapping",
        ),
        datasets=tuple(profile_datasets),
    )


def _stage_table(
    effective: SourceDataset,
    physical: SourceDataset,
    mapping: DatasetMapping,
    table: SourceTable,
    rule: RelatedDatasetRule | None,
    role: str,
) -> SourceTable:
    source_name_by_key = {
        column.stable_key: column.source_name for column in physical.columns
    }
    staged_rows: list[SourceRow] = []
    seen_parent_keys: set[tuple[str, ...]] = set()
    for row in table.rows:
        values = {
            column.stable_key: row.values.get(source_name_by_key[column.stable_key])
            for column in effective.columns
        }
        if rule is not None:
            for key in (
                rule.parent_key_column_key,
                rule.scope_column_key,
                rule.child_key_column_key if role == "child" else None,
            ):
                if key is not None and key in values:
                    values[key] = _normalized_key(values.get(key))
        if role == "parent" and rule is not None:
            keys = tuple(
                values.get(key)
                for key in (
                    rule.parent_key_column_key,
                    rule.scope_column_key,
                )
                if key is not None
            )
            if all(value is not None for value in keys):
                canonical = tuple(str(value) for value in keys)
                if canonical in seen_parent_keys:
                    continue
                seen_parent_keys.add(canonical)
        for index, field in enumerate(mapping.fields):
            if field.value_source is ScalarValueSource.ODOO_DEFAULT:
                continue
            raw = (
                values.get(field.source_column_key)
                if field.source_column_key is not None
                else None
            )
            try:
                values[_synthetic_field(index)] = canonicalize_scalar_value(
                    field,
                    raw,
                    formula_context={
                        "value": raw,
                        **{
                            f"column_{column.ordinal}": values.get(
                                column.stable_key
                            )
                            for column in effective.columns
                        },
                    },
                )
            except ScalarValueRuleError as error:
                values[_synthetic_field(index)] = InvalidPreparedValue(
                    code=error.code,
                    message=str(error),
                )
            except ScalarValueError as error:
                values[_synthetic_field(index)] = (
                    None
                    if "required value" in str(error).casefold()
                    else "__impodo_invalid_value__"
                )
        staged_rows.append(SourceRow(number=row.number, values=values))
    headers = (
        *(column.stable_key for column in effective.columns),
        *(
            _synthetic_field(index)
            for index, field in enumerate(mapping.fields)
            if field.value_source is not ScalarValueSource.ODOO_DEFAULT
        ),
    )
    return SourceTable(
        dataset=effective.name,
        path=table.path,
        headers=tuple(headers),
        rows=tuple(staged_rows),
        content_hash=table.content_hash,
    )


def _synthetic_field(index: int) -> str:
    return f"__impodo_scalar_{index}"


def _normalized_key(value: object) -> str | None:
    if value is None:
        return None
    normalized = " ".join(unicodedata.normalize("NFKC", str(value)).split())
    return normalized or None


def _readiness_report(
    run_id: str,
    project: MigrationProject,
    revision: MappingRevision,
    result: PreflightResult,
    dataset_labels: Mapping[str, str],
    source_labels: Mapping[tuple[str, str], str],
    actor: Actor,
) -> ReadinessReport:
    rows = tuple(
        _readiness_row(decision, dataset_labels, source_labels)
        for decision in result.decisions
    )
    target_by_dataset: dict[str, str] = {}
    for item in result.metadata_coverage:
        if item.get("dataset") and item.get("model"):
            target_by_dataset.setdefault(
                str(item["dataset"]),
                str(item["model"]),
            )
    datasets = []
    for dataset in dict.fromkeys(
        [*dataset_labels, *(item.dataset for item in rows)]
    ):
        dataset_rows = [item for item in rows if item.dataset == dataset]
        datasets.append(
            ReadinessDataset(
                dataset=dataset,
                label=dataset_labels.get(dataset, dataset),
                target_model=target_by_dataset.get(dataset, ""),
                total=len(dataset_rows),
                ready=sum(item.status == "ready" for item in dataset_rows),
                needs_review=sum(
                    item.status == "needs_review" for item in dataset_rows
                ),
                blocked=sum(item.status == "blocked" for item in dataset_rows),
            )
        )
    return ReadinessReport(
        run_id=run_id,
        project_id=project.project_id,
        mapping_id=revision.mapping_id,
        mapping_version=revision.version,
        mapping_content_hash=revision.definition.content_hash,
        target_hash=result.fingerprint.target_hash,
        checked_at=datetime.now(timezone.utc),
        checked_by=actor.identity.display_name,
        datasets=tuple(datasets),
        rows=rows,
    )


def _readiness_row(
    decision: Decision,
    labels: Mapping[str, str],
    source_labels: Mapping[tuple[str, str], str],
) -> ReadinessRow:
    status = (
        "needs_review"
        if decision.classification is Classification.AMBIGUOUS
        else (
            "blocked"
            if decision.classification is Classification.BLOCKED
            else "ready"
        )
    )
    issue = next((item for item in decision.issues if item.blocking), None)
    if issue is None and decision.issues:
        issue = decision.issues[0]
    code = (
        issue.code
        if issue is not None
        else (
            "TARGET_IDENTITY_AMBIGUOUS"
            if decision.classification is Classification.AMBIGUOUS
            else ""
        )
    )
    reason, action = _plain_guidance(code, decision.classification)
    field = issue.field if issue is not None and issue.field else ""
    field = source_labels.get((decision.dataset, field), field)
    identity = " · ".join(
        _display_value(item) for item in decision.business_identity
    ) or "—"
    return ReadinessRow(
        dataset=decision.dataset,
        dataset_label=labels.get(decision.dataset, decision.dataset),
        source_row=decision.source_row,
        status=status,
        classification=decision.classification.value,
        identity=identity,
        reason=reason,
        field=field,
        recommended_action=action,
        technical_code=code,
        issue_count=len(decision.issues),
    )


def _plain_guidance(
    code: str,
    classification: Classification,
) -> tuple[str, str]:
    guidance = {
        "SOURCE_FIELD_MISSING": (
            "A mapped source column is unavailable.",
            "Return to mapping and choose an available column.",
        ),
        "SOURCE_IDENTITY_INVALID": (
            "A required key is empty or invalid.",
            "Complete the key in the source data.",
        ),
        "SOURCE_IDENTITY_DUPLICATE": (
            "This row uses the same key as another row.",
            "Keep one unique row or correct the key.",
        ),
        "SOURCE_REQUIRED_VALUE_MISSING": (
            "A required value is missing.",
            "Complete the value and check again.",
        ),
        "SOURCE_TYPE_INVALID": (
            "A value has the wrong format.",
            "Correct the value format and check again.",
        ),
        "SOURCE_TEXT_LENGTH_INVALID": (
            "A value has the wrong number of characters.",
            "Correct the value or review its exact-length rule.",
        ),
        "SOURCE_TEXT_SEGMENT_INVALID": (
            "Part of a value contains unexpected characters.",
            "Correct the value or review its character rule.",
        ),
        "SOURCE_PATTERN_MISMATCH": (
            "A value does not follow its custom format.",
            "Correct the value or review the advanced custom pattern.",
        ),
        "SOURCE_FORMULA_INVALID": (
            "A formula could not calculate this value.",
            "Review the row inputs and the field formula.",
        ),
        "SOURCE_REPLACEMENT_INVALID": (
            "Find and replace could not process this value safely.",
            "Review the find-and-replace rule.",
        ),
        "SOURCE_DECIMAL_ROUNDING_INVALID": (
            "A decimal value could not be rounded safely.",
            "Review the decimal value and rounding rule.",
        ),
        "SOURCE_REFERENCE_DUPLICATE": (
            "This row repeats the same related key.",
            "Remove the duplicate related value.",
        ),
        "REFERENCE_NOT_FOUND": (
            "A related record cannot be found.",
            "Add or correct the related key.",
        ),
        "REFERENCE_AMBIGUOUS": (
            "A related key matches more than one record.",
            "Use a more specific business key.",
        ),
        "REFERENCE_BLOCKED_BY_DEPENDENCY": (
            "A related parent row is blocked.",
            "Resolve the parent row first.",
        ),
        "TARGET_REFERENCE_UNRESOLVED": (
            "An Odoo relationship has no usable business key.",
            "Check the related Odoo record and its business key.",
        ),
        "TARGET_IDENTITY_AMBIGUOUS": (
            "More than one Odoo record matches this key.",
            "Review the matching Odoo records.",
        ),
        "REQUIRED_ON_CREATE_MISSING": (
            "Odoo needs another value to create this record.",
            "Map or provide the required value.",
        ),
        "CREATE_IDENTITY_EXISTS": (
            "This create-only key already exists in Odoo.",
            "Review the create-only policy.",
        ),
        "COMPARISON_UNSUPPORTED": (
            "This value cannot be compared safely.",
            "Review the mapped field type and comparison rule.",
        ),
    }
    if code in guidance:
        return guidance[code]
    if classification is Classification.CREATE:
        return "Ready to create.", "No action needed."
    if classification is Classification.UPDATE:
        return "Ready to update.", "Review changes in the package."
    if classification is Classification.UNCHANGED:
        return "Already matches Odoo.", "No action needed."
    if classification is Classification.AMBIGUOUS:
        return "More than one Odoo record matches.", "Review the matching records."
    return "This row cannot be processed safely.", "Review the row details."


def _display_value(value: object) -> str:
    portable = portable_value(value)
    if isinstance(portable, Mapping) and "value" in portable:
        return str(portable["value"])
    if isinstance(portable, list):
        return " / ".join(_display_value(item) for item in portable)
    if isinstance(portable, Mapping):
        return json.dumps(portable, ensure_ascii=False, separators=(",", ":"))
    return str(portable) if portable is not None else "—"


def _write_manifest(
    repository: ReadinessRepository,
    project_id: str,
    run_id: str,
    result: PreflightResult,
) -> Path:
    canonical_run_id = str(UUID(run_id))
    reports = repository.project_directory(project_id) / "reports" / canonical_run_id
    reports.mkdir(parents=False, exist_ok=False)
    target = reports / MANIFEST_NAME
    partial = target.with_suffix(".json.partial")
    partial.write_bytes(canonical_json_bytes(result.to_portable_dict()) + b"\n")
    partial.replace(target)
    return target
