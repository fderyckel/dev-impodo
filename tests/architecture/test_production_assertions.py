"""Keep removable Python assertions out of production behavior."""

from __future__ import annotations

import ast
import unittest

from tests.support.paths import REPOSITORY_ROOT

PRODUCTION_ROOT = REPOSITORY_ROOT / "src" / "impodo"


class ProductionAssertionTests(unittest.TestCase):
    def test_production_code_uses_explicit_runtime_contracts(self) -> None:
        assertions = []
        for path in sorted(PRODUCTION_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            assertions.extend(
                f"{path.relative_to(REPOSITORY_ROOT).as_posix()}:{node.lineno}"
                for node in ast.walk(tree)
                if isinstance(node, ast.Assert)
            )
        self.assertEqual(
            assertions,
            [],
            "Production invariants must remain active under python -O:\n"
            + "\n".join(assertions),
        )


if __name__ == "__main__":
    unittest.main()
