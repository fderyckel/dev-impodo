"""Recognize reported Odoo versions and decide which operations are enabled.

Recognition is separate from support. Odoo 19 preserves its existing operation
set, including development builds. Final Odoo 20 enables only the read and
Recipe-authoring boundaries qualified in Phase 3; writes, recovery, and
Production remain disabled. These transient decisions add no fields to stored
fingerprints or policy hashes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import re


class OdooReleaseStage(StrEnum):
    """Identify the release stage reported by Odoo, without relabelling builds."""

    ALPHA = "alpha"
    BETA = "beta"
    CANDIDATE = "candidate"
    FINAL = "final"


class OdooVersionProblem(StrEnum):
    """Explain why supplied version evidence cannot establish a version."""

    UNKNOWN = "ODOO_VERSION_UNKNOWN"
    MALFORMED = "ODOO_VERSION_MALFORMED"
    CONFLICT = "ODOO_VERSION_CONFLICT"


class OdooOperation(StrEnum):
    """Name the workflow boundary requesting version support."""

    CONNECT = "CONNECT"
    CAPTURE_SCHEMA = "CAPTURE_SCHEMA"
    CAPTURE_SOURCE = "CAPTURE_SOURCE"
    COMPARE = "COMPARE"
    WRITE = "WRITE"
    RECOVER = "RECOVER"
    RECIPE = "RECIPE"
    PRODUCTION = "PRODUCTION"


@dataclass(frozen=True, slots=True)
class OdooVersion:
    """Retain the raw version and its recognized series and release stage."""

    raw: str
    major: int | None = None
    series: str = ""
    release_stage: OdooReleaseStage | None = None
    is_saas: bool = False
    problem: OdooVersionProblem | None = None


@dataclass(frozen=True, slots=True)
class OdooSupportDecision:
    """Return an operation decision without granting transport capabilities."""

    version: OdooVersion
    operation: OdooOperation
    allowed: bool
    reason: str


_VERSION = re.compile(
    r"(?P<saas>saas[~-])?(?P<major>[1-9][0-9]*)\.(?P<minor>[0-9]+)"
    r"(?:(?P<stage>a|b|rc)(?P<serial>[0-9]*))?"
    r"(?P<suffix>(?:[.+-][A-Za-z0-9][A-Za-z0-9.+_-]*)?)"
)
_STAGES = {
    "a": OdooReleaseStage.ALPHA,
    "b": OdooReleaseStage.BETA,
    "rc": OdooReleaseStage.CANDIDATE,
    None: OdooReleaseStage.FINAL,
}
_STAGE_LABELS = {"alpha": "a", "beta": "b", "candidate": "rc", "final": ""}
_MISSING_VERSION_INFO = object()
# Enumerate operations so adding a new operation does not silently enable it.
_ODOO_19_OPERATIONS = frozenset({
    OdooOperation.CONNECT,
    OdooOperation.CAPTURE_SCHEMA,
    OdooOperation.CAPTURE_SOURCE,
    OdooOperation.COMPARE,
    OdooOperation.WRITE,
    OdooOperation.RECOVER,
    OdooOperation.RECIPE,
    OdooOperation.PRODUCTION,
})
_ODOO_20_READ_OPERATIONS = frozenset({
    OdooOperation.CONNECT,
    OdooOperation.CAPTURE_SCHEMA,
    OdooOperation.CAPTURE_SOURCE,
    OdooOperation.COMPARE,
    OdooOperation.RECIPE,
})


def recognize_odoo_version(
    raw: object, *, version_info: object = _MISSING_VERSION_INFO,
) -> OdooVersion:
    """Parse a full version and cross-check structured evidence when supplied.

    Odoo's release tuple is (major, minor, micro, stage, serial, suffix).
    Its version string omits micro. Five-element tuples without a suffix are
    also accepted. An absent tuple permits string-only evidence; a supplied
    null, malformed tuple, or conflicting string cannot fall back silently.
    """

    version = _recognize_string(raw)
    if version_info is _MISSING_VERSION_INFO:
        return version
    structured = _structured_version_string(version_info)
    if structured is None:
        return OdooVersion(version.raw, problem=OdooVersionProblem.MALFORMED)
    if version.problem is not None:
        return version
    if structured != version.raw:
        return OdooVersion(version.raw, problem=OdooVersionProblem.CONFLICT)
    # The validated tuple is authoritative; keep the exact reported string.
    return replace(_recognize_string(structured), raw=version.raw)


def assess_odoo_operation(
    version: str | OdooVersion, operation: OdooOperation,
) -> OdooSupportDecision:
    """Apply the qualified version gate to one named operation.

    The accepted Odoo 19 release forms retain their previous behavior. SaaS
    series, Odoo 20 prereleases, and all other majors remain disabled. Final
    Odoo 20 permits only read and Recipe-authoring operations; write-capable
    operations retain a separate later qualification gate. Model, access,
    schema freshness, and Production checks still apply.
    """

    recognized = version if isinstance(version, OdooVersion) else recognize_odoo_version(version)
    if recognized.problem is not None:
        reason = recognized.problem.value
    elif recognized.is_saas:
        reason = "ODOO_SERIES_DISABLED"
    elif recognized.major == 19:
        if operation not in _ODOO_19_OPERATIONS:
            reason = "ODOO_OPERATION_DISABLED"
        else:
            return OdooSupportDecision(
                recognized,
                operation,
                True,
                "ODOO_19_LEGACY_ACCEPTED",
            )
    elif recognized.major == 20:
        if (
            recognized.series != "20.0"
            or recognized.release_stage is not OdooReleaseStage.FINAL
        ):
            reason = "ODOO_SERIES_DISABLED"
        elif operation not in _ODOO_20_READ_OPERATIONS:
            reason = "ODOO_OPERATION_DISABLED"
        else:
            return OdooSupportDecision(
                recognized,
                operation,
                True,
                "ODOO_20_READ_QUALIFIED",
            )
    else:
        reason = "ODOO_MAJOR_DISABLED"
    return OdooSupportDecision(recognized, operation, False, reason)


def same_odoo_major(left: str, right: str) -> bool:
    """Check a transfer pair's recognized majors, without enabling either end."""

    source = recognize_odoo_version(left)
    target = recognize_odoo_version(right)
    return source.major is not None and source.major == target.major


