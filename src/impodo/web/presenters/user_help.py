"""Provide static, version-aware links to Impodo's GitHub user guides."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from types import MappingProxyType
from typing import Final
from urllib.parse import quote

GITHUB_REPOSITORY_URL: Final = "https://github.com/fderyckel/dev-impodo"


class UserHelpScope(StrEnum):
    """Classify where a user guide is discoverable in Impodo."""

    CONTEXTUAL = "CONTEXTUAL"
    GLOBAL = "GLOBAL"
    PRELAUNCH = "PRELAUNCH"


@dataclass(frozen=True, slots=True)
class UserHelpTarget:
    """Describe one reviewed GitHub destination for data-manager help."""

    help_id: str
    title: str
    document_path: str
    scope: UserHelpScope
    description: str
    fragment: str = ""

    def github_url(self, *, documentation_ref: str | None = None) -> str:
        """Build the reviewed GitHub URL without request or workspace data."""

        reference = documentation_ref or installed_documentation_ref()
        encoded_reference = quote(reference, safe="._-")
        encoded_path = quote(self.document_path, safe="/")
        suffix = f"#{quote(self.fragment, safe='-_')}" if self.fragment else ""
        return (
            f"{GITHUB_REPOSITORY_URL}/blob/{encoded_reference}/"
            f"{encoded_path}{suffix}"
        )

    @property
    def url(self) -> str:
        """Return the GitHub URL for the running Impodo build."""

        return self.github_url()

    @property
    def accessible_name(self) -> str:
        """Name the destination and external-tab behavior for assistive tools."""

        return f"Open {self.title} on GitHub in a new tab"


@dataclass(frozen=True, slots=True)
class UserHelpGroup:
    """Group related guides on the authenticated Help page."""

    title: str
    description: str
    target_ids: tuple[str, ...]


def installed_documentation_ref() -> str:
    """Use a matching release tag, or current development documentation."""

    try:
        installed_version = version("impodo")
    except PackageNotFoundError:
        return "main"
    if installed_version == "0.0.0" or ".dev" in installed_version:
        return "main"
    return f"v{installed_version}"


USER_HELP_TARGETS = (
    UserHelpTarget(
        help_id="user-index",
        title="Impodo user documentation",
        document_path="docs/user/README.md",
        scope=UserHelpScope.GLOBAL,
        description="Choose the guide for your current migration task.",
    ),
    UserHelpTarget(
        help_id="getting-started",
        title="Data project and authoring workspace setup",
        document_path="docs/user/getting-started.md",
        scope=UserHelpScope.CONTEXTUAL,
        description="Create a data project and prepare its first Data version.",
    ),
    UserHelpTarget(
        help_id="concepts",
        title="How Impodo organizes your migration",
        document_path="docs/user/concepts.md",
        scope=UserHelpScope.CONTEXTUAL,
        description="Understand Data versions, workspaces, Recipes, and runs.",
    ),
    UserHelpTarget(
        help_id="source-data",
        title="Source data",
        document_path="docs/user/workflow/01-source-data.md",
        scope=UserHelpScope.CONTEXTUAL,
        description="Check source files or freeze the selected Odoo records.",
    ),
    UserHelpTarget(
        help_id="odoo-data",
        title="Odoo data",
        document_path="docs/user/workflow/02-odoo-data.md",
        scope=UserHelpScope.CONTEXTUAL,
        description="Choose record types and how Impodo identifies records.",
    ),
    UserHelpTarget(
        help_id="match-data",
        title="Match data",
        document_path="docs/user/workflow/03-match-data.md",
        scope=UserHelpScope.CONTEXTUAL,
        description="Match source fields, identities, and relationships to Odoo.",
    ),
    UserHelpTarget(
        help_id="prepare-data",
        title="Prepare data",
        document_path="docs/user/workflow/04-prepare-data.md",
        scope=UserHelpScope.CONTEXTUAL,
        description="Apply confirmed rules and resolve data findings.",
    ),
    UserHelpTarget(
        help_id="final-review",
        title="Final review",
        document_path="docs/user/workflow/05-final-review.md",
        scope=UserHelpScope.CONTEXTUAL,
        description="Compare prepared data with Odoo before any write.",
    ),
    UserHelpTarget(
        help_id="load-into-odoo",
        title="Load into Odoo",
        document_path="docs/user/workflow/06-load-into-odoo.md",
        scope=UserHelpScope.CONTEXTUAL,
        description="Confirm, load, recover, and verify the reviewed records.",
    ),
    UserHelpTarget(
        help_id="local-odoo",
        title="Connect to Odoo on this computer",
        document_path="docs/user/guides/local-odoo.md",
        scope=UserHelpScope.GLOBAL,
        description="Connect, reconnect, and understand the Odoo access checks.",
    ),
    UserHelpTarget(
        help_id="related-tables",
        title="Prepare related and combined tables",
        document_path="docs/user/guides/related-tables.md",
        scope=UserHelpScope.CONTEXTUAL,
        description="Join tables, build hierarchies, and split repeated rows.",
    ),
    UserHelpTarget(
        help_id="integrated-test-runs",
        title="Integrated multi-Recipe Test run",
        document_path="docs/user/guides/integrated-test-runs.md",
        scope=UserHelpScope.CONTEXTUAL,
        description="Test several saved Recipe versions with one fresh delivery.",
    ),
    UserHelpTarget(
        help_id="qualify-integrated-test",
        title="Integrated Test qualification",
        document_path="docs/user/guides/qualify-integrated-test.md",
        scope=UserHelpScope.CONTEXTUAL,
        description="Qualify the exact evidence that a Cutover plan preserves.",
    ),
    UserHelpTarget(
        help_id="production-rollout",
        title="Production rollout with latest data",
        document_path="docs/user/guides/production-rollout.md",
        scope=UserHelpScope.CONTEXTUAL,
        description="Apply the qualified plan with fresh Production evidence.",
    ),
    UserHelpTarget(
        help_id="scenario-trials",
        title="Trial your files through Impodo",
        document_path="docs/user/guides/scenario-trials.md",
        scope=UserHelpScope.GLOBAL,
        description="Prepare a bounded scenario trial before broader rollout.",
    ),
    UserHelpTarget(
        help_id="end-to-end-training",
        title="Impodo end-to-end local-browser guide",
        document_path="docs/user/tutorials/end-to-end-training.md",
        scope=UserHelpScope.GLOBAL,
        description="Practice the complete workflow with fictional migration data.",
    ),
    UserHelpTarget(
        help_id="match-data-questions",
        title="Match data: questions and answers",
        document_path="docs/user/tutorials/match-data-questions-and-answers.md",
        scope=UserHelpScope.GLOBAL,
        description="Find direct answers for identities, fields, and relationships.",
    ),
    UserHelpTarget(
        help_id="install-windows",
        title="Install Impodo on Windows",
        document_path="docs/user/installation/windows.md",
        scope=UserHelpScope.PRELAUNCH,
        description="Install or update Impodo on a Windows computer.",
    ),
    UserHelpTarget(
        help_id="install-macos",
        title="Install Impodo on macOS",
        document_path="docs/user/installation/macos.md",
        scope=UserHelpScope.PRELAUNCH,
        description="Install or update Impodo on a macOS computer.",
    ),
)

USER_HELP_BY_ID = MappingProxyType(
    {target.help_id: target for target in USER_HELP_TARGETS}
)

USER_HELP_GROUPS = (
    UserHelpGroup(
        title="Start and understand",
        description="Choose a project structure and learn what Impodo keeps.",
        target_ids=("user-index", "getting-started", "concepts"),
    ),
    UserHelpGroup(
        title="Authoring workflow",
        description="Follow the six stages from source review through verification.",
        target_ids=(
            "source-data",
            "odoo-data",
            "match-data",
            "prepare-data",
            "final-review",
            "load-into-odoo",
        ),
    ),
    UserHelpGroup(
        title="Focused guides",
        description="Open help for connections, related data, Test, and Production.",
        target_ids=(
            "local-odoo",
            "related-tables",
            "integrated-test-runs",
            "qualify-integrated-test",
            "production-rollout",
            "scenario-trials",
        ),
    ),
    UserHelpGroup(
        title="Tutorials",
        description="Practice a complete migration or answer one Match data question.",
        target_ids=("end-to-end-training", "match-data-questions"),
    ),
    UserHelpGroup(
        title="Installation",
        description="Install Impodo on another computer or review setup steps.",
        target_ids=("install-windows", "install-macos"),
    ),
)

PAGE_HELP_ID_BY_TEMPLATE = MappingProxyType(
    {
        "concepts.html": "concepts",
        "project_business_overview.html": "getting-started",
        "project_integrated_qualification.html": "qualify-integrated-test",
        "project_integrated_run.html": "integrated-test-runs",
        "project_list.html": "getting-started",
        "project_new.html": "getting-started",
        "project_production_activation.html": "production-rollout",
        "project_production_run_new.html": "production-rollout",
        "project_recipe_detail.html": "getting-started",
        "project_recipe_odoo_defaults.html": "integrated-test-runs",
        "project_recipe_run_progress.html": "integrated-test-runs",
        "project_recipe_target_matches.html": "integrated-test-runs",
        "project_run_fresh_data.html": "integrated-test-runs",
        "project_test_run_new.html": "integrated-test-runs",
        "mapping/identity_choice_review.html": "match-data",
        "mapping/page.html": "match-data",
        "workspace_correction.html": "load-into-odoo",
        "workspace_correction_progress.html": "load-into-odoo",
        "workspace_datasets.html": "source-data",
        "workspace_deferred_scope.html": "final-review",
        "workspace_derived_entities.html": "related-tables",
        "workspace_destination_matching.html": "load-into-odoo",
        "workspace_files.html": "getting-started",
        "workspace_load.html": "load-into-odoo",
        "workspace_load_progress.html": "load-into-odoo",
        "workspace_normalization.html": "prepare-data",
        "workspace_odoo_capture_progress.html": "source-data",
        "workspace_odoo_capture_selection.html": "source-data",
        "workspace_overview.html": "getting-started",
        "workspace_preflight_progress.html": "final-review",
        "workspace_preparation_progress.html": "prepare-data",
        "workspace_prepare.html": "prepare-data",
        "workspace_quality_cause.html": "final-review",
        "workspace_resolution.html": "prepare-data",
        "workspace_review.html": "getting-started",
        "workspace_schema.html": "odoo-data",
        "workspace_sources.html": "source-data",
        "workspace_summary.html": "final-review",
        "workspace_target.html": "getting-started",
        "workspace_transfer_destination.html": "load-into-odoo",
        "workspace_transfer_load.html": "load-into-odoo",
        "workspace_transfer_load_progress.html": "load-into-odoo",
        "workspace_transfer_order.html": "load-into-odoo",
        "workspace_transfer_preflight.html": "load-into-odoo",
        "workspace_transfer_review.html": "load-into-odoo",
        "workspace_transformation_impact.html": "match-data",
    }
)


def page_help_for_template(template_name: str) -> UserHelpTarget | None:
    """Return static page help without opening project or migration state."""

    help_id = PAGE_HELP_ID_BY_TEMPLATE.get(template_name)
    return USER_HELP_BY_ID[help_id] if help_id is not None else None
