"""Apply captured Odoo company compatibility without reading source rows again.

See docs/developer/contracts/preflight.md.
"""

from collections.abc import Sequence
from dataclasses import replace

from impodo.domain.compiler.contracts import CompiledMigrationPlan
from impodo.domain.data_version.catalog import TargetCatalog
from impodo.domain.odoo.company import CompanyReferenceCheck
from impodo.domain.shared.models import (
    Issue,
    LogicalReference,
    PreparedRecord,
    Severity,
)


def check_company_references(
    records: Sequence[PreparedRecord],
    plan: CompiledMigrationPlan,
    catalog: TargetCatalog,
    checks: tuple[CompanyReferenceCheck, ...],
) -> tuple[PreparedRecord, ...]:
    """Block only references that a complete authoritative domain check rejected."""
    if not checks:
        return tuple(records)
    indexed = {
        (check.parent_model, check.field): (
            check.relation_model,
            set(check.incompatible_ids),
        )
        for check in checks
    }
    result = []
    for record in records:
        issues = list(record.issues)
        for name, reference in record.references.items():
            evidence = indexed.get((record.target_model, name))
            rule = plan.dataset(record.dataset).relations.get(name)
            if (
                evidence is None
                or rule is None
                or rule.validate_only
                or not isinstance(reference, LogicalReference)
                or reference.origin not in {"target", "target_then_incoming"}
            ):
                continue
            relation_model, incompatible = evidence
            if reference.model != relation_model or not incompatible:
                continue
            matches = catalog.find_by_fields(
                str(reference.model),
                (*reference.target_fields, *reference.target_scope_fields),
                (*reference.key, *reference.scope),
            )
            if len(matches) == 1 and matches[0].odoo_id in incompatible:
                issues.append(
                    Issue(
                        code="REFERENCE_COMPANY_MISMATCH",
                        message="Odoo does not allow this linked record for the company used for this import. Review the company and linked value, then compare again.",
                        severity=Severity.ERROR,
                        dataset=record.dataset,
                        row=record.source_row,
                        field=name,
                    )
                )
        result.append(replace(record, issues=tuple(issues)))
    return tuple(result)
