"""Resolve prepared data and classify its effect on the Odoo target.

The engine is the orchestration layer for a preflight run.  It receives typed
source records and read-only Odoo snapshots, then:

1. validates the compiled plan against live/snapshotted Odoo metadata;
2. resolves symbolic references to stable business references;
3. indexes existing targets by the compiled plan's business identity;
4. classifies each actionable source row as create, update, unchanged,
   ambiguous, or blocked; and
5. returns deterministic, grouped evidence for reporting.

Numeric Odoo IDs remain internal to :class:`~impodo.catalog.TargetCatalog`.
The decisions and field differences emitted by this module use portable
business references instead.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any, Iterable

from .canonical import ValueParseError, parse_value, values_equal
from .catalog import TargetCatalog, relation_ids
from .connectors import MetadataSnapshot, RecordSnapshot
from .domain.compiler.contracts import CompiledMigrationPlan
from .metadata import validate_plan_metadata
from .models import (
    BusinessReference,
    Classification,
    Decision,
    FieldDifference,
    Issue,
    LogicalReference,
    PreparedRecord,
    PreflightResult,
    ReferenceResolution,
    Severity,
    TargetRecord,
    canonical_json_text,
    portable_value,
)
from .profile import (
    DatasetSpec,
    IdentityComponent,
    RelationSpec,
    ResolveSpec,
)
from .selection_domains import (
    apply_live_selection_domains,
    live_selection_metadata_issues,
    validate_selection_metadata_drift,
)
from .source import PreparedBundle
from .workspace_contracts import OdooSchemaCatalog


class PreflightEngine:
    """Coordinate metadata checks, resolution, comparison, and classification."""

    def run(
        self,
        plan: CompiledMigrationPlan,
        prepared: PreparedBundle,
        metadata_snapshot: MetadataSnapshot,
        record_snapshot: RecordSnapshot,
        *,
        captured_schema: OdooSchemaCatalog | None = None,
    ) -> PreflightResult:
        """Execute one deterministic, read-only preflight.

        ``prepared`` is produced by :func:`source.prepare_sources`; both
        snapshots are produced by the same connector run.  Their fingerprints
        must match so decisions cannot accidentally combine two Odoo states.

        Returns:
            A portable result containing row decisions, differences,
            resolution evidence, issues, hashes, and metadata coverage.

        Raises:
            ValueError: If snapshot provenance differs or record coverage is
                explicitly incomplete.
        """

        if metadata_snapshot.fingerprint != record_snapshot.fingerprint:
            raise ValueError("metadata and record snapshots have different fingerprints")
        if not record_snapshot.complete:
            raise ValueError("record snapshot is incomplete")

        metadata_issues, coverage = validate_plan_metadata(
            plan, metadata_snapshot
        )
        metadata_issues = (
            *metadata_issues,
            *live_selection_metadata_issues(plan, metadata_snapshot),
            *validate_selection_metadata_drift(
                plan,
                metadata_snapshot,
                captured_schema,
            ),
        )
        catalog = TargetCatalog(record_snapshot.records)
        with_metadata_issues = _apply_dataset_issues(
            prepared.records, metadata_issues
        )
        with_selection_issues = apply_live_selection_domains(
            plan,
            with_metadata_issues,
            metadata_snapshot,
        )
        resolved_records, resolution_evidence = _resolve_records(
            plan, with_selection_issues, catalog
        )

        decisions: list[Decision] = []
        runtime_issues: list[Issue] = []
        for dataset in plan.datasets:
            if dataset.target.mode == "reference":
                continue
            dataset_records = [
                record for record in resolved_records if record.dataset == dataset.name
            ]
            target_index, index_issues = _build_target_index(
                plan, dataset, catalog
            )
            runtime_issues.extend(index_issues)
            if index_issues:
                dataset_records = [
                    replace(record, issues=(*record.issues, *index_issues))
                    for record in dataset_records
                ]
            for record in dataset_records:
                decisions.append(
                    _classify_record(plan, dataset, record, target_index, catalog)
                )

        all_issues = [
            *prepared.issues,
            *metadata_issues,
            *runtime_issues,
            *(
                issue
                for record in resolved_records
                for issue in record.issues
            ),
        ]
        grouped_issues = _group_issues(all_issues)
        grouped_resolutions = _group_resolutions(resolution_evidence)
        dataset_order = {
            dataset.name: index for index, dataset in enumerate(plan.datasets)
        }
        decisions.sort(
            key=lambda decision: (
                dataset_order[decision.dataset],
                canonical_json_text(portable_value(decision.business_identity)),
                decision.source_row,
            )
        )
        return PreflightResult(
            profile_id=plan.plan_id,
            source_hashes=prepared.source_hashes,
            fingerprint=record_snapshot.fingerprint,
            metadata_snapshot_hash=metadata_snapshot.content_hash,
            record_snapshot_hash=record_snapshot.content_hash,
            decisions=tuple(decisions),
            reference_resolutions=tuple(grouped_resolutions),
            issues=tuple(grouped_issues),
            metadata_coverage=coverage,
        )


def _apply_dataset_issues(
    records: Iterable[PreparedRecord],
    issues: Iterable[Issue],
) -> tuple[PreparedRecord, ...]:
    """Attach global and dataset metadata issues to applicable source records.

    Issues are indexed once by dataset, avoiding a full issue scan for every
    row.
    """

    by_dataset: dict[str | None, list[Issue]] = defaultdict(list)
    for issue in issues:
        by_dataset[issue.dataset].append(issue)
    result = []
    for record in records:
        applicable = (*by_dataset.get(None, ()), *by_dataset.get(record.dataset, ()))
        result.append(
            replace(record, issues=(*record.issues, *applicable))
            if applicable
            else record
        )
    return tuple(result)


def _resolve_records(
    plan: CompiledMigrationPlan,
    records: Iterable[PreparedRecord],
    catalog: TargetCatalog,
) -> tuple[tuple[PreparedRecord, ...], tuple[ReferenceResolution, ...]]:
    """Resolve all logical references and collect auditable lookup evidence.

    Incoming references use source identities from this same bundle; target
    references use the catalog's prebuilt indexes.  Recursive incoming
    dependencies are cached by dataset and source row, so a referenced row is
    resolved only once.
    """

    original = tuple(records)
    by_dataset_identity: dict[
        tuple[str, tuple[Any, ...]], list[PreparedRecord]
    ] = defaultdict(list)
    for record in original:
        by_dataset_identity[(record.dataset, record.source_identity)].append(record)

    cache: dict[tuple[str, int], PreparedRecord] = {}
    evidence: list[ReferenceResolution] = []

    def resolve_record(record: PreparedRecord) -> PreparedRecord:
        """Resolve one row's identity, scope, and relation values recursively."""

        cache_key = (record.dataset, record.source_row)
        if cache_key in cache:
            return cache[cache_key]
        dataset = plan.dataset(record.dataset)
        record_issues = list(record.issues)

        identity = []
        for index, value in enumerate(record.target_identity):
            component = _expanded_component(dataset.target_identity.components, index)
            identity.append(
                resolve_value(
                    value,
                    record,
                    f"target_identity:{component.target_fields[0]}",
                    record_issues,
                    Severity.ERROR,
                )
            )
        scope = []
        for index, value in enumerate(record.target_scope):
            component = _expanded_component(dataset.target_identity.scope, index)
            scope.append(
                resolve_value(
                    value,
                    record,
                    f"scope:{component.target_fields[0]}",
                    record_issues,
                    Severity.ERROR,
                )
            )

        references: dict[str, Any] = {}
        for field_name, value in record.references.items():
            relation = dataset.relations[field_name]
            severity_missing = (
                Severity.ERROR
                if relation.on_missing == "error"
                else Severity.WARNING
            )
            if isinstance(value, tuple):
                references[field_name] = tuple(
                    resolve_value(
                        item,
                        record,
                        field_name,
                        record_issues,
                        severity_missing,
                        ambiguous_severity=(
                            Severity.ERROR
                            if relation.on_ambiguous == "error"
                            else Severity.WARNING
                        ),
                    )
                    for item in value
                )
            elif value is None:
                references[field_name] = None
            else:
                references[field_name] = resolve_value(
                    value,
                    record,
                    field_name,
                    record_issues,
                    severity_missing,
                    ambiguous_severity=(
                        Severity.ERROR
                        if relation.on_ambiguous == "error"
                        else Severity.WARNING
                    ),
                )

        resolved = replace(
            record,
            target_identity=tuple(identity),
            target_scope=tuple(scope),
            references=references,
            issues=tuple(record_issues),
        )
        cache[cache_key] = resolved
        return resolved

    def resolve_value(
        value: Any,
        owner: PreparedRecord,
        field_name: str,
        issues: list[Issue],
        missing_severity: Severity,
        *,
        ambiguous_severity: Severity = Severity.ERROR,
    ) -> Any:
        """Resolve one logical reference or pass an already scalar value through.

        A unique match becomes a ``BusinessReference``.  Missing, ambiguous,
        or dependency-blocked matches keep the logical value, attach the
        configured issue severity, and always add resolution evidence.
        """

        if not isinstance(value, LogicalReference):
            return value
        matches: tuple[Any, ...]
        if value.origin == "incoming":
            candidates = by_dataset_identity.get(
                (str(value.dataset), value.key), ()
            )
            matches = tuple(candidates)
        else:
            matches = catalog.find_by_fields(
                str(value.model), value.target_fields, value.key
            )

        if len(matches) == 0:
            status = "NOT_FOUND"
            issues.append(
                Issue(
                    code="REFERENCE_NOT_FOUND",
                    message=(
                        f"{field_name} reference {value.key!r} was not found"
                    ),
                    severity=missing_severity,
                    dataset=owner.dataset,
                    row=owner.source_row,
                    field=field_name,
                )
            )
            result: Any = value
        elif len(matches) > 1:
            status = "AMBIGUOUS"
            issues.append(
                Issue(
                    code="REFERENCE_AMBIGUOUS",
                    message=(
                        f"{field_name} reference {value.key!r} matched "
                        f"{len(matches)} records"
                    ),
                    severity=ambiguous_severity,
                    dataset=owner.dataset,
                    row=owner.source_row,
                    field=field_name,
                    affected_count=len(matches),
                )
            )
            result = value
        elif value.origin == "incoming":
            target_record = resolve_record(matches[0])
            if target_record.blocked:
                status = "BLOCKED_BY_DEPENDENCY"
                issues.append(
                    Issue(
                        code="REFERENCE_BLOCKED_BY_DEPENDENCY",
                        message=(
                            f"{field_name} depends on blocked "
                            f"{target_record.dataset} {target_record.source_identity!r}"
                        ),
                        severity=Severity.ERROR,
                        dataset=owner.dataset,
                        row=owner.source_row,
                        field=field_name,
                    )
                )
                result = value
            else:
                status = "RESOLVED"
                result = BusinessReference(
                    model=target_record.target_model,
                    key=tuple(target_record.target_identity),
                    scope=tuple(target_record.target_scope),
                )
        else:
            status = "RESOLVED"
            result = BusinessReference(
                model=str(value.model),
                key=value.key,
                scope=value.scope,
            )
        evidence.append(
            ReferenceResolution(
                dataset=owner.dataset,
                field=field_name,
                reference=value,
                status=status,
                match_count=len(matches),
            )
        )
        return result

    resolved_records = tuple(resolve_record(record) for record in original)
    return resolved_records, tuple(evidence)


