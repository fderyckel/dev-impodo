"""Review changed production code for hidden coupling and advisory size growth.

The Git comparison is the review boundary, not a saved exception baseline.
Existing findings stay visible; new private imports and obvious runtime
replacement of imported objects or declared methods fail the check. Static
inspection deliberately does not claim to discover every dynamic mutation.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_PREFIX = "src/impodo/"
MODULE_LINE_LIMIT = 500
FUNCTION_LINE_LIMIT = 80
COMPLEXITY_LIMIT = 15


@dataclass(frozen=True, order=True)
class Finding:
    """Identify a source-level coupling finding independently of line movement."""

    rule: str
    path: str
    target: str
    detail: str
    line: int

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return self.rule, self.path, self.target, self.detail

    def describe(self) -> str:
        detail = (
            self.detail
            if self.rule == "private-import"
            else "requires an explicit collaborator"
        )
        return f"{self.path}:{self.line}: {self.rule}: {self.target} {detail}"


@dataclass(frozen=True)
class Advisory:
    """Describe growth above a review threshold without treating size as a defect."""

    path: str
    symbol: str
    metric: str
    previous: int
    current: int
    limit: int

    def describe(self) -> str:
        return (
            f"{self.path}: {self.symbol} {self.metric} "
            f"{self.previous} -> {self.current} (review threshold {self.limit})"
        )


@dataclass(frozen=True)
class Review:
    """Keep hard new findings separate from existing coupling and size advice."""

    findings: tuple[Finding, ...]
    introduced: tuple[Finding, ...]
    advisories: tuple[Advisory, ...]


def _module_name(path: str) -> tuple[str, str]:
    name = path.removeprefix("src/").removesuffix(".py").replace("/", ".")
    is_package = name.endswith(".__init__")
    name = name.removesuffix(".__init__")
    return name, name if is_package else name.rpartition(".")[0]


def _imported_names(tree: ast.Module, package: str) -> dict[str, str]:
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            module = (
                importlib.util.resolve_name(
                    "." * node.level + (node.module or ""), package
                )
                if node.level
                else node.module or ""
            )
            for alias in node.names:
                names[alias.asname or alias.name] = f"{module}.{alias.name}"
    return names


class _CouplingVisitor(ast.NodeVisitor):
    def __init__(self, path: str, tree: ast.Module) -> None:
        self.path = path
        self.module, self.package = _module_name(path)
        self.imports = _imported_names(tree, self.package)
        self.classes = {
            node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        }
        self.methods: list[set[str]] = []
        self.findings: list[Finding] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if any(
                part.startswith("_") and not part.startswith("__")
                for part in alias.name.split(".")
            ):
                self.findings.append(
                    Finding(
                        "private-import",
                        self.path,
                        alias.name,
                        "private module",
                        node.lineno,
                    )
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = (
            importlib.util.resolve_name(
                "." * node.level + (node.module or ""), self.package
            )
            if node.level
            else node.module or ""
        )
        for alias in node.names:
            if (
                alias.name.startswith("_")
                and not alias.name.startswith("__")
                or any(
                    part.startswith("_") and not part.startswith("__")
                    for part in module.split(".")
                )
            ):
                self.findings.append(
                    Finding(
                        "private-import", self.path, module, alias.name, node.lineno
                    )
                )
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.methods.append(
            {
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
        )
        self.generic_visit(node)
        self.methods.pop()

    def _replacement_target(self, target: ast.expr) -> str | None:
        if not isinstance(target, ast.Attribute):
            return None
        base = target.value
        while isinstance(base, ast.Attribute):
            base = base.value
        if not isinstance(base, ast.Name):
            return None
        if base.id in self.imports:
            return ast.unparse(target).replace(base.id, self.imports[base.id], 1)
        if base.id in self.classes:
            return ast.unparse(target)
        if (
            base.id in {"self", "cls"}
            and self.methods
            and isinstance(target.value, ast.Name)
            and target.attr in self.methods[-1]
        ):
            return ast.unparse(target)
        return None

    def _record_replacement(self, target: ast.expr, value: ast.expr | None) -> None:
        name = self._replacement_target(target)
        if name is not None:
            self.findings.append(
                Finding(
                    "runtime-replacement",
                    self.path,
                    name,
                    ast.dump(value, include_attributes=False) if value else "delete",
                    target.lineno,
                )
            )

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._record_replacement(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._record_replacement(node.target, node.value)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._record_replacement(node.target, node.value)
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._record_replacement(target, None)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = ast.unparse(node.func)
        root = name.split(".")[0]
        if root in self.imports:
            name = name.replace(root, self.imports[root], 1)
        if name in {
            "unittest.mock.patch",
            "unittest.mock.patch.object",
            "unittest.mock.patch.multiple",
            "unittest.mock.patch.dict",
        }:
            self.findings.append(
                Finding(
                    "runtime-replacement",
                    self.path,
                    name,
                    ast.dump(node, include_attributes=False),
                    node.lineno,
                )
            )
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in {"setattr", "delattr"}
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            target = ast.Attribute(
                value=node.args[0], attr=node.args[1].value, ctx=ast.Store()
            )
            target.lineno = node.lineno
            self._record_replacement(
                target, node.args[2] if len(node.args) > 2 else None
            )
        self.generic_visit(node)


def coupling_findings(path: str, source: str) -> tuple[Finding, ...]:
    """Find direct private imports and inspectable class/import mutations."""
    tree = ast.parse(source, filename=path)
    visitor = _CouplingVisitor(path, tree)
    visitor.visit(tree)
    return tuple(sorted(visitor.findings))


class _ComplexityVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.value = 1

    def visit(self, node: ast.AST) -> None:
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            return
        if isinstance(
            node,
            (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler),
        ):
            self.value += 1
        elif isinstance(node, ast.BoolOp):
            self.value += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            self.value += 1 + len(node.ifs)
        elif isinstance(node, ast.match_case):
            wildcard = (
                isinstance(node.pattern, ast.MatchAs) and node.pattern.pattern is None
            )
            self.value += not wildcard
            self.value += node.guard is not None
        super().visit(node)


def _metrics(source: str) -> dict[tuple[str, str], int]:
    metrics = {("module", "physical lines"): len(source.splitlines())}

    def visit(nodes: Sequence[ast.stmt], parent: str = "") -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{parent}.{node.name}" if parent else node.name
                if not isinstance(node, ast.ClassDef):
                    start = min(
                        [node.lineno, *(item.lineno for item in node.decorator_list)]
                    )
                    metrics[name, "physical lines"] = (
                        (node.end_lineno or node.lineno) - start + 1
                    )
                    complexity = _ComplexityVisitor()
                    for statement in node.body:
                        complexity.visit(statement)
                    metrics[name, "cyclomatic complexity"] = complexity.value
                visit(node.body, name)
            else:
                for field in ("body", "orelse", "finalbody"):
                    body = getattr(node, field, ())
                    if isinstance(body, list):
                        visit(body, parent)
                if isinstance(node, (ast.Try, ast.TryStar)):
                    for handler in node.handlers:
                        visit(handler.body, parent)

    visit(ast.parse(source).body)
    return metrics


def growth_advisories(path: str, before: str, after: str) -> tuple[Advisory, ...]:
    """Report threshold crossings and growth in already oversized changed code."""
    old = _metrics(before)
    advisories: list[Advisory] = []
    for (symbol, metric), current in _metrics(after).items():
        previous = old.get((symbol, metric), 0)
        limit = (
            COMPLEXITY_LIMIT
            if metric == "cyclomatic complexity"
            else MODULE_LINE_LIMIT
            if symbol == "module"
            else FUNCTION_LINE_LIMIT
        )
        if current > limit and current > previous:
            advisories.append(Advisory(path, symbol, metric, previous, current, limit))
    return tuple(advisories)


def review_sources(before: Mapping[str, str], after: Mapping[str, str]) -> Review:
    """Compare source pairs without saving or suppressing historical findings."""
    findings: list[Finding] = []
    introduced: list[Finding] = []
    advisories: list[Advisory] = []
    for path, source in sorted(after.items()):
        current = coupling_findings(path, source)
        findings.extend(current)
        previous = before.get(path, "")
        if previous == source:
            continue
        known = {item.identity for item in coupling_findings(path, previous)}
        introduced.extend(item for item in current if item.identity not in known)
        advisories.extend(growth_advisories(path, previous, source))
    return Review(tuple(findings), tuple(introduced), tuple(advisories))


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments), cwd=root, text=True, capture_output=True, check=False
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Git review command failed")
    return result.stdout


def review_base(root: Path, requested: str | None = None) -> str:
    """Resolve one fixed comparison commit, including manually dispatched CI."""
    selected = requested or os.environ.get("IMPODO_REVIEW_BASE")
    if not selected and (branch := os.environ.get("GITHUB_BASE_REF")):
        selected = _git(root, "merge-base", f"origin/{branch}", "HEAD").strip()
    previous = os.environ.get("IMPODO_CI_BEFORE", "")
    if not selected and previous and set(previous) != {"0"}:
        selected = previous
    if not selected:
        selected = "HEAD^" if os.environ.get("GITHUB_ACTIONS") == "true" else "HEAD"
    return _git(
        root, "rev-parse", "--verify", "--end-of-options", f"{selected}^{{commit}}"
    ).strip()


def repository_review(root: Path, base: str) -> Review:
    """Read production sources and only fetch historical files changed since base."""
    after = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted((root / "src" / "impodo").rglob("*.py"))
    }
    changed = set(
        _git(root, "diff", "--name-only", "-z", base, "--", "src/impodo").split("\0")
    )
    changed.update(
        _git(
            root, "ls-files", "--others", "--exclude-standard", "-z", "src/impodo"
        ).split("\0")
    )
    historical_paths = set(
        _git(
            root, "ls-tree", "-r", "--name-only", "-z", base, "--", "src/impodo"
        ).split("\0")
    )
    before = {
        path: (_git(root, "show", f"{base}:{path}") if path in historical_paths else "")
        if path in changed
        else source
        for path, source in after.items()
    }
    return review_sources(before, after)


def main(argv: Sequence[str] | None = None) -> int:
    """Fail on new coupling findings while printing nonblocking review advice."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="fixed Git review base (or IMPODO_REVIEW_BASE)")
    parser.add_argument(
        "--all-findings", action="store_true", help="list existing findings too"
    )
    arguments = parser.parse_args(argv)
    try:
        base = review_base(ROOT, arguments.base)
        result = repository_review(ROOT, base)
    except (OSError, SyntaxError, ValueError) as error:
        print(f"Maintainability review unavailable: {error}", file=sys.stderr)
        return 2
    print(f"Maintainability review base: {base}")
    print(
        f"Existing/current findings: {len(result.findings)}; introduced: {len(result.introduced)}"
    )
    for finding in result.findings if arguments.all_findings else result.introduced:
        print(finding.describe())
    for advisory in result.advisories:
        print(f"Advisory: {advisory.describe()}")
    return int(bool(result.introduced))


if __name__ == "__main__":
    raise SystemExit(main())
