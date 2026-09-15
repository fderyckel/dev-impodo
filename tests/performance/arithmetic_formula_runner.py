"""Measure exact arithmetic parity and warm transformation cost on fictional rows.

Run with ``python -m tests.performance.arithmetic_formula_runner``. This
diagnostic measures formula work and in-memory browser row transformation;
it does not qualify complete preparation time, worker memory, or row limits.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import statistics
from time import perf_counter
from unittest.mock import patch

from impodo.domain.recipe import value_rules
from impodo.domain.staging import evaluator
from impodo.domain.shared.models import canonical_json_bytes, portable_value
from tests.domain.recipe.test_arithmetic_formula import _oracle
from tests.domain.preparation.test_browser_arithmetic_formula import _fixture, _prepare


def measure_pair(before, after, repetitions=5):
    """Alternate route order and require exact evidence equality."""

    samples = {"before": [], "after": []}
    results = {}
    for _ in range(repetitions):
        order = (("before", before), ("after", after))
        if _ % 2:
            order = tuple(reversed(order))
        for label, calculate in order:
            started = perf_counter()
            results[label] = calculate()
            samples[label].append(perf_counter() - started)
    assert results["before"] == results["after"]
    return results["after"], samples["before"], samples["after"]


def no_arithmetic(*args, **kwargs):
    return None


def run():
    """Emit a local diagnostic without creating or opening any project store."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    measurements = []
    for width in (0, 60):
        definition, selection, templates = _fixture(width=width)
        rows = tuple(replace(templates[i % 3], number=i + 2) for i in range(3000))
        formula = definition.datasets[0].fields[0].transform.formula
        context = {"value": "2.95", "column_2": "2.95", "column_3": "1", **{f"unused_{i}": "100" for i in range(width)}}
        program = value_rules.compile_arithmetic_formula(formula, allowed_names=set(context))
        assert str(program.evaluate(context)) == str(_oracle(formula, context))

        def old_formula():
            for _ in range(20000):
                result = _oracle(formula, context)
            return str(result)

        def compiled_formula():
            for _ in range(20000):
                result = program.evaluate(context)
            return str(result)

        _, old_samples, compiled_samples = measure_pair(old_formula, compiled_formula)

        def old_browser():
            with (
                patch.object(value_rules, "evaluate_formula", new=_oracle),
                patch.object(evaluator, "compile_arithmetic_formula", new=no_arithmetic),
            ):
                return _prepare(definition, selection, rows)

        compiled_records, old_browser_samples, compiled_browser_samples = measure_pair(
            old_browser, lambda: _prepare(definition, selection, rows),
        )
        record_hash = hashlib.sha256(canonical_json_bytes(portable_value({
            "records": [asdict(record) for record in compiled_records[0]],
            "impacts": asdict(compiled_records[1]),
        }))).hexdigest()
        for stage, count, before, after in (
            ("formula_only", 20000, old_samples, compiled_samples),
            ("browser_row_transformation", len(rows), old_browser_samples, compiled_browser_samples),
        ):
            old_median, new_median = statistics.median(before), statistics.median(after)
            measurements.append({
                "stage": stage, "rows": count, "unused_source_columns": width,
                "repetitions": len(before), "baseline_seconds": before,
                "compiled_seconds": after, "baseline_median_seconds": old_median,
                "compiled_median_seconds": new_median,
                "median_reduction_percent": 100 * (old_median - new_median) / old_median,
                "exact_record_and_impact_parity": True,
                "record_and_impact_hash": record_hash,
            })
    report = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(), "platform": platform.platform(),
        "route": "bounded_python", "measurements": measurements,
        "scope": "Warm formula and in-memory browser row transformation only; excludes project publication, quality, normalization and worker startup. No peak memory qualification.",
    }
    if arguments.output is not None:
        arguments.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    run()