def _expanded_component(
    components: tuple[IdentityComponent, ...], flat_index: int
) -> IdentityComponent:
    """Map a flattened identity-value index back to its compiled component.

    Direct components occupy one position per target field; resolved
    components occupy one position because they become one business reference.
    """

    cursor = 0
    for component in components:
        width = 1 if component.resolve is not None else len(component.target_fields)
        if cursor <= flat_index < cursor + width:
            return component
        cursor += width
    raise IndexError(flat_index)


def _build_target_index(
    plan: CompiledMigrationPlan,
    dataset: DatasetSpec,
    catalog: TargetCatalog,
) -> tuple[
    dict[tuple[tuple[Any, ...], tuple[Any, ...]], tuple[TargetRecord, ...]],
    tuple[Issue, ...],
]:
    """Index one model's Odoo records by canonical identity and scope.

    Each target row is canonicalized once.  Records with the same key remain
    together so :func:`_classify_record` can distinguish a unique match from an
    ambiguous target identity without additional Odoo reads.
    """

    buckets: dict[
        tuple[tuple[Any, ...], tuple[Any, ...]], list[TargetRecord]
    ] = defaultdict(list)
    issues: list[Issue] = []
    for target in catalog.records(dataset.target.model):
        try:
            identity = _target_identity(
                plan, dataset.target_identity.components, target, catalog
            )
            scope = _target_identity(
                plan, dataset.target_identity.scope, target, catalog
            )
        except (KeyError, ValueError, ValueParseError) as exc:
            issues.append(
                Issue(
                    code="TARGET_REFERENCE_UNRESOLVED",
                    message=f"cannot canonicalize target identity: {exc}",
                    dataset=dataset.name,
                )
            )
            continue
        buckets[(identity, scope)].append(target)
    return (
        {key: tuple(records) for key, records in buckets.items()},
        tuple(_group_issues(issues)),
    )


