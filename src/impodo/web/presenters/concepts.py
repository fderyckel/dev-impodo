"""Provide one reviewed source for data-manager concept explanations."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True, slots=True)
class ConceptHelp:
    """Describe one browser concept without depending on saved project state."""

    slug: str
    term: str
    title: str
    definition: str
    relationship: str
    exclusion: str
    example: str
    practical_effect: str
    related_slugs: tuple[str, ...]


CONCEPTS = (
    ConceptHelp(
        slug="data-project",
        term="Data project",
        title="How a data project is organized",
        definition=(
            "A data project keeps one migration effort together. It gives you "
            "one place to govern the source deliveries, workspaces, optional "
            "Recipes, and migration runs for that effort."
        ),
        relationship=(
            "The data project contains its data versions and workspaces. It can "
            "also contain Recipes when you decide that the preparation rules "
            "will be useful again."
        ),
        exclusion=(
            "Creating a data project does not inspect Odoo, write to Odoo, or "
            "require you to save a Recipe."
        ),
        example=(
            "A Customer migration project can contain a sample delivery for "
            "authoring, a later Test delivery, and the final Production delivery."
        ),
        practical_effect=(
            "Open one existing data project when you want to continue that "
            "migration effort. Create another project only for a separate effort."
        ),
        related_slugs=("data-version", "workspace", "recipe", "test-run"),
    ),
    ConceptHelp(
        slug="data-version",
        term="Data version",
        title="What one data version represents",
        definition=(
            "A data version is one complete delivery of source data that Impodo "
            "accepts and keeps unchanged."
        ),
        relationship=(
            "A workspace uses selected datasets from one data version. A Test or "
            "Production run uses its own accepted data version so its evidence "
            "continues to describe the exact delivery you reviewed."
        ),
        exclusion=(
            "A data version does not contain Recipe rules, Odoo credentials, "
            "approvals, or migration results."
        ),
        example=(
            "The files received on Monday can become Data version 1. A corrected "
            "delivery received on Thursday becomes a different data version."
        ),
        practical_effect=(
            "Check that the complete delivery is present before you select Accept "
            "Data version. Later corrections belong in a new delivery."
        ),
        related_slugs=("data-project", "workspace", "test-run", "production-run"),
    ),
    ConceptHelp(
        slug="workspace",
        term="Workspace",
        title="Why a workspace uses one data version",
        definition=(
            "A workspace is the working area where you inspect, match, prepare, "
            "compare, and load data for one use."
        ),
        relationship=(
            "The workspace uses selected datasets from one data version. The data "
            "project keeps both the workspace and the accepted data version."
        ),
        exclusion=(
            "A workspace does not own, copy, or silently replace the accepted "
            "source data."
        ),
        example=(
            "Your first workspace can use a small Authoring data version while "
            "you develop the Customer matching rules."
        ),
        practical_effect=(
            "You can complete one migration in this workspace without saving a "
            "Recipe. Save a Recipe only when you want to reuse the rules."
        ),
        related_slugs=("data-project", "data-version", "recipe"),
    ),
    ConceptHelp(
        slug="recipe",
        term="Recipe",
        title="What a Recipe saves",
        definition=(
            "A Recipe saves reusable preparation, matching, relationship, and "
            "checking rules."
        ),
        relationship=(
            "Impodo can apply a saved Recipe to another suitable data version. "
            "The data project continues to keep its data versions and workspaces."
        ),
        exclusion=(
            "A Recipe does not contain source rows, Odoo access, approvals, "
            "numeric Odoo record IDs, or migration results."
        ),
        example=(
            "A Customer Recipe can remember how legacy customer columns match "
            "Odoo fields without copying any customer records."
        ),
        practical_effect=(
            "Save a Recipe only when the rules will be useful with another "
            "delivery. One-off work remains complete without one."
        ),
        related_slugs=("workspace", "recipe-version", "test-run"),
    ),
    ConceptHelp(
        slug="recipe-version",
        term="Recipe version",
        title="Why saved Recipe rules have versions",
        definition=(
            "A Recipe version is one saved set of reusable rules. Impodo keeps "
            "that version unchanged so later Test evidence still has an exact meaning."
        ),
        relationship=(
            "When reusable rules change, Impodo saves another version under the "
            "same Recipe. Test and Production select an exact version."
        ),
        exclusion=(
            "Saving a new Recipe version does not change an earlier version or "
            "the evidence produced from it."
        ),
        example=(
            "Customer Recipe version 2 can add a reviewed country rule while "
            "version 1 remains available as the rules used by an earlier Test."
        ),
        practical_effect=(
            "When you change reusable rules, test the new Recipe version before "
            "you select it for a later rollout."
        ),
        related_slugs=("recipe", "test-run", "cutover-plan"),
    ),
    ConceptHelp(
        slug="test-run",
        term="Test run",
        title="What an Integrated Test run proves",
        definition=(
            "An Integrated Test run rehearses selected Recipe versions together "
            "with one accepted Test data version and one reviewed Odoo target."
        ),
        relationship=(
            "The run gives each selected Recipe a separate work area and checks "
            "their required order and overlapping Odoo changes."
        ),
        exclusion=(
            "A successful Test run does not authorize Production and does not "
            "supply Production data or credentials."
        ),
        example=(
            "A Customer Recipe can finish before a Sales Order Recipe when the "
            "orders need the customer records created by the first step."
        ),
        practical_effect=(
            "Select only the Recipe versions and dependency order that you want "
            "to prove together."
        ),
        related_slugs=("recipe-version", "recipe-work-area", "cutover-plan"),
    ),
    ConceptHelp(
        slug="recipe-work-area",
        term="Recipe work area",
        title="Why each Recipe has a separate work area",
        definition=(
            "A Recipe work area is the separate workspace where Impodo applies "
            "one selected Recipe version during a Test or Production run."
        ),
        relationship=(
            "All work areas in the run use the run's accepted data and reviewed "
            "Odoo target, but each keeps its own mapping and verification evidence."
        ),
        exclusion=(
            "One Recipe work area cannot silently change another work area or "
            "the saved Recipe version."
        ),
        example=(
            "The Customer and Sales Order Recipes receive separate work areas "
            "even when both participate in the same Test run."
        ),
        practical_effect=(
            "Open a Recipe work area when that specific Recipe needs attention. "
            "Return to the run overview to review shared progress."
        ),
        related_slugs=("test-run", "production-run", "recipe-version"),
    ),
    ConceptHelp(
        slug="cutover-plan",
        term="Cutover plan",
        title="What a Cutover plan preserves",
        definition=(
            "A Cutover plan records the exact Recipe versions, order, writable "
            "fields, and shared controls that the Integrated Test proved."
        ),
        relationship=(
            "Qualification confirms that every Recipe work area completed and "
            "was verified in the required order. You may then select that exact "
            "plan as the candidate for Production."
        ),
        exclusion=(
            "Qualification or selection does not authorize a Production write. "
            "Production still requires fresh data, access, comparison, and approval."
        ),
        example=(
            "The plan can require Customers to finish and verify before Sales "
            "Orders begin."
        ),
        practical_effect=(
            "Select the plan only when its Recipe versions, order, and controls "
            "match the rollout you intend to prepare."
        ),
        related_slugs=("test-run", "production-run", "recipe-version"),
    ),
    ConceptHelp(
        slug="production-run",
        term="Production run",
        title="Why Production starts with fresh evidence",
        definition=(
            "A Production run applies the selected Cutover plan to the latest "
            "accepted source delivery and the intended Production Odoo target."
        ),
        relationship=(
            "The selected plan supplies the tested reusable rules and order. The "
            "Production run supplies its own data version, Odoo access, comparison, "
            "approval, load records, and verification."
        ),
        exclusion=(
            "Production never treats Test data, credentials, approvals, or results "
            "as current Production evidence."
        ),
        example=(
            "A plan qualified with June Test data can be applied to the separately "
            "accepted July Production export only after fresh Production checks."
        ),
        practical_effect=(
            "Confirm the latest complete delivery and separate Production access "
            "before Impodo creates the Recipe work areas."
        ),
        related_slugs=("cutover-plan", "data-version", "recipe-work-area"),
    ),
)


CONCEPTS_BY_SLUG: Mapping[str, ConceptHelp] = MappingProxyType(
    {concept.slug: concept for concept in CONCEPTS}
)
