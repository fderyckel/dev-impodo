"""Compile saved Odoo fields into portable, policy-governed restore intents.

This pure domain boundary compares captured and destination metadata and
computes the effective values for one already matched record. It performs no
Odoo reads or writes and grants no write capability. The existing transfer
planner remains responsible for unique identity resolution, company context,
admitted references, exact approval, execution, and independent read-back.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from .odoo.compatibility import OdooOperation, assess_odoo_operation
from .odoo.field_capabilities import is_editable_scalar_field
from .serialization import canonical_json, content_hash
from .shared.models import (
    BusinessReference,
    Issue,
    LogicalReference,
    ScalarValue,
    portable_value,
)
from .workspace.contracts import SchemaField, SchemaModel

type RestoreReference = LogicalReference | BusinessReference
type RestoreValue = (
    ScalarValue | float | RestoreReference | tuple[RestoreReference, ...]
)

_FIELD_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_PROTECTED_FIELDS = frozenset(
    {"id", "create_uid", "create_date", "write_uid", "write_date", "__last_update"}
)


class RestoreValuePolicy(StrEnum):
    """Choose the effective scalar value of an existing matched record."""

    USE_SAVED = "use_saved"
    KEEP_DESTINATION = "keep_destination"
    FILL_EMPTY = "fill_empty"


class RestoreLinkPolicy(StrEnum):
    """Choose membership of a captured Many2many field without deleting rows."""

    REPLACE = "replace"
    KEEP = "keep"
    ADD_MISSING = "add_missing"


class RestoreCoverageKind(StrEnum):
    """Explain how a field participates in the effective restore scope."""

    RESTORE = "restore"
    EXCLUDED = "excluded"
    REMOVED = "removed"
    INCOMPATIBLE = "incompatible"
    NOT_WRITABLE = "not_writable"
    DESTINATION_ONLY = "destination_only"
    CREATE_DEFAULT = "create_default"
    REQUIRED_VALUE = "required_value"
    MODEL_MISSING = "model_missing"


@dataclass(frozen=True, slots=True)
class RestoreFieldRule:
    """Remember one explicit source-field decision for a destination profile."""

    source_field: str
    target_field: str | None = None
    policy: RestoreValuePolicy = RestoreValuePolicy.USE_SAVED
    link_policy: RestoreLinkPolicy = RestoreLinkPolicy.REPLACE
    excluded: bool = False

    def __post_init__(self) -> None:
        _require_field(self.source_field)
        if self.target_field is not None:
            _require_field(self.target_field)
        object.__setattr__(self, "policy", RestoreValuePolicy(self.policy))
        object.__setattr__(self, "link_policy", RestoreLinkPolicy(self.link_policy))
        if not isinstance(self.excluded, bool):
            raise TypeError("A restore exclusion must be an explicit boolean")

    def to_dict(self) -> dict[str, object]:
        """Serialize portable policy choices without data or target IDs."""

        return {
            "source_field": self.source_field,
            "target_field": self.target_field,
            "policy": self.policy.value,
            "link_policy": self.link_policy.value,
            "excluded": self.excluded,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RestoreFieldRule:
        """Reject malformed persisted rules instead of guessing old meaning."""

        if set(value) != {
            "source_field",
            "target_field",
            "policy",
            "link_policy",
            "excluded",
        }:
            raise ValueError("Stored restore field rule has an unsupported shape")
        source, target = value["source_field"], value["target_field"]
        policy, links, excluded = (
            value["policy"],
            value["link_policy"],
            value["excluded"],
        )
        if (
            not isinstance(source, str)
            or (target is not None and not isinstance(target, str))
            or not isinstance(policy, str)
            or not isinstance(links, str)
            or not isinstance(excluded, bool)
        ):
            raise ValueError("Stored restore field rule contains invalid values")
        return cls(
            source,
            target,
            RestoreValuePolicy(policy),
            RestoreLinkPolicy(links),
            excluded,
        )


@dataclass(frozen=True, slots=True)
class RestoreCreateDefault:
    """Supply one explicit creation value or a checked native Odoo default."""

    field_name: str
    value: RestoreValue = None
    use_odoo_default: bool = False

    def __post_init__(self) -> None:
        _require_field(self.field_name)
        if not isinstance(self.use_odoo_default, bool):
            raise TypeError("A native restore default choice must be explicit")
        if self.use_odoo_default and self.value is not None:
            raise ValueError("A restore default cannot provide two competing values")


@dataclass(frozen=True, slots=True)
class RestoreFieldCoverage:
    """Describe restored, omitted, or unresolved metadata without data rows."""

    source_field: str | None
    target_field: str | None
    kind: RestoreCoverageKind
    message: str


@dataclass(frozen=True, slots=True)
class CompiledRestoreField:
    """Bind a portable field policy to the exact compared field definitions."""

    source: SchemaField
    destination: SchemaField
    policy: RestoreValuePolicy
    link_policy: RestoreLinkPolicy


@dataclass(frozen=True, slots=True)
class OdooRestorePlan:
    """Hold metadata, coverage, and policies needed by the transfer compiler."""

    model: str
    source_version: str
    destination_version: str
    fields: tuple[CompiledRestoreField, ...]
    destination_fields: tuple[SchemaField, ...]
    identity_fields: tuple[str, ...]
    defaults: tuple[RestoreCreateDefault, ...]
    coverage: tuple[RestoreFieldCoverage, ...]
    issues: tuple[Issue, ...]

    @property
    def ready(self) -> bool:
        """Return whether schema decisions allow row-level compilation."""

        return not any(issue.blocking for issue in self.issues)

    @property
    def content_hash(self) -> str:
        """Bind exact metadata and policy meaning without captured data rows."""

        return content_hash(portable_value(asdict(self)))


@dataclass(frozen=True, slots=True)
class RestoreRowResult:
    """Return exact write intent and policy-aware verification expectations."""

    writes: tuple[tuple[str, RestoreValue], ...]
    expected_values: tuple[tuple[str, RestoreValue], ...]
    odoo_default_fields: tuple[str, ...]
    issues: tuple[Issue, ...]

    @property
    def ready(self) -> bool:
        """Return whether the result is safe to pass to admitted planning."""

        return not any(issue.blocking for issue in self.issues)


def restore_policy_binding(
    plans: Mapping[str, OdooRestorePlan] | None,
    generation_id: str | None,
    *,
    required_models: Iterable[str] = (),
) -> str | None:
    """Bind an admitted restore policy set to one fresh destination generation.

    Ordinary transfer callers pass neither argument. A restore cannot inherit
    the ordinary transfer defaults by omitting its policy set, and policy
    objects cannot grant an ordinary workspace restoration privileges.
    """

    if plans is None and generation_id is None:
        return None
    if plans is None or generation_id is None:
        raise ValueError(
            "Saved-data restoration requires its generation and explicit field plans"
        )
    try:
        UUID(generation_id)
    except (ValueError, AttributeError) as error:
        raise ValueError("The saved-data destination generation is invalid") from error
    if any(name != plan.model or not plan.ready for name, plan in plans.items()):
        raise ValueError("Resolve every selected restore field plan before continuing")
    missing = set(required_models) - set(plans)
    if missing:
        raise ValueError(
            "Restore field policies must cover every writable record type: "
            + ", ".join(sorted(missing))
        )
    return content_hash(
        {
            "destination_generation": generation_id,
            "restore_plans": {name: plans[name].content_hash for name in sorted(plans)},
        }
    )


def compile_odoo_restore(
    source: SchemaModel,
    destination: SchemaModel | None,
    *,
    source_version: str,
    destination_version: str,
    captured_fields: tuple[str, ...] | None = None,
    identity_fields: tuple[str, ...] = (),
    rules: tuple[RestoreFieldRule, ...] = (),
    defaults: tuple[RestoreCreateDefault, ...] = (),
    creates_expected: bool = True,
) -> OdooRestorePlan:
    """Compile same-major schema differences and explicit bulk field rules.

    ``captured_fields`` identifies actual saved columns; a field present only
    in the source schema does not become captured data. Defaults are required
    only for missing creation inputs when ``creates_expected`` is true. This
    does not bypass per-row required-value validation for an actual create.
    """

    source_fields = _index_fields(source.fields)
    selected = tuple(
        sorted(source_fields if captured_fields is None else captured_fields)
    )
    if len(selected) != len(set(selected)) or not set(selected) <= source_fields.keys():
        raise ValueError("Captured restore fields must be unique source fields")
    if len(identity_fields) != len(set(identity_fields)) or not set(
        identity_fields
    ) <= set(selected):
        raise ValueError("Restore identity fields must be unique captured fields")
    rule_map = {rule.source_field: rule for rule in rules}
    if len(rule_map) != len(rules) or not rule_map.keys() <= set(selected):
        raise ValueError("Restore field rules must identify distinct captured fields")
    default_map = {item.field_name: item for item in defaults}
    if len(default_map) != len(defaults):
        raise ValueError("A restore field cannot have competing creation providers")

    issues = _version_issues(source_version, destination_version)
    coverage: list[RestoreFieldCoverage] = []
    compiled: list[CompiledRestoreField] = []
    target_fields = _index_fields(destination.fields) if destination is not None else {}
    if destination is None or destination.name != source.name:
        issues.append(
            _issue(
                "RESTORE_MODEL_MISSING",
                "The selected model is unavailable in the destination.",
            )
        )
        coverage.append(
            RestoreFieldCoverage(
                None,
                None,
                RestoreCoverageKind.MODEL_MISSING,
                "Resolve the missing destination model before restoring its records.",
            )
        )
    if not default_map.keys() <= target_fields.keys():
        raise ValueError("Restore defaults must identify existing destination fields")

    claimed_targets: set[str] = set()
    for name in selected:
        saved = source_fields[name]
        rule = rule_map.get(name, RestoreFieldRule(name))
        target_name = rule.target_field or name
        target = target_fields.get(target_name)
        if rule.excluded:
            if name in identity_fields:
                issues.append(
                    _issue(
                        "RESTORE_IDENTITY_EXCLUDED",
                        "A matching field cannot be excluded from restoration.",
                        name,
                    )
                )
            coverage.append(
                RestoreFieldCoverage(
                    name,
                    target_name,
                    RestoreCoverageKind.EXCLUDED,
                    "The saved value is preserved in the data version and omitted from this restore.",
                )
            )
            continue
        if target_name in claimed_targets:
            issues.append(
                _issue(
                    "RESTORE_FIELD_AMBIGUOUS",
                    "Several saved fields would write the same destination field.",
                    target_name,
                )
            )
            continue
        claimed_targets.add(target_name)
        if name in _PROTECTED_FIELDS or target_name in _PROTECTED_FIELDS:
            issues.append(
                _issue(
                    "RESTORE_PROTECTED_FIELD",
                    "Instance-specific identity and audit fields cannot be restored as business values.",
                    name,
                )
            )
            continue
        if name in identity_fields and (
            target_name != name
            or rule.policy is not RestoreValuePolicy.USE_SAVED
            or rule.link_policy is not RestoreLinkPolicy.REPLACE
        ):
            issues.append(
                _issue(
                    "RESTORE_IDENTITY_POLICY",
                    "Matching fields require a separately reviewed identity strategy before changing their meaning.",
                    name,
                )
            )
            continue
        if target is None:
            coverage.append(
                RestoreFieldCoverage(
                    name,
                    target_name,
                    RestoreCoverageKind.REMOVED,
                    "This saved field is absent from the destination; choose an exclusion or replacement field.",
                )
            )
            issues.append(
                _issue(
                    "RESTORE_FIELD_REMOVED",
                    "Review the missing destination field or explicitly exclude it.",
                    name,
                )
            )
            continue
        if saved.type != target.type or saved.relation != target.relation:
            coverage.append(
                RestoreFieldCoverage(
                    name,
                    target_name,
                    RestoreCoverageKind.INCOMPATIBLE,
                    "The field type or related model changed; no implicit conversion is permitted.",
                )
            )
            issues.append(
                _issue(
                    "RESTORE_FIELD_INCOMPATIBLE",
                    "Choose a compatible field mapping or explicit exclusion.",
                    name,
                )
            )
            continue
        if not _writable(target):
            coverage.append(
                RestoreFieldCoverage(
                    name,
                    target_name,
                    RestoreCoverageKind.NOT_WRITABLE,
                    "The destination capability does not admit this field for restoration.",
                )
            )
            issues.append(
                _issue(
                    "RESTORE_FIELD_NOT_WRITABLE",
                    "Review the generated, context-dependent, or unsupported destination field.",
                    name,
                )
            )
            continue
        if (
            rule.link_policy is not RestoreLinkPolicy.REPLACE
            and target.type != "many2many"
        ):
            issues.append(
                _issue(
                    "RESTORE_LINK_POLICY_INVALID",
                    "Collection membership choices apply only to Many2many fields.",
                    name,
                )
            )
            continue
        compiled.append(
            CompiledRestoreField(saved, target, rule.policy, rule.link_policy)
        )
        coverage.append(
            RestoreFieldCoverage(
                name,
                target_name,
                RestoreCoverageKind.RESTORE,
                "Apply the saved field policy to uniquely matched destination records.",
            )
        )

    providers: list[RestoreCreateDefault] = []
    supplied = {item.destination.name for item in compiled}
    for name, target in sorted(target_fields.items()):
        provider = default_map.get(name)
        if provider is not None:
            value = (
                target.create_default_value
                if provider.use_odoo_default
                else provider.value
            )
            if (
                not _writable(target)
                or (provider.use_odoo_default and not target.create_default_present)
                or not _valid_value(target, value)
            ):
                issues.append(
                    _issue(
                        "RESTORE_DEFAULT_INVALID",
                        "The creation provider does not supply a valid admitted field value.",
                        name,
                    )
                )
            else:
                providers.append(provider)
                coverage.append(
                    RestoreFieldCoverage(
                        None,
                        name,
                        RestoreCoverageKind.CREATE_DEFAULT,
                        "Use the checked creation provider when saved data does not supply this field.",
                    )
                )
        if name in supplied:
            continue
        if provider is None and target.required and _writable(target):
            if target.create_default_present and _valid_value(
                target, target.create_default_value
            ):
                providers.append(RestoreCreateDefault(name, use_odoo_default=True))
                coverage.append(
                    RestoreFieldCoverage(
                        None,
                        name,
                        RestoreCoverageKind.CREATE_DEFAULT,
                        "Use the checked Odoo creation default.",
                    )
                )
            else:
                coverage.append(
                    RestoreFieldCoverage(
                        None,
                        name,
                        RestoreCoverageKind.REQUIRED_VALUE,
                        "New records need a valid value or checked creation default.",
                    )
                )
                if creates_expected:
                    issues.append(
                        _issue(
                            "RESTORE_REQUIRED_VALUE_MISSING",
                            "Supply the new required creation value before restoring missing records.",
                            name,
                        )
                    )
        elif name not in supplied and provider is None:
            coverage.append(
                RestoreFieldCoverage(
                    None,
                    name,
                    RestoreCoverageKind.DESTINATION_ONLY,
                    "Keep this destination-only field on existing records and omit it from creation values.",
                )
            )

    return OdooRestorePlan(
        source.name,
        source_version,
        destination_version,
        tuple(compiled),
        tuple(target_fields[name] for name in sorted(target_fields)),
        tuple(identity_fields),
        tuple(providers),
        tuple(coverage),
        tuple(issues),
    )


def apply_odoo_restore(
    plan: OdooRestorePlan,
    saved_values: Mapping[str, RestoreValue],
    destination_values: Mapping[str, RestoreValue] | None = None,
    *,
    destination_match_count: int | None = None,
) -> RestoreRowResult:
    """Compute effective values for a uniquely matched or missing record.

    Values are portable references or typed scalars. Missing saved keys never
    become explicit clears. Any blocking finding suppresses all write intent
    for the row. The caller must still obtain exact write authorization and
    fresh target evidence before using the result.
    """

    issues = list(plan.issues)
    count = (
        (1 if destination_values is not None else 0)
        if destination_match_count is None
        else destination_match_count
    )
    if not isinstance(count, int) or isinstance(count, bool) or count not in {0, 1}:
        issues.append(
            _issue(
                "RESTORE_MATCH_AMBIGUOUS",
                "Resolve the destination record uniquely before applying field policies.",
            )
        )
    elif bool(count) != (destination_values is not None):
        issues.append(
            _issue(
                "RESTORE_MATCH_EVIDENCE_MISSING",
                "The matched record and destination values do not agree.",
            )
        )
    if issues:
        return RestoreRowResult((), (), (), tuple(issues))
    existing = destination_values is not None
    current = destination_values if destination_values is not None else {}
    writes: dict[str, RestoreValue] = {}
    expected: dict[str, RestoreValue] = {}
    for name in plan.identity_fields:
        if name not in saved_values:
            issues.append(
                _issue(
                    "RESTORE_IDENTITY_MISSING",
                    "The saved record is missing a required matching value.",
                    name,
                )
            )
    for item in plan.fields:
        name = item.destination.name
        if item.source.name not in saved_values:
            continue
        saved = saved_values[item.source.name]
        if not _valid_value(item.destination, saved):
            issues.append(
                _issue(
                    "RESTORE_SAVED_VALUE_INVALID",
                    "The saved value is incompatible with the destination field.",
                    name,
                )
            )
            continue
        if item.source.name in plan.identity_fields and _empty(item.destination, saved):
            issues.append(
                _issue(
                    "RESTORE_IDENTITY_EMPTY",
                    "A saved matching value must be populated.",
                    name,
                )
            )
            continue
        needs_current = existing and (
            item.source.name in plan.identity_fields
            or item.policy is not RestoreValuePolicy.USE_SAVED
            or item.link_policy is not RestoreLinkPolicy.REPLACE
        )
        if needs_current and name not in current:
            issues.append(
                _issue(
                    "RESTORE_DESTINATION_VALUE_MISSING",
                    "Read the current destination value before applying this field policy.",
                    name,
                )
            )
            continue
        if (
            existing
            and item.source.name in plan.identity_fields
            and not _equal(item.destination, saved, current[name])
        ):
            issues.append(
                _issue(
                    "RESTORE_IDENTITY_CHANGED",
                    "The saved matching value differs from the resolved destination identity.",
                    name,
                )
            )
            continue
        effective = saved
        if existing:
            if item.policy is RestoreValuePolicy.KEEP_DESTINATION or (
                item.policy is RestoreValuePolicy.FILL_EMPTY
                and not _empty(item.destination, current[name])
            ):
                effective = current[name]
            elif item.destination.type == "many2many":
                if item.link_policy is RestoreLinkPolicy.KEEP:
                    effective = current[name]
                elif item.link_policy is RestoreLinkPolicy.ADD_MISSING:
                    if not _valid_value(item.destination, current[name]):
                        issues.append(
                            _issue(
                                "RESTORE_DESTINATION_VALUE_INVALID",
                                "The current collection must contain portable references.",
                                name,
                            )
                        )
                        continue
                    effective = _merge_links(current[name], saved)
        if not _valid_value(item.destination, effective):
            issues.append(
                _issue(
                    "RESTORE_DESTINATION_VALUE_INVALID",
                    "The effective destination value is incompatible with its field.",
                    name,
                )
            )
            continue
        expected[name] = effective
        if (
            not existing
            or name not in current
            or not _equal(item.destination, effective, current[name])
        ):
            writes[name] = effective

    native_defaults: list[str] = []
    if not existing:
        fields = {field.name: field for field in plan.destination_fields}
        for provider in plan.defaults:
            if provider.field_name in expected:
                continue
            if provider.use_odoo_default:
                native_defaults.append(provider.field_name)
                expected[provider.field_name] = fields[
                    provider.field_name
                ].create_default_value
            else:
                writes[provider.field_name] = provider.value
                expected[provider.field_name] = provider.value
        for field in plan.destination_fields:
            if (
                field.required
                and _writable(field)
                and (
                    field.name not in expected
                    or not _valid_value(field, expected[field.name])
                )
            ):
                issues.append(
                    _issue(
                        "RESTORE_REQUIRED_VALUE_MISSING",
                        "The new record needs a valid required value before creation.",
                        field.name,
                    )
                )
    if issues:
        return RestoreRowResult((), (), (), tuple(issues))
    return RestoreRowResult(
        tuple(sorted(writes.items())),
        tuple(sorted(expected.items())),
        tuple(sorted(native_defaults)),
        (),
    )


def _require_field(name: str) -> None:
    if not isinstance(name, str) or _FIELD_NAME.fullmatch(name) is None:
        raise ValueError("Restore fields must use technical field names")


def _index_fields(fields: tuple[SchemaField, ...]) -> dict[str, SchemaField]:
    result = {field.name: field for field in fields}
    if len(result) != len(fields):
        raise ValueError("Restore schema contains duplicate field identities")
    return result


def _issue(code: str, message: str, field: str | None = None) -> Issue:
    return Issue(code, message, field=field)


def _version_issues(source_version: str, destination_version: str) -> list[Issue]:
    source = assess_odoo_operation(source_version, OdooOperation.CAPTURE_SOURCE)
    destination = assess_odoo_operation(destination_version, OdooOperation.WRITE)
    if (
        not source.allowed
        or not destination.allowed
        or source.version.major != destination.version.major
    ):
        return [
            _issue(
                "RESTORE_VERSION_INCOMPATIBLE",
                "Restoration requires supported source and destination databases of the same Odoo major version.",
            )
        ]
    return []


def _writable(field: SchemaField) -> bool:
    if field.name in _PROTECTED_FIELDS:
        return False
    if is_editable_scalar_field(field):
        return True
    return bool(
        field.type in {"many2one", "many2many"}
        and field.relation
        and not field.readonly
        and field.company_dependent is False
        and field.exportable is True
        and field.related is not True
        and not (field.computed is True and field.has_inverse is not True)
    )


def _empty(field: SchemaField, value: RestoreValue) -> bool:
    if field.type == "boolean":
        return value is None
    if field.type in {"integer", "float", "monetary"}:
        return value is None
    if field.type == "many2many":
        return value is None or value is False or value == ()
    return value is None or value is False or value == ""


def _valid_value(field: SchemaField, value: RestoreValue) -> bool:
    if _empty(field, value):
        return not field.required
    if field.type == "boolean":
        return isinstance(value, bool)
    if field.type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if field.type in {"float", "monetary"}:
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            return False
        if isinstance(value, Decimal):
            return value.is_finite()
        return True if isinstance(value, int) else math.isfinite(value)
    if field.type in {"char", "text", "html"}:
        return isinstance(value, str)
    if field.type == "selection":
        return isinstance(value, str) and value in {
            key for key, _label in field.selection
        }
    if field.type in {"date", "datetime"}:
        if isinstance(value, str):
            try:
                (date if field.type == "date" else datetime).fromisoformat(value)
            except ValueError:
                return False
            return True
        return (
            isinstance(value, datetime)
            if field.type == "datetime"
            else isinstance(value, date) and not isinstance(value, datetime)
        )
    if field.type == "many2one":
        return _valid_reference(value, field.relation)
    if field.type == "many2many":
        return isinstance(value, tuple) and all(
            _valid_reference(member, field.relation) for member in value
        )
    return False


def _valid_reference(value: object, model: str | None) -> bool:
    return bool(
        isinstance(value, (LogicalReference, BusinessReference))
        and value.model == model
        and value.key
        and "id" not in (*value.target_fields, *value.target_scope_fields)
        and len(value.target_fields) == len(value.key)
        and len(value.target_scope_fields) == len(value.scope)
        and all(
            item is not None and item is not False and item != "" for item in value.key
        )
    )


def _reference_key(value: RestoreReference) -> str:
    return canonical_json(portable_value(value))


def _merge_links(
    current: RestoreValue, saved: RestoreValue
) -> tuple[RestoreReference, ...]:
    current_links = current if isinstance(current, tuple) else ()
    saved_links = saved if isinstance(saved, tuple) else ()
    members = {
        _reference_key(member): member for member in (*current_links, *saved_links)
    }
    return tuple(members[key] for key in sorted(members))


def _equal(field: SchemaField, left: RestoreValue, right: RestoreValue) -> bool:
    if _empty(field, left) and _empty(field, right):
        return True
    if (
        field.type == "many2many"
        and isinstance(left, tuple)
        and isinstance(right, tuple)
    ):
        return {_reference_key(member) for member in left} == {
            _reference_key(member) for member in right
        }
    return type(left) is type(right) and left == right
