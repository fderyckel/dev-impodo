"""Run unittest cases in recorded shuffled orders and isolated processes."""

from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
import unittest
from collections.abc import Iterable
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODULES = ("tests.test_integrated_recipe_runs",)
DEFAULT_SEEDS = (1729, 20260826)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def flatten_suite(suite: unittest.TestSuite) -> tuple[unittest.TestCase, ...]:
    """Flatten nested unittest suites without depending on loader order."""

    cases: list[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            cases.extend(flatten_suite(item))
        else:
            cases.append(item)
    return tuple(cases)


def shuffled_ids(test_ids: Iterable[str], seed: int) -> tuple[str, ...]:
    """Return one stable shuffled order from a sorted starting sequence."""

    ordered = sorted(test_ids)
    random.Random(seed).shuffle(ordered)
    return tuple(ordered)


def shuffled_suite(modules: Iterable[str], seed: int) -> unittest.TestSuite:
    loader = unittest.defaultTestLoader
    loaded = loader.loadTestsFromNames(tuple(modules))
    cases = {case.id(): case for case in flatten_suite(loaded)}
    return unittest.TestSuite(cases[test_id] for test_id in shuffled_ids(cases, seed))


def _run_child(modules: tuple[str, ...], seed: int, verbosity: int) -> int:
    print(
        f"Running {len(flatten_suite(unittest.defaultTestLoader.loadTestsFromNames(modules)))} "
        f"tests with shuffle seed {seed} and PYTHONHASHSEED={os.environ.get('PYTHONHASHSEED')}",
        flush=True,
    )
    result = unittest.TextTestRunner(verbosity=verbosity).run(
        shuffled_suite(modules, seed)
    )
    return 0 if result.wasSuccessful() else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", action="append", dest="modules")
    parser.add_argument("--seed", action="append", dest="seeds", type=int)
    parser.add_argument("--verbosity", type=int, default=2)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    modules = tuple(args.modules or DEFAULT_MODULES)
    seeds = tuple(args.seeds or DEFAULT_SEEDS)
    if args.child:
        if len(seeds) != 1:
            parser.error("A child process requires exactly one seed")
        return _run_child(modules, seeds[0], args.verbosity)

    for seed in seeds:
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--child",
            "--seed",
            str(seed),
            "--verbosity",
            str(args.verbosity),
        ]
        for module in modules:
            command.extend(("--module", module))
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = str(seed)
        print(f"Starting isolated unittest process for seed {seed}", flush=True)
        completed = subprocess.run(command, cwd=ROOT, env=environment, check=False)
        if completed.returncode:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