def _recognize_string(raw: object) -> OdooVersion:
    if raw is None or raw == "" or raw == "unknown":
        return OdooVersion(raw if isinstance(raw, str) else "", problem=OdooVersionProblem.UNKNOWN)
    match = _VERSION.fullmatch(raw) if isinstance(raw, str) and len(raw) <= 256 else None
    if match is None:
        return OdooVersion(str(raw), problem=OdooVersionProblem.MALFORMED)
    return OdooVersion(
        raw=raw,
        major=int(match["major"]),
        series=f"{match['saas'] or ''}{match['major']}.{match['minor']}",
        release_stage=_STAGES[match["stage"]],
        is_saas=bool(match["saas"]),
    )


def _structured_version_string(info: object) -> str | None:
    if not isinstance(info, (tuple, list)) or len(info) not in {5, 6}:
        return None
    major, minor, micro, stage, serial = info[:5]
    suffix = info[5] if len(info) == 6 else ""
    # Enterprise endpoints can report the edition as bare "e" in the tuple
    # while the full version uses "+e". Normalize only that known edition.
    if suffix == "e":
        suffix = "+e"
    if not (
        (type(major) is int and major > 0)
        or (isinstance(major, str) and re.fullmatch(r"saas[~-][1-9][0-9]*", major))
    ):
        return None
    if any(type(value) is not int or value < 0 for value in (minor, micro, serial)):
        return None
    if not isinstance(stage, str) or stage not in _STAGE_LABELS or not isinstance(suffix, str):
        return None
    if stage == "final" and serial != 0:
        return None
    if suffix and re.fullmatch(r"[.+-][A-Za-z0-9][A-Za-z0-9.+_-]*", suffix) is None:
        return None
    rendered = f"{major}.{minor}{_STAGE_LABELS[stage]}{serial or ''}{suffix}"
    parsed = _recognize_string(rendered)
    if parsed.problem is not None or parsed.release_stage.value != stage:
        return None
    return rendered
