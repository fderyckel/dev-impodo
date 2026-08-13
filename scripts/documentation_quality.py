"""Validate Impodo workflow documentation coverage and repository references.

The workflow manifest is a documentation registry, not a runtime contract.
This tool checks objective drift: audience pairs, required sections, local
links and anchors, images, code symbols, tests, route ownership, template
ownership, and the implemented file-source navigation labels. Prose quality
still requires human review and the advisory Vale rules.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable, Mapping, Sequence
from urllib.parse import unquote

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = Path("docs/workflow.yml")
LINK_RE = re.compile(
    r"(?P<image>!)?\[(?P<label>[^\]]*)\]\(\s*(?P<target><[^>]+>|[^)\s]+)",
    re.MULTILINE,
)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

USER_HEADINGS = (
    "Goal",
    "Before you start",
    "Steps in Impodo",
    "What to check",
    "What Complete means",
    "What changes and what does not",
    "Needs attention",
    "What makes this work stale",
    "Next stage",
    "Related documentation",
)
DEVELOPER_HEADINGS = (
    "Responsibility",
    "Entry conditions",
    "Implementation flow",
    "Code references",
    "Evidence and state",
    "Completion and navigation",
    "Invalidation and recovery",
    "Odoo 19 and performance",
    "Verification",
    "Related documentation",
)


@dataclass(frozen=True, slots=True, order=True)
class DocumentationIssue:
    """One deterministic documentation validation failure."""

    path: str
    message: str

    def render(self) -> str:
        """Return one concise command-line diagnostic."""

        return f"{self.path}: {self.message}"


def load_manifest(
    repo_root: Path = ROOT,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> Mapping[str, object]:
    """Load the workflow registry from the repository."""

    path = repo_root / manifest_path
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Documentation workflow manifest must be a mapping")
    return payload


def _front_matter(text: str) -> Mapping[str, object]:
    if not text.startswith("---\n"):
        return {}
    boundary = text.find("\n---\n", 4)
    if boundary < 0:
        return {}
    payload = yaml.safe_load(text[4:boundary])
    return payload if isinstance(payload, dict) else {}


def _markdown_slug(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("`", "").strip().lower()
    return "".join(
        character
        for character in value.replace(" ", "-")
        if character.isalnum() or character in {"-", "_"}
    )


def _heading_anchors(path: Path) -> frozenset[str]:
    text = path.read_text(encoding="utf-8")
    counts: Counter[str] = Counter()
    anchors: set[str] = set()
    for _marks, heading in HEADING_RE.findall(text):
        base = _markdown_slug(heading)
        suffix = counts[base]
        counts[base] += 1
        anchors.add(base if suffix == 0 else f"{base}-{suffix}")
    return frozenset(anchors)


def _resolved_links(path: Path) -> tuple[Path, ...]:
    resolved: list[Path] = []
    for match in LINK_RE.finditer(path.read_text(encoding="utf-8")):
        target = match.group("target").strip("<>")
        if SCHEME_RE.match(target) or target.startswith("#"):
            continue
        relative, _separator, _anchor = target.partition("#")
        if relative:
            resolved.append((path.parent / unquote(relative)).resolve())
    return tuple(resolved)


def _validate_links(repo_root: Path) -> list[DocumentationIssue]:
    issues: list[DocumentationIssue] = []
    paths = [repo_root / "README.md", *(repo_root / "docs").rglob("*.md")]
    for path in sorted(path for path in paths if path.is_file()):
        relative_path = path.relative_to(repo_root).as_posix()
        text = path.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            target = match.group("target").strip("<>")
            if match.group("image") and not match.group("label").strip():
                issues.append(
                    DocumentationIssue(relative_path, f"image {target!r} has no alt text")
                )
            if SCHEME_RE.match(target):
                continue
            relative, _separator, anchor = target.partition("#")
            target_path = path if not relative else path.parent / unquote(relative)
            target_path = target_path.resolve()
            if not target_path.exists():
                issues.append(
                    DocumentationIssue(relative_path, f"missing local link target {target!r}")
                )
                continue
            if anchor and target_path.suffix.lower() == ".md":
                decoded_anchor = unquote(anchor).lower()
                if decoded_anchor not in _heading_anchors(target_path):
                    issues.append(
                        DocumentationIssue(
                            relative_path,
                            f"missing Markdown anchor {anchor!r} in "
                            f"{target_path.relative_to(repo_root).as_posix()}",
                        )
                    )
    return issues


def _symbol_exists(path: Path, symbol: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parts = symbol.split(".")
    nodes: Iterable[ast.AST] = tree.body
    current: ast.AST | None = None
    for part in parts:
        current = next(
            (
                node
                for node in nodes
                if isinstance(
                    node,
                    (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
                )
                and node.name == part
            ),
            None,
        )
        if current is None:
            return False
        nodes = current.body if isinstance(current, ast.ClassDef) else ()
    return current is not None


def resolve_code_reference(repo_root: Path, reference: str) -> bool:
    """Return whether ``path::qualified.symbol`` resolves in current Python."""

    path_text, separator, symbol = reference.partition("::")
    if not separator or not symbol:
        return False
    path = repo_root / path_text
    return path.is_file() and path.suffix == ".py" and _symbol_exists(path, symbol)


def _owned_paths(
    stages: Sequence[Mapping[str, object]],
    key: str,
) -> Mapping[str, tuple[str, ...]]:
    owners: defaultdict[str, list[str]] = defaultdict(list)
    for stage in stages:
        for value in stage.get(key, []):
            owners[str(value)].append(str(stage.get("id", "")))
    return {path: tuple(values) for path, values in owners.items()}


def _implemented_navigation(repo_root: Path) -> tuple[tuple[str, int, str], ...]:
    path = repo_root / "src/impodo/web/presenters/navigation.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != "_locked_stages":
            continue
        for child in node.body:
            if not isinstance(child, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == "definitions"
                for target in child.targets
            ):
                continue
            values = ast.literal_eval(child.value)
            return tuple((str(item[0]), int(item[1]), str(item[2])) for item in values)
    raise ValueError("Could not read canonical navigation definitions")


def _route_count(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"get", "post"}:
            continue
        if isinstance(node.func.value, ast.Name) and node.func.value.id == "router":
            count += 1
    return count


def _validate_stage(
    repo_root: Path,
    stage: Mapping[str, object],
) -> list[DocumentationIssue]:
    issues: list[DocumentationIssue] = []
    stage_id = str(stage.get("id", ""))
    label = str(stage.get("label", ""))
    for audience, key, headings in (
        ("user", "user_doc", USER_HEADINGS),
        ("developer", "developer_doc", DEVELOPER_HEADINGS),
    ):
        relative = str(stage.get(key, ""))
        path = repo_root / relative
        if not path.is_file():
            issues.append(DocumentationIssue(relative or "docs/workflow.yml", "missing stage document"))
            continue
        text = path.read_text(encoding="utf-8")
        metadata = _front_matter(text)
        if metadata.get("audience") != audience:
            issues.append(DocumentationIssue(relative, f"audience must be {audience!r}"))
        if metadata.get("stage") != stage_id:
            issues.append(DocumentationIssue(relative, f"stage must be {stage_id!r}"))
        if metadata.get("status") != "current":
            issues.append(DocumentationIssue(relative, "status must be 'current'"))
        if f"# {label}\n" not in text:
            issues.append(DocumentationIssue(relative, f"H1 must be {label!r}"))
        present = {heading for _marks, heading in HEADING_RE.findall(text)}
        for heading in headings:
            if heading not in present:
                issues.append(DocumentationIssue(relative, f"missing required heading {heading!r}"))

    user_path = (repo_root / str(stage.get("user_doc", ""))).resolve()
    developer_path = (repo_root / str(stage.get("developer_doc", ""))).resolve()
    if user_path.is_file() and developer_path not in _resolved_links(user_path):
        issues.append(
            DocumentationIssue(
                user_path.relative_to(repo_root).as_posix(),
                "missing link to paired developer document",
            )
        )
    if developer_path.is_file() and user_path not in _resolved_links(developer_path):
        issues.append(
            DocumentationIssue(
                developer_path.relative_to(repo_root).as_posix(),
                "missing link to paired user document",
            )
        )

    if developer_path.is_file():
        developer_links = set(_resolved_links(developer_path))
        registered_references = {
            str(reference).partition("::")[0]
            for reference in stage.get("code_references", [])
        }
        registered_references.update(str(value) for value in stage.get("tests", []))
        registered_references.update(
            str(value) for value in stage.get("contracts", [])
        )
        registered_references.update(
            str(value) for value in stage.get("references", [])
        )
        for relative in sorted(registered_references):
            if (repo_root / relative).resolve() not in developer_links:
                issues.append(
                    DocumentationIssue(
                        developer_path.relative_to(repo_root).as_posix(),
                        f"missing link to registered reference {relative!r}",
                    )
                )

    for reference in stage.get("code_references", []):
        if not resolve_code_reference(repo_root, str(reference)):
            issues.append(
                DocumentationIssue(
                    "docs/workflow.yml",
                    f"stage {stage_id!r} has unresolved code reference {reference!r}",
                )
            )
    for key in (
        "router_modules",
        "templates",
        "tests",
        "contracts",
        "references",
    ):
        for value in stage.get(key, []):
            if not (repo_root / str(value)).is_file():
                issues.append(
                    DocumentationIssue(
                        "docs/workflow.yml",
                        f"stage {stage_id!r} has missing {key} entry {value!r}",
                    )
                )
    return issues


def validate_repository(
    repo_root: Path = ROOT,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> tuple[DocumentationIssue, ...]:
    """Return all deterministic workflow-documentation failures."""

    manifest = load_manifest(repo_root, manifest_path)
    issues: list[DocumentationIssue] = []
    if manifest.get("schema_version") != 1:
        issues.append(DocumentationIssue(str(manifest_path), "schema_version must be 1"))

    raw_stages = manifest.get("stages")
    if not isinstance(raw_stages, list):
        return (DocumentationIssue(str(manifest_path), "stages must be a list"),)
    stages = [stage for stage in raw_stages if isinstance(stage, dict)]
    if len(stages) != len(raw_stages):
        issues.append(DocumentationIssue(str(manifest_path), "every stage must be a mapping"))

    ids = [str(stage.get("id", "")) for stage in stages]
    orders = [int(stage.get("order", -1)) for stage in stages]
    for value, count in Counter(ids).items():
        if not value or count != 1:
            issues.append(DocumentationIssue(str(manifest_path), f"invalid or duplicate stage id {value!r}"))
    for value, count in Counter(orders).items():
        if value < 0 or count != 1:
            issues.append(DocumentationIssue(str(manifest_path), f"invalid or duplicate stage order {value!r}"))

    for stage in stages:
        issues.extend(_validate_stage(repo_root, stage))

    navigation = tuple(
        (str(stage["id"]), int(stage["order"]), str(stage["label"]))
        for stage in sorted(stages, key=lambda item: int(item["order"]))
        if stage.get("navigation") is True
    )
    try:
        implemented = _implemented_navigation(repo_root)
    except (OSError, SyntaxError, TypeError, ValueError) as error:
        issues.append(DocumentationIssue("src/impodo/web/presenters/navigation.py", str(error)))
    else:
        if navigation != implemented:
            issues.append(
                DocumentationIssue(
                    str(manifest_path),
                    f"canonical navigation {navigation!r} does not match code {implemented!r}",
                )
            )

    navigation_source = (
        repo_root / "src/impodo/web/presenters/navigation.py"
    ).read_text(encoding="utf-8")
    for stage in stages:
        variants = stage.get("variants", {})
        if not isinstance(variants, dict):
            issues.append(
                DocumentationIssue(
                    str(manifest_path),
                    f"stage {stage.get('id')!r} variants must be a mapping",
                )
            )
            continue
        for variant_id, variant in variants.items():
            if not isinstance(variant, dict):
                issues.append(
                    DocumentationIssue(
                        str(manifest_path),
                        f"variant {variant_id!r} must be a mapping",
                    )
                )
                continue
            label = variant.get("label")
            order = variant.get("order")
            if not isinstance(label, str) or not label:
                issues.append(
                    DocumentationIssue(
                        str(manifest_path),
                        f"variant {variant_id!r} requires a label",
                    )
                )
            elif f'"{label}"' not in navigation_source:
                issues.append(
                    DocumentationIssue(
                        str(manifest_path),
                        f"variant label {label!r} is absent from navigation code",
                    )
                )
            if not isinstance(order, int) or order < 1:
                issues.append(
                    DocumentationIssue(
                        str(manifest_path),
                        f"variant {variant_id!r} requires a positive order",
                    )
                )

    router_owners = dict(_owned_paths(stages, "router_modules"))
    shared = manifest.get("shared", {})
    if isinstance(shared, dict):
        for path in shared.get("router_modules", []):
            router_owners.setdefault(str(path), ("shared",))
        for path in shared.get("documents", []):
            if not (repo_root / str(path)).is_file():
                issues.append(DocumentationIssue(str(manifest_path), f"missing shared document {path!r}"))
    actual_routers = {
        path.relative_to(repo_root).as_posix()
        for path in (repo_root / "src/impodo/web/routers").glob("*.py")
        if path.name != "__init__.py"
    }
    for path in sorted(actual_routers | set(router_owners)):
        owners = router_owners.get(path, ())
        if path not in actual_routers:
            issues.append(DocumentationIssue(str(manifest_path), f"registered router does not exist: {path}"))
        elif len(owners) != 1:
            issues.append(DocumentationIssue(str(manifest_path), f"router must have one owner: {path} -> {owners!r}"))

    template_owners = _owned_paths(stages, "templates")
    actual_templates = {
        path.relative_to(repo_root).as_posix()
        for path in (repo_root / "src/impodo/web/templates").glob("project_*.html")
    }
    for path in sorted(actual_templates | set(template_owners)):
        owners = template_owners.get(path, ())
        if path not in actual_templates:
            issues.append(DocumentationIssue(str(manifest_path), f"registered template does not exist: {path}"))
        elif len(owners) != 1:
            issues.append(DocumentationIssue(str(manifest_path), f"template must have one owner: {path} -> {owners!r}"))

    issues.extend(_validate_links(repo_root))
    return tuple(sorted(set(issues)))


def render_report(
    repo_root: Path = ROOT,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> str:
    """Render deterministic workflow coverage as a Markdown table."""

    manifest = load_manifest(repo_root, manifest_path)
    stages = manifest.get("stages", [])
    lines = [
        "| Stage | User | Developer | Routers | Routes | Templates | Symbols | Tests |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for stage in sorted(stages, key=lambda item: int(item["order"])):
        router_paths = [repo_root / str(path) for path in stage.get("router_modules", [])]
        route_count = sum(_route_count(path) for path in router_paths if path.is_file())
        lines.append(
            f"| {stage['label']} | yes | yes | {len(router_paths)} | {route_count} | "
            f"{len(stage.get('templates', []))} | {len(stage.get('code_references', []))} | "
            f"{len(stage.get('tests', []))} |"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """Build the documentation-quality command line."""

    parser = argparse.ArgumentParser(
        description="Validate Impodo workflow documentation coverage."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=ROOT,
        help="repository root (default: current Impodo checkout)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="workflow manifest relative to the repository root",
    )
    parser.add_argument("--check", action="store_true", help="fail on validation issues")
    parser.add_argument("--report", action="store_true", help="print the coverage table")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the report and optional blocking validation."""

    arguments = build_parser().parse_args(argv)
    repo_root = arguments.repo_root.resolve()
    if arguments.report or not arguments.check:
        print(render_report(repo_root, arguments.manifest))
    issues = validate_repository(repo_root, arguments.manifest)
    if issues:
        print()
        print("Documentation issues:")
        for issue in issues:
            print(f"- {issue.render()}")
    return 1 if arguments.check and issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