def _target_identity(
    plan: CompiledMigrationPlan,
    components: tuple[IdentityComponent, ...],
    target: TargetRecord,
    catalog: TargetCatalog,
) -> tuple[Any, ...]:
    """Canonicalize identity components from an existing Odoo target record.

    Scalar components use the same parsing policy as source data.  Relational
    components are converted from raw Odoo IDs to stable business references
    through the catalog.
    """

    result: list[Any] = []
    for component in components:
        if component.resolve is None:
            for target_field in component.target_fields:
                raw = target.values.get(target_field)
                if raw is False and component.type != "boolean":
                    raw = None
                result.append(
                    parse_value(
                        raw,
                        component.type,
                        component.normalize,
                        required=True,
                    )
                )
            continue
        model, identity_fields, scope_fields = _resolve_target_shape(
            plan, component.resolve
        )
        result.append(
            catalog.reference_from_id(
                model,
                target.values.get(component.target_fields[0]),
                identity_fields,
                scope_fields,
            )
        )
    return tuple(result)


def _resolve_target_shape(
    plan: CompiledMigrationPlan,
    resolve: ResolveSpec,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Return the model, identity fields, and scope fields for a resolver.

    Explicit target resolvers provide the shape directly.  Incoming-dataset
    resolvers inherit it from the referenced dataset's target identity.
    """

    if resolve.target_model is not None:
        return (
            resolve.target_model,
            resolve.target_fields,
            resolve.target_scope_fields,
        )
    referenced = plan.dataset(str(resolve.dataset))
    identity_fields = tuple(
        target_field
        for component in referenced.target_identity.components
        for target_field in component.target_fields
    )
    scope_fields = tuple(
        target_field
        for component in referenced.target_identity.scope
        for target_field in component.target_fields
    )
    return referenced.target.model, identity_fields, scope_fields


def _classify_record(
    plan: CompiledMigrationPlan,
    dataset: DatasetSpec,
    record: PreparedRecord,
    target_index: dict[
        tuple[tuple[Any, ...], tuple[Any, ...]], tuple[TargetRecord, ...]
    ],
    catalog: TargetCatalog,
) -> Decision:
    """Classify one prepared row against its canonical target matches.

    Blocking source/reference issues take precedence.  A missing target becomes
    ``CREATE`` only after create-required fields are checked; multiple targets
    become ``AMBIGUOUS``; create-only datasets follow ``on_existing``; and a
    unique upsert match is compared field by field for ``UPDATE`` versus
    ``UNCHANGED``.
    """

    business_identity = tuple(record.target_identity)
    business_scope = tuple(record.target_scope)
    matches = target_index.get(
        (tuple(record.target_identity), tuple(record.target_scope)), ()
    )
    record_issues = list(record.issues)
    if record.blocked:
        return Decision(
            dataset=record.dataset,
            source_row=record.source_row,
            source_trace_id=record.source_trace_id,
            business_identity=business_identity,
            business_scope=business_scope,
            classification=Classification.BLOCKED,
            target_match_count=len(matches),
            issues=tuple(record_issues),
        )
    if len(matches) > 1:
        issue = Issue(
            code="TARGET_IDENTITY_AMBIGUOUS",
            message=f"target identity matched {len(matches)} records",
            dataset=record.dataset,
            row=record.source_row,
            affected_count=len(matches),
        )
        return Decision(
            dataset=record.dataset,
            source_row=record.source_row,
            source_trace_id=record.source_trace_id,
            business_identity=business_identity,
            business_scope=business_scope,
            classification=Classification.AMBIGUOUS,
            target_match_count=len(matches),
            issues=(issue,),
        )
    if len(matches) == 0:
        record_issues.extend(_required_on_create_issues(dataset, record))
        classification = (
            Classification.BLOCKED
            if any(issue.blocking for issue in record_issues)
            else Classification.CREATE
        )
        return Decision(
            dataset=record.dataset,
            source_row=record.source_row,
            source_trace_id=record.source_trace_id,
            business_identity=business_identity,
            business_scope=business_scope,
            classification=classification,
            target_match_count=0,
            issues=tuple(record_issues),
        )

    if dataset.target.mode == "create":
        if dataset.target.on_existing == "block":
            issue = Issue(
                code="CREATE_IDENTITY_EXISTS",
                message="create-only dataset identity already exists",
                dataset=record.dataset,
                row=record.source_row,
            )
            return Decision(
                dataset=record.dataset,
                source_row=record.source_row,
                source_trace_id=record.source_trace_id,
                business_identity=business_identity,
                business_scope=business_scope,
                classification=Classification.BLOCKED,
                target_match_count=1,
                issues=(issue,),
            )
        return Decision(
            dataset=record.dataset,
            source_row=record.source_row,
            source_trace_id=record.source_trace_id,
            business_identity=business_identity,
            business_scope=business_scope,
            classification=Classification.UNCHANGED,
            target_match_count=1,
        )

    differences, comparison_issues = _compare_record(
        plan, dataset, record, matches[0], catalog
    )
    record_issues.extend(comparison_issues)
    if any(issue.blocking for issue in record_issues):
        classification = Classification.BLOCKED
        differences = ()
    elif differences:
        classification = Classification.UPDATE
    else:
        classification = Classification.UNCHANGED
    return Decision(
        dataset=record.dataset,
        source_row=record.source_row,
        source_trace_id=record.source_trace_id,
        business_identity=business_identity,
        business_scope=business_scope,
        classification=classification,
        target_match_count=1,
        differences=tuple(differences),
        issues=tuple(record_issues),
    )


def _required_on_create_issues(
    dataset: DatasetSpec, record: PreparedRecord
) -> list[Issue]:
    """Report mapped scalar/relation values required only for new records."""

    issues = []
    for field_name, spec in dataset.fields.items():
        if spec.required_on_create and record.scalar_values.get(field_name) is None:
            issues.append(
                Issue(
                    code="REQUIRED_ON_CREATE_MISSING",
                    message=f"{field_name} is required when creating",
                    dataset=record.dataset,
                    row=record.source_row,
                    field=field_name,
                )
            )
    for field_name, spec in dataset.relations.items():
        if spec.required_on_create and not record.references.get(field_name):
            issues.append(
                Issue(
                    code="REQUIRED_ON_CREATE_MISSING",
                    message=f"{field_name} is required when creating",
                    dataset=record.dataset,
                    row=record.source_row,
                    field=field_name,
                )
            )
    return issues


def _compare_record(
    plan: CompiledMigrationPlan,
    dataset: DatasetSpec,
    source: PreparedRecord,
    target: TargetRecord,
    catalog: TargetCatalog,
) -> tuple[list[FieldDifference], list[Issue]]:
    """Compare one source row with its unique target using compiled semantics.

    Scalar target values are normalized with the same rules as their source
    values.  Relational target IDs are first converted to business references.
    Fields marked ``validate_only`` or with comparison disabled are excluded.
    """

    differences: list[FieldDifference] = []
    issues: list[Issue] = []
    identity = tuple(source.target_identity)
    for field_name, spec in dataset.fields.items():
        if not spec.compare or spec.validate_only:
            continue
        proposed = source.scalar_values.get(field_name)
        raw_existing = target.values.get(field_name)
        if raw_existing is False and spec.type != "boolean":
            raw_existing = None
        try:
            existing = parse_value(
                raw_existing,
                spec.type,
                spec.normalize,
                required=False,
            )
        except ValueParseError as exc:
            issues.append(
                Issue(
                    code="COMPARISON_UNSUPPORTED",
                    message=f"cannot normalize target {field_name}: {exc}",
                    dataset=source.dataset,
                    row=source.source_row,
                    field=field_name,
                )
            )
            continue
        if not values_equal(proposed, existing, spec.null_policy):
            differences.append(
                FieldDifference(
                    dataset=source.dataset,
                    business_identity=identity,
                    business_scope=tuple(source.target_scope),
                    field=field_name,
                    existing=existing,
                    proposed=proposed,
                    comparison_rule=_field_rule(spec),
                )
            )

    for field_name, spec in dataset.relations.items():
        if not spec.compare or spec.validate_only:
            continue
        proposed = source.references.get(field_name)
        try:
            existing = _existing_relation(
                plan,
                spec,
                target.values.get(field_name),
                catalog,
            )
        except (KeyError, ValueError) as exc:
            issues.append(
                Issue(
                    code="TARGET_REFERENCE_UNRESOLVED",
                    message=f"cannot canonicalize target {field_name}: {exc}",
                    dataset=source.dataset,
                    row=source.source_row,
                    field=field_name,
                )
            )
            continue

        material, final_proposal = _relation_difference(spec, existing, proposed)
        if material:
            differences.append(
                FieldDifference(
                    dataset=source.dataset,
                    business_identity=identity,
                    business_scope=tuple(source.target_scope),
                    field=field_name,
                    existing=existing,
                    proposed=final_proposal,
                    comparison_rule=(
                        f"{spec.kind}:{spec.operation}:"
                        f"null={spec.null_policy}"
                    ),
                )
            )
    return differences, issues


def _existing_relation(
    plan: CompiledMigrationPlan,
    spec: RelationSpec,
    raw_value: Any,
    catalog: TargetCatalog,
) -> Any:
    """Convert an Odoo relation value into portable business references.

    Many2many results are sorted canonically so their order in Odoo cannot
    produce a false difference.
    """

    model, identity_fields, scope_fields = _resolve_target_shape(
        plan, spec.resolve
    )
    if spec.kind == "many2one":
        return catalog.reference_from_id(
            model, raw_value, identity_fields, scope_fields
        )
    return tuple(
        sorted(
            (
                catalog.reference_from_id(
                    model, odoo_id, identity_fields, scope_fields
                )
                for odoo_id in relation_ids(raw_value)
            ),
            key=lambda item: canonical_json_text(portable_value(item)),
        )
    )


def _relation_difference(
    spec: RelationSpec, existing: Any, proposed: Any
) -> tuple[bool, Any]:
    """Apply relation null/operation policy and return materiality plus result.

    Many2one comparison honours source-null semantics.  Many2many comparison
    computes the final set for ``replace``, ``add``, or ``remove`` before
    deciding whether a change is material.
    """

    if spec.kind == "many2one":
        if spec.null_policy == "ignore_source_null" and proposed is None:
            return False, existing
        if spec.null_policy == "equivalent" and not proposed and not existing:
            return False, existing
        return existing != proposed, proposed

    existing_set = set(existing or ())
    proposed_set = set(proposed or ())
    if spec.operation == "replace":
        final = proposed_set
    elif spec.operation == "add":
        final = existing_set | proposed_set
    else:
        final = existing_set - proposed_set
    ordered = tuple(
        sorted(final, key=lambda item: canonical_json_text(portable_value(item)))
    )
    return final != existing_set, ordered


def _field_rule(spec: Any) -> str:
    """Describe the scalar comparison policy stored beside a difference."""

    active = [
        name
        for name in ("trim", "collapse_whitespace", "casefold", "empty_as_null")
        if getattr(spec.normalize, name)
    ]
    if spec.normalize.decimal_places is not None:
        active.append(f"decimal_places={spec.normalize.decimal_places}")
    return f"{spec.type};normalize={','.join(active) or 'none'};null={spec.null_policy}"


def _group_issues(issues: Iterable[Issue]) -> list[Issue]:
    """Deduplicate equivalent issues and aggregate their affected row counts.

    Grouping removes repeated dataset-wide metadata/runtime issues that were
    attached to individual records, while retaining a row number when exactly
    one distinct row is affected.
    """

    grouped: dict[tuple[Any, ...], list[Issue]] = defaultdict(list)
    for issue in issues:
        grouped[
            (
                issue.code,
                issue.message,
                issue.severity,
                issue.dataset,
                issue.field,
            )
        ].append(issue)
    result = []
    for key, group in grouped.items():
        code, message, severity, dataset, field = key
        rows = [issue.row for issue in group if issue.row is not None]
        result.append(
            Issue(
                code=code,
                message=message,
                severity=severity,
                dataset=dataset,
                row=min(rows) if len(set(rows)) == 1 else None,
                field=field,
                affected_count=len(set(rows)) if rows else max(
                    issue.affected_count for issue in group
                ),
            )
        )
    return sorted(
        result,
        key=lambda issue: (
            issue.severity.value,
            issue.code,
            issue.dataset or "",
            issue.field or "",
            issue.message,
        ),
    )


def _group_resolutions(
    evidence: Iterable[ReferenceResolution],
) -> list[ReferenceResolution]:
    """Aggregate identical reference outcomes into deterministic evidence rows."""

    grouped: dict[tuple[Any, ...], list[ReferenceResolution]] = defaultdict(list)
    for item in evidence:
        grouped[
            (
                item.dataset,
                item.field,
                canonical_json_text(portable_value(item.reference)),
                item.status,
                item.match_count,
            )
        ].append(item)
    result = []
    for group in grouped.values():
        first = group[0]
        result.append(replace(first, affected_count=len(group)))
    return sorted(
        result,
        key=lambda item: (
            item.dataset,
            item.field,
            item.status,
            canonical_json_text(portable_value(item.reference)),
        ),
    )
