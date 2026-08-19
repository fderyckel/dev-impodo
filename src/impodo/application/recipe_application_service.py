"""Apply one immutable Recipe revision to fresh Test or Production evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Mapping, Protocol
import unicodedata
from uuid import uuid4

from ..access import Actor, AuthorizationPolicy, Capability
from ..adapters.protected_recipe_store import ProtectedRecipeStore
from ..domain.coverage import ReferenceBundle
from ..derived_entities import (
    DerivedEntityPlan,
    DerivedEntityRule,
    RelatedDatasetRule,
)
from ..domain.mapping.contracts import (
    BusinessControlDefinition,
    CategoricalCoveragePolicy,
    DatasetMapping,
    IdentityComponentMapping,
    MappingControlExpectation,
    MappingDefinition,
    MappingTargetMode,
    ReferenceKeyMapping,
    ReferenceLookupMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
    ScalarValueSource,
    TargetFieldDisposition,
    TargetFieldHandling,
    ValueMapping,
)
from ..domain.recipe_applications import (
    RecipeApplicationDraft,
    RecipeApplicationError,
    RecipeApplicationEvidence,
    RecipeApplicationIssue,
    RecipeApplicationIssueLevel,
    RecipeApplicationState,
    RecipeControlValues,
    RecipeParameterValues,
    TargetBinding,
    TargetCredentialRole,
    TargetEnvironment,
    TargetProbeStatus,
)
from ..domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
    SchemaGovernance,
)
from ..domain.structural import (
    AggregateSpec,
    ExactJoinRule,
    GroupAggregateRule,
    GroupKey,
    JoinKey,
    JoinKind,
    StructuralOutputColumn,
    StructuralProjection,
    UnionAllRule,
    UnionBranch,
)
from ..domain.serialization import content_hash
from ..models import target_identity_hash
from ..projects import (
    MigrationProject,
    OdooConnectionMode,
    ProjectService,
)
from ..quality import (
    QualityOutcomePolicy,
    QualityOwnerRole,
    QualityRule,
    QualityRuleFamily,
    QualityRuleSource,
)
from ..recipes import DataVersion, DataVersionPurpose, RecipeConflictError
from ..value_rules import ScalarTransformPolicy, ScalarValidationPolicy, TextTransformStep
from ..workspace_contracts import OdooSchemaCatalog, SchemaOrigin, SourceSelection
from .categorical_coverage_service import CategoricalCoverageService
from .mapping_workspace_service import MappingWorkspaceService
from .recipe_service import RecipeService
from .schema_workspace_service import SchemaWorkspaceService


class RecipeApplicationProjectReader(Protocol):
    def get(self, project_id: str) -> MigrationProject: ...


class RecipeApplicationSourceRepository(Protocol):
    def get_source_selection(self, project_id: str) -> SourceSelection | None: ...
    def get_mapping_source_selection(self, project_id: str) -> SourceSelection | None: ...


class RecipeApplicationSchemaRepository(Protocol):
    def get_odoo_schema_catalog(self, project_id: str) -> OdooSchemaCatalog | None: ...
    def get_schema_governance(self, project_id: str) -> SchemaGovernance | None: ...


class RecipeApplicationReferenceRepository(Protocol):
    def get_reference_bundle(self, project_id: str) -> ReferenceBundle | None: ...


class RecipeApplicationPreparationRepository(Protocol):
    def get_derived_entity_plan(self, project_id: str) -> DerivedEntityPlan | None: ...
    def save_derived_entity_plan(
        self,
        project_id: str,
        plan: DerivedEntityPlan,
        *,
        expected_parent_version: int | None,
        actor: Actor,
    ) -> None: ...


class RecipeApplicationStateRepository(Protocol):
    def get_target_binding(self, project_id: str) -> TargetBinding | None: ...
    def save_target_binding(self, project_id: str, binding: TargetBinding, *, actor: Actor) -> None: ...
    def get_parameter_values(self, project_id: str) -> RecipeParameterValues | None: ...
    def save_parameter_values(self, project_id: str, values: RecipeParameterValues, *, actor: Actor) -> None: ...
    def get_control_values(self, project_id: str) -> RecipeControlValues | None: ...
    def save_control_values(self, project_id: str, values: RecipeControlValues, *, actor: Actor) -> None: ...
    def get_draft(self, project_id: str) -> RecipeApplicationDraft | None: ...
    def save_draft(self, project_id: str, draft: RecipeApplicationDraft, *, expected_revision: int | None, actor: Actor) -> None: ...
    def save_evidence_projection(self, project_id: str, *, application_id: str, content_hash: str, evidence_json: str, created_at: str) -> None: ...
    def save_quality_seed(self, project_id: str, *, application_id: str, mapping_content_hash: str, rules: tuple[QualityRule, ...], actor: Actor) -> None: ...


@dataclass(frozen=True, slots=True)
class RecipeApplicationReview:
    """Read-only focused drift projection rendered before materialization."""

    recipe_id: str
    recipe_revision: int
    data_version: DataVersion
    project: MigrationProject
    recipe_semantic_hash: str
    state: RecipeApplicationState
    issues: tuple[RecipeApplicationIssue, ...]
    source_bindings: Mapping[str, str]
    source_candidates: Mapping[str, tuple[tuple[str, str], ...]]
    overrides: Mapping[str, str]
    target_binding: TargetBinding | None
    target_assessment_hash: str
    parameter_definitions: tuple[Mapping[str, object], ...]
    control_definitions: tuple[Mapping[str, object], ...]
    parameter_values: RecipeParameterValues | None
    control_values: RecipeControlValues | None
    reused_rule_count: int

    @property
    def can_apply(self) -> bool:
        return self.state is RecipeApplicationState.READY and not any(
            issue.blocks for issue in self.issues
        )


class RecipeApplicationService:
    """Bind portable Recipe meaning to one fresh contained workspace."""

    def __init__(
        self,
        *,
        recipes: RecipeService,
        projects: ProjectService,
        project_reader: RecipeApplicationProjectReader,
        sources: RecipeApplicationSourceRepository,
        schemas: RecipeApplicationSchemaRepository,
        schema_workspace: SchemaWorkspaceService,
        references: RecipeApplicationReferenceRepository,
        preparation: RecipeApplicationPreparationRepository,
        applications: RecipeApplicationStateRepository,
        mappings: MappingWorkspaceService,
        categorical: CategoricalCoverageService,
        store: ProtectedRecipeStore,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.recipes = recipes
        self.projects = projects
        self.project_reader = project_reader
        self.sources = sources
        self.schemas = schemas
        self.schema_workspace = schema_workspace
        self.references = references
        self.preparation = preparation
        self.applications = applications
        self.mappings = mappings
        self.categorical = categorical
        self.store = store
        self.authorization = authorization

    def current_draft(
        self,
        project_id: str,
        *,
        actor: Actor,
    ) -> RecipeApplicationDraft | None:
        """Return the bounded application status for surrounding workspace UI."""

        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=project_id,
        )
        return self.applications.get_draft(project_id)

    def start_test_data_version(
        self,
        recipe_id: str,
        *,
        expected_recipe_revision: int,
        label: str,
        parameter_values: Mapping[str, str],
        actor: Actor,
    ) -> tuple[DataVersion, MigrationProject]:
        """Provision a clean Test workspace without copying target evidence."""

        return self._start_data_version(
            recipe_id,
            expected_recipe_revision=expected_recipe_revision,
            label=label,
            parameter_values=parameter_values,
            control_values={},
            purpose=DataVersionPurpose.TEST,
            expected_cutover_candidate_id=None,
            actor=actor,
        )

    def start_production_data_version(
        self,
        recipe_id: str,
        *,
        expected_recipe_revision: int,
        expected_cutover_candidate_id: str,
        label: str,
        parameter_values: Mapping[str, str],
        control_values: Mapping[str, str],
        actor: Actor,
    ) -> tuple[DataVersion, MigrationProject]:
        """Provision a clean Production workspace from the selected candidate."""

        return self._start_data_version(
            recipe_id,
            expected_recipe_revision=expected_recipe_revision,
            label=label,
            parameter_values=parameter_values,
            control_values=control_values,
            purpose=DataVersionPurpose.PRODUCTION,
            expected_cutover_candidate_id=expected_cutover_candidate_id,
            actor=actor,
        )

    def _start_data_version(
        self,
        recipe_id: str,
        *,
        expected_recipe_revision: int,
        label: str,
        parameter_values: Mapping[str, str],
        control_values: Mapping[str, str],
        purpose: DataVersionPurpose,
        expected_cutover_candidate_id: str | None,
        actor: Actor,
    ) -> tuple[DataVersion, MigrationProject]:
        """Create an evidence-empty application workspace for one exact revision."""

        self.authorization.require(actor, Capability.RECIPE_APPLY)
        self.authorization.require(actor, Capability.DATA_VERSION_CREATE)
        recipe = self.recipes.get(recipe_id, actor=actor)
        if recipe.optimistic_revision != expected_recipe_revision:
            raise RecipeConflictError("Recipe changed; reload before starting this run")
        if recipe.current_recipe_revision is None or recipe.current_data_version_id is None:
            raise RecipeConflictError("Publish a Recipe revision before applying it")
        if purpose is DataVersionPurpose.PRODUCTION:
            candidate = self.recipes.cutover_candidate(recipe_id, actor=actor)
            if (
                candidate is None
                or candidate.cutover_candidate_id != expected_cutover_candidate_id
            ):
                raise RecipeConflictError(
                    "The selected rollout candidate changed; reload before starting Production"
                )
            recipe_revision = candidate.recipe_revision
        else:
            recipe_revision = recipe.current_recipe_revision
        envelope = self.recipes.read_revision(
            recipe_id,
            recipe_revision,
            actor=actor,
        )
        definition = dict(envelope["recipe"])
        normalized = self._parameter_values(
            tuple(dict(definition["parameter_definitions"]).get("parameters", ())),
            parameter_values,
        )
        normalized_controls = self._control_values(
            tuple(dict(definition["control_definitions"]).get("controls", ())),
            control_values,
        )
        current_version = next(
            item
            for item in self.recipes.data_versions(recipe_id, actor=actor)
            if item.data_version_id == recipe.current_data_version_id
        )
        origin = self.project_reader.get(current_version.workspace_project_id)
        clean_label = label.strip()
        if not clean_label or len(clean_label) > 200:
            raise RecipeApplicationError(f"{purpose.value.title()} data label is invalid")
        data_version_id = str(uuid4())
        now = datetime.now(timezone.utc)
        values = RecipeParameterValues(
            data_version_id=data_version_id,
            values=normalized,
            source="DATA_MANAGER",
            reason=f"{purpose.value.title()} application of Recipe v{recipe_revision}",
            actor=actor.identity,
            confirmed_at=now,
        )
        controls = RecipeControlValues(
            data_version_id=data_version_id,
            values=normalized_controls,
            actor=actor.identity,
            confirmed_at=now,
        )
        workspace = self.projects.create_data_version_workspace(
            actor=actor,
            name=clean_label,
            source_system=origin.source_system,
            data_manager=origin.data_manager,
            functional_owner=origin.functional_owner,
            business_unit=origin.business_unit,
            data_classification=recipe.data_classification,
            retention_days=recipe.retention_days,
            support_access=origin.support_access,
        )
        export_date = self._export_date(normalized)
        try:
            self.applications.save_parameter_values(
                workspace.project_id,
                values,
                actor=actor,
            )
            self.applications.save_control_values(
                workspace.project_id,
                controls,
                actor=actor,
            )
            self.recipes.create_data_version(
                recipe_id,
                expected_recipe_revision=expected_recipe_revision,
                workspace_project_id=workspace.project_id,
                purpose=purpose,
                label=clean_label,
                actor=actor,
                export_as_of_date=export_date,
                parameter_values_hash=values.content_hash,
                data_version_id=data_version_id,
            )
        except Exception:
            self.projects.discard_unlinked_workspace(workspace.project_id)
            raise
        created = next(
            item
            for item in self.recipes.data_versions(recipe_id, actor=actor)
            if item.data_version_id == data_version_id
        )
        return created, workspace

    def save_inputs(
        self,
        recipe_id: str,
        *,
        parameter_values: Mapping[str, str],
        control_values: Mapping[str, str],
        overrides: Mapping[str, str],
        actor: Actor,
    ) -> None:
        """Save only explicit DataVersion values and exact physical overrides."""

        context = self._context(recipe_id, actor=actor)
        definition = context[3]
        data_version = context[1]
        project = context[2]
        now = datetime.now(timezone.utc)
        parameters = RecipeParameterValues(
            data_version_id=data_version.data_version_id,
            values=self._parameter_values(
                tuple(dict(definition["parameter_definitions"]).get("parameters", ())),
                parameter_values,
            ),
            source="DATA_MANAGER",
            reason=(
                f"Confirmed for current {data_version.purpose.value.title()} "
                "Recipe application"
            ),
            actor=actor.identity,
            confirmed_at=now,
        )
        controls = RecipeControlValues(
            data_version_id=data_version.data_version_id,
            values=self._control_values(
                tuple(dict(definition["control_definitions"]).get("controls", ())),
                control_values,
            ),
            actor=actor.identity,
            confirmed_at=now,
        )
        current_parameters = self.applications.get_parameter_values(project.project_id)
        if current_parameters is None:
            raise RecipeApplicationError(
                "Current Recipe parameter evidence is missing; start a new data version"
            )
        if current_parameters.content_hash != parameters.content_hash:
            self.recipes.update_data_version_parameter_values_hash(
                recipe_id,
                data_version.data_version_id,
                expected_hash=current_parameters.content_hash,
                parameter_values_hash=parameters.content_hash,
                actor=actor,
            )
        self.applications.save_parameter_values(project.project_id, parameters, actor=actor)
        self.applications.save_control_values(project.project_id, controls, actor=actor)
        current = self.applications.get_draft(project.project_id)
        review = self.review(
            recipe_id,
            credential_generation="",
            credential_storage_class="SESSION",
            supplied_overrides=overrides,
            actor=actor,
        )
        self._save_review_draft(review, actor=actor, expected=current)

    def review(
        self,
        recipe_id: str,
        *,
        credential_generation: str,
        credential_storage_class: str,
        actor: Actor,
        supplied_overrides: Mapping[str, str] | None = None,
    ) -> RecipeApplicationReview:
        """Assess only required source, target, parameter, control, and credential drift."""

        recipe, data_version, project, definition, semantic_hash = self._context(
            recipe_id,
            actor=actor,
        )
        existing = self.applications.get_draft(project.project_id)
        overrides = dict(
            supplied_overrides
            if supplied_overrides is not None
            else (existing.overrides if existing is not None else {})
        )
        issues: list[RecipeApplicationIssue] = []
        selection = self.sources.get_mapping_source_selection(project.project_id)
        source_bindings: dict[str, str] = {}
        candidates: dict[str, tuple[tuple[str, str], ...]] = {}
        if selection is None:
            issues.append(self._block(
                "RECIPE_SOURCE_NOT_READY",
                "Freeze the representative source tables before applying the Recipe.",
                "Open Source data and confirm the replacement tables.",
            ))
        else:
            source_bindings, candidates, source_issues = self._source_assessment(
                definition,
                selection,
                overrides,
            )
            issues.extend(source_issues)
        parameters = self.applications.get_parameter_values(project.project_id)
        controls = self.applications.get_control_values(project.project_id)
        issues.extend(self._input_issues(definition, data_version, parameters, controls))
        schema = self.schemas.get_odoo_schema_catalog(project.project_id)
        target_hash = content_hash({"target": "not-ready"})
        target_binding = None
        if schema is None:
            issues.append(self._block(
                "RECIPE_TARGET_NOT_READY",
                "Capture current Odoo field evidence before applying the Recipe.",
                "Open Odoo data and capture the required models and fields.",
            ))
        else:
            target_hash, target_issues = self._target_assessment(definition, schema)
            issues.extend(target_issues)
            target_binding, binding_issue = self._target_binding(
                project,
                data_version,
                schema,
                target_hash,
                credential_generation=credential_generation,
                credential_storage_class=credential_storage_class,
                actor=actor,
            )
            if binding_issue is not None:
                issues.append(binding_issue)
        issues.extend(self._reference_issues(definition, project.project_id))
        issues.extend(self._quality_issues(definition))
        if (
            existing is not None
            and existing.state is RecipeApplicationState.BLOCKED
            and selection is not None
            and parameters is not None
            and existing.source_selection_hash == selection.content_hash
            and existing.parameter_values_hash == parameters.content_hash
            and existing.target_assessment_hash == target_hash
            and dict(existing.overrides) == overrides
        ):
            issues.extend(
                item
                for item in existing.issues
                if item.code.startswith("MAPPING_")
            )
        ordered = self._bounded_issues(issues)
        state = (
            RecipeApplicationState.REVIEW_REQUIRED
            if any(item.blocks for item in ordered)
            else RecipeApplicationState.READY
        )
        return RecipeApplicationReview(
            recipe_id=recipe.recipe_id,
            recipe_revision=data_version.pinned_recipe_revision or recipe.current_recipe_revision or 0,
            data_version=data_version,
            project=project,
            recipe_semantic_hash=semantic_hash,
            state=state,
            issues=ordered,
            source_bindings=source_bindings,
            source_candidates=candidates,
            overrides=overrides,
            target_binding=target_binding,
            target_assessment_hash=target_hash,
            parameter_definitions=tuple(dict(definition["parameter_definitions"]).get("parameters", ())),
            control_definitions=tuple(dict(definition["control_definitions"]).get("controls", ())),
            parameter_values=parameters,
            control_values=controls,
            reused_rule_count=self._reused_rule_count(definition),
        )

    def apply(
        self,
        recipe_id: str,
        *,
        credential_generation: str,
        credential_storage_class: str,
        actor: Actor,
    ) -> RecipeApplicationEvidence:
        """Create a fresh MappingWorkingDraft and immutable application evidence."""

        self.authorization.require(actor, Capability.RECIPE_APPLY)
        review = self.review(
            recipe_id,
            credential_generation=credential_generation,
            credential_storage_class=credential_storage_class,
            actor=actor,
        )
        application_id = str(uuid4())
        now = datetime.now(timezone.utc)
        issues = list(review.issues)
        mapping_id = None
        mapping_hash = None
        target_binding = review.target_binding
        if target_binding is None:
            raise RecipeApplicationError(
                "Accept a current remote TargetBinding before applying this Recipe"
            )
        self.applications.save_target_binding(
            review.project.project_id,
            target_binding,
            actor=actor,
        )
        if review.can_apply:
            definition = self._context(recipe_id, actor=actor)[3]
            self._materialize_preparation(
                review.project.project_id,
                definition,
                review.source_bindings,
                actor=actor,
            )
            governance = self._materialize_governance(
                review.project.project_id,
                definition,
                actor=actor,
            )
            selection = self.sources.get_mapping_source_selection(review.project.project_id)
            schema = self.schemas.get_odoo_schema_catalog(review.project.project_id)
            if selection is None or schema is None:
                raise RecipeApplicationError("Application evidence changed before materialization")
            effective_bindings = self._effective_bindings(
                selection,
                review.source_bindings,
            )
            datasets = self._mapping_datasets(
                definition,
                effective_bindings,
                selection,
                review.control_values,
                self.references.get_reference_bundle(review.project.project_id),
            )
            candidate = MappingDefinition(
                mapping_id=str(uuid4()),
                source_selection_hash=selection.content_hash,
                schema_hash=governance.content_hash,
                datasets=datasets,
            )
            collection = self.categorical.collect(
                review.project.project_id,
                candidate,
                selection,
                schema,
            )
            for item in collection.issues:
                issues.append(
                    RecipeApplicationIssue(
                        code=item.code,
                        level=RecipeApplicationIssueLevel.BLOCKER,
                        message=item.message,
                        recovery_action=item.remediation,
                        logical_id=item.path,
                    )
                )
            if not any(item.blocks for item in issues):
                current = self.mappings.mappings.get_mapping_working_draft(
                    review.project.project_id
                )
                draft = self.mappings.save_working_draft(
                    review.project.project_id,
                    datasets=datasets,
                    expected_version=(current.version if current else None),
                    actor=actor,
                )
                mapping_id = draft.mapping_id
                mapping_hash = draft.definition.content_hash
                self.applications.save_quality_seed(
                    review.project.project_id,
                    application_id=application_id,
                    mapping_content_hash=mapping_hash,
                    rules=self._quality_rules(
                        definition,
                        effective_bindings,
                        selection,
                    ),
                    actor=actor,
                )
        status = (
            RecipeApplicationState.APPLIED
            if mapping_id is not None
            else RecipeApplicationState.BLOCKED
        )
        binding_hash = content_hash(
            {
                "controls": review.control_values.content_hash if review.control_values else None,
                "overrides": dict(sorted(review.overrides.items())),
                "parameters": review.parameter_values.content_hash if review.parameter_values else None,
                "source_bindings": dict(sorted(review.source_bindings.items())),
                "target_binding": target_binding.content_hash if target_binding else None,
            }
        )
        issue_hash = content_hash([item.fingerprint for item in issues])
        evidence = RecipeApplicationEvidence(
            application_id=application_id,
            recipe_id=review.recipe_id,
            recipe_revision=review.recipe_revision,
            recipe_semantic_hash=review.recipe_semantic_hash,
            data_version_id=review.data_version.data_version_id,
            workspace_project_id=review.project.project_id,
            source_artifact_hash=self._source_artifact_hash(review.project.project_id),
            source_selection_hash=(
                self.sources.get_mapping_source_selection(review.project.project_id).content_hash
                if self.sources.get_mapping_source_selection(review.project.project_id)
                else content_hash({"source": "missing"})
            ),
            parameter_values_hash=(
                review.parameter_values.content_hash
                if review.parameter_values
                else content_hash({"parameters": "missing"})
            ),
            control_values_hash=(
                review.control_values.content_hash
                if review.control_values
                else content_hash({"controls": "missing"})
            ),
            target_binding_id=target_binding.target_binding_id,
            target_binding_hash=target_binding.content_hash,
            target_contract_assessment_hash=review.target_assessment_hash,
            binding_hash=binding_hash,
            issue_hash=issue_hash,
            mapping_id=mapping_id,
            mapping_content_hash=mapping_hash,
            status=status,
            created_at=now,
            created_by=actor.identity,
        )
        stored = self.store.put(
            review.recipe_id,
            kind="applications",
            object_id=application_id,
            logical_hash=evidence.content_hash,
            payload=evidence.to_json().encode("utf-8"),
        )
        self.applications.save_evidence_projection(
            review.project.project_id,
            application_id=application_id,
            content_hash=evidence.content_hash,
            evidence_json=evidence.to_json(),
            created_at=now.isoformat(),
        )
        self.recipes.record_application_projection(
            actor=actor,
            application_id=application_id,
            recipe_id=review.recipe_id,
            recipe_revision=review.recipe_revision,
            data_version_id=review.data_version.data_version_id,
            workspace_project_id=review.project.project_id,
            source_selection_hash=evidence.source_selection_hash,
            parameter_values_hash=evidence.parameter_values_hash,
            target_binding_hash=evidence.target_binding_hash,
            credential_generation=target_binding.credential_generation,
            binding_hash=binding_hash,
            issue_hash=issue_hash,
            mapping_id=mapping_id,
            mapping_content_hash=mapping_hash,
            status=("APPLIED" if status is RecipeApplicationState.APPLIED else "BLOCKED"),
            evidence_storage_key=stored.storage_key,
            evidence_hash=evidence.content_hash,
            created_at=now,
        )
        prior = self.applications.get_draft(review.project.project_id)
        draft = RecipeApplicationDraft(
            application_id=application_id,
            recipe_id=review.recipe_id,
            recipe_revision=review.recipe_revision,
            data_version_id=review.data_version.data_version_id,
            workspace_project_id=review.project.project_id,
            target_binding_hash=evidence.target_binding_hash,
            source_selection_hash=evidence.source_selection_hash,
            parameter_values_hash=evidence.parameter_values_hash,
            revision=(prior.revision + 1 if prior else 1),
            state=status,
            overrides=review.overrides,
            issues=tuple(issues),
            binding_hash=binding_hash,
            target_assessment_hash=review.target_assessment_hash,
            updated_at=now,
            updated_by=actor.identity,
        )
        self.applications.save_draft(
            review.project.project_id,
            draft,
            expected_revision=(prior.revision if prior else None),
            actor=actor,
        )
        return evidence

    def _context(self, recipe_id: str, *, actor: Actor):
        recipe = self.recipes.get(recipe_id, actor=actor)
        if recipe.current_recipe_revision is None or recipe.current_data_version_id is None:
            raise RecipeConflictError("Publish a Recipe revision before applying it")
        data_version = next(
            item
            for item in self.recipes.data_versions(recipe_id, actor=actor)
            if item.data_version_id == recipe.current_data_version_id
        )
        revision = data_version.pinned_recipe_revision
        if revision is None:
            raise RecipeConflictError("The data version has no pinned Recipe revision")
        if data_version.purpose is DataVersionPurpose.TEST:
            if revision != recipe.current_recipe_revision:
                raise RecipeConflictError(
                    "Start a Test data version for the current Recipe revision"
                )
        elif data_version.purpose is DataVersionPurpose.PRODUCTION:
            candidate = self.recipes.cutover_candidate(recipe_id, actor=actor)
            if (
                candidate is None
                or recipe.cutover_candidate_id != candidate.cutover_candidate_id
                or revision != candidate.recipe_revision
            ):
                raise RecipeConflictError(
                    "Start Production again from the selected rollout candidate"
                )
        else:
            raise RecipeConflictError(
                "Start a Test or Production data version before applying the Recipe"
            )
        project = self.project_reader.get(data_version.workspace_project_id)
        envelope = self.recipes.read_revision(recipe_id, revision, actor=actor)
        return recipe, data_version, project, dict(envelope["recipe"]), str(envelope["semantic_hash"])

    @staticmethod
    def _parameter_values(definitions, supplied):
        expected = {str(item["logical_parameter_id"]): dict(item) for item in definitions}
        unknown = sorted(set(supplied) - set(expected))
        if unknown:
            raise RecipeApplicationError(f"Parameter {unknown[0]} is not declared by this Recipe")
        normalized: dict[str, object] = {}
        for logical_id, definition in expected.items():
            raw = str(supplied.get(logical_id, "")).strip()
            if not raw:
                if bool(definition.get("required")):
                    raise RecipeApplicationError(f"Enter {definition.get('label', logical_id)}")
                continue
            value_type = str(definition.get("type", "string"))
            if value_type == "date":
                try:
                    value = date.fromisoformat(raw)
                except ValueError as error:
                    raise RecipeApplicationError(f"{definition.get('label', logical_id)} must be a date") from error
                if dict(definition.get("constraints", {})).get("not_after_application_date") and value > date.today():
                    raise RecipeApplicationError(f"{definition.get('label', logical_id)} cannot be in the future")
                normalized[logical_id] = value.isoformat()
            elif value_type == "integer":
                try:
                    normalized[logical_id] = int(raw)
                except ValueError as error:
                    raise RecipeApplicationError(f"{definition.get('label', logical_id)} must be a whole number") from error
            elif value_type == "decimal":
                try:
                    normalized[logical_id] = format(Decimal(raw), "f")
                except InvalidOperation as error:
                    raise RecipeApplicationError(f"{definition.get('label', logical_id)} must be a number") from error
            else:
                normalized[logical_id] = raw
        return normalized

    @staticmethod
    def _control_values(definitions, supplied):
        expected = {str(item["logical_control_id"]): dict(item) for item in definitions}
        unknown = sorted(set(supplied) - set(expected))
        if unknown:
            raise RecipeApplicationError(f"Control {unknown[0]} is not declared by this Recipe")
        values: dict[str, str] = {}
        for logical_id, definition in expected.items():
            if bool(definition.get("invariant_expectation")):
                values[logical_id] = str(definition["invariant_expected_total"])
                continue
            raw = str(supplied.get(logical_id, "")).strip()
            if not raw:
                continue
            try:
                values[logical_id] = format(Decimal(raw), "f")
            except InvalidOperation as error:
                raise RecipeApplicationError(f"Control {definition.get('name', logical_id)} must be a number") from error
        return values

    @staticmethod
    def _export_date(values):
        raw = values.get("parameter:export_as_of_date")
        return date.fromisoformat(str(raw)) if raw else None

    def _input_issues(self, definition, data_version, parameters, controls):
        issues = []
        parameter_defs = tuple(dict(definition["parameter_definitions"]).get("parameters", ()))
        if parameters is None or parameters.data_version_id != data_version.data_version_id:
            issues.append(self._block("RECIPE_PARAMETER_VALUES_MISSING", "Current Recipe parameter values are missing.", "Enter the values required for this data version."))
        else:
            if data_version.parameter_values_hash != parameters.content_hash:
                issues.append(self._block("RECIPE_PARAMETER_VALUES_STALE", "Current Recipe parameter values are not pinned to this data version.", "Save the current parameter values again or start a new data version."))
            supplied = set(parameters.values)
            for item in parameter_defs:
                logical_id = str(item["logical_parameter_id"])
                if bool(item.get("required")) and logical_id not in supplied:
                    issues.append(self._block("RECIPE_PARAMETER_VALUE_MISSING", f"{item.get('label', logical_id)} is required.", "Enter and confirm the current parameter value.", logical_id))
        control_defs = tuple(dict(definition["control_definitions"]).get("controls", ()))
        if control_defs and (controls is None or controls.data_version_id != data_version.data_version_id):
            issues.append(self._block("RECIPE_CONTROL_VALUES_MISSING", "Current expected control values are missing.", "Enter the expected totals for this data version."))
        elif controls is not None:
            for item in control_defs:
                logical_id = str(item["logical_control_id"])
                if not bool(item.get("invariant_expectation")) and logical_id not in controls.values:
                    issues.append(self._block("RECIPE_CONTROL_VALUE_MISSING", f"Expected value for {item.get('name', logical_id)} is required.", "Enter and confirm the current expected total.", logical_id))
        return issues

    @staticmethod
    def _bounded_issues(issues):
        """Keep blocker detail and collapse unbounded informational source drift."""

        ordered = sorted(
            issues,
            key=lambda item: (item.level.value, item.code, item.logical_id),
        )
        if len(ordered) <= 2_000:
            return tuple(ordered)
        actionable = [
            item
            for item in ordered
            if item.level is not RecipeApplicationIssueLevel.INFORMATION
        ]
        capacity = max(0, 1_999 - len(actionable))
        informational = [
            item
            for item in ordered
            if item.level is RecipeApplicationIssueLevel.INFORMATION
        ]
        collapsed = RecipeApplicationIssue(
            code="RECIPE_INFORMATION_COLLAPSED",
            level=RecipeApplicationIssueLevel.INFORMATION,
            message=(
                f"{len(informational) - capacity} additional unused source items "
                "are not used by this Recipe."
            ),
            recovery_action="No action is required.",
        )
        return tuple((actionable + informational[:capacity] + [collapsed])[:2_000])

    def _source_assessment(self, definition, selection, overrides):
        issues = []
        bindings: dict[str, str] = {}
        candidates: dict[str, tuple[tuple[str, str], ...]] = {}
        by_name: dict[str, list] = {}
        for dataset in selection.datasets:
            by_name.setdefault(dataset.name, []).append(dataset)
        used_datasets = set()
        used_columns: dict[str, set[str]] = {}
        for required in dict(definition["source_shape"]).get("datasets", ()):
            logical_dataset = str(required["logical_dataset_id"])
            matches = by_name.get(str(required["logical_name"]), [])
            if len(matches) != 1:
                issues.append(self._block("RECIPE_SOURCE_DATASET_MISSING", f"Required source table {required['logical_name']} is missing or ambiguous.", "Confirm one table with the exact reusable name.", logical_dataset))
                continue
            dataset = matches[0]
            bindings[logical_dataset] = dataset.dataset_id
            used_datasets.add(dataset.dataset_id)
            by_column: dict[str, list] = {}
            for column in dataset.columns:
                by_column.setdefault(column.source_name, []).append(column)
            used_columns[dataset.dataset_id] = set()
            for required_column in required.get("columns", ()):
                logical_column = str(required_column["logical_column_id"])
                matches = by_column.get(str(required_column["source_name"]), [])
                selected = matches[0] if len(matches) == 1 else None
                override = overrides.get(logical_column)
                if override:
                    selected = next((column for column in dataset.columns if column.stable_key == override), None)
                    if selected is None:
                        issues.append(self._block("RECIPE_SOURCE_OVERRIDE_STALE", f"The confirmed replacement for {required_column['source_name']} is no longer present.", "Choose the current exact replacement column.", logical_column))
                if selected is None:
                    candidates[logical_column] = tuple((column.stable_key, column.source_name) for column in dataset.columns)
                    issues.append(self._block("RECIPE_SOURCE_COLUMN_MISSING", f"Required source column {required_column['source_name']} is missing.", "Confirm the exact replacement column.", logical_column))
                    continue
                bindings[logical_column] = selected.stable_key
                used_columns[dataset.dataset_id].add(selected.stable_key)
        for dataset in selection.datasets:
            if dataset.dataset_id not in used_datasets:
                issues.append(self._info("RECIPE_SOURCE_DATASET_UNUSED", f"New source table {dataset.name} is not used by this Recipe.", "No action is required unless the table should become reusable Recipe meaning."))
                continue
            for column in dataset.columns:
                if column.stable_key not in used_columns.get(dataset.dataset_id, set()):
                    issues.append(self._info("RECIPE_SOURCE_COLUMN_UNUSED", f"New source column {column.source_name} is not used by this Recipe.", "No action is required unless the column should become reusable Recipe meaning."))
        return bindings, candidates, issues

    def _target_assessment(self, definition, schema):
        issues = []
        contract = dict(definition["odoo_target_contract"])
        try:
            actual_major = int(str(schema.odoo_version).split(".", 1)[0])
        except ValueError:
            actual_major = -1
        if actual_major != int(contract["odoo_major_version"]):
            issues.append(self._block("RECIPE_TARGET_VERSION_INCOMPATIBLE", "The connected Odoo major version does not match this Recipe.", "Choose a compatible Odoo server or publish and retest a new Recipe revision."))
        actual_models = {item.name: item for item in schema.models}
        dependency_projection = []
        provider_fields = {
            (str(dataset["target_model"]), str(field["target_field"]))
            for dataset in dict(definition["mapping"]).get("datasets", ())
            for field in dataset.get("fields", ())
            if str(dict(field.get("provider", {})).get("kind")) != "ODOO_DEFAULT"
        }
        provider_fields.update(
            (str(dataset["target_model"]), str(field["target_field"]))
            for dataset in dict(definition["mapping"]).get("datasets", ())
            for field in dataset.get("relationships", ())
        )
        dispositions = {
            (str(dataset["target_model"]), str(item["target_field"]))
            for dataset in dict(definition["mapping"]).get("datasets", ())
            for item in dataset.get("target_field_dispositions", ())
        }
        for required_model in contract.get("models", ()):
            model_name = str(required_model["model"])
            model = actual_models.get(model_name)
            if model is None:
                issues.append(self._block("RECIPE_TARGET_MODEL_MISSING", f"Required Odoo model {model_name} is missing.", "Install or expose the required Odoo application, then refresh Odoo data.", model_name))
                continue
            fields = {item.name: item for item in model.fields}
            for required_field in required_model.get("fields", ()):
                field_name = str(required_field["name"])
                field = fields.get(field_name)
                logical = f"{model_name}.{field_name}"
                if field is None:
                    issues.append(self._block("RECIPE_TARGET_FIELD_MISSING", f"Required Odoo field {logical} is missing.", "Add or expose the field, then refresh Odoo data.", logical))
                    continue
                dependency_projection.append({"model": model_name, "field": field_name, "type": field.type, "relation": field.relation, "required": field.required, "readonly": field.readonly, "selection": list(field.selection)})
                if field.type != str(required_field["field_type"]):
                    issues.append(self._block("RECIPE_TARGET_FIELD_TYPE_CHANGED", f"Odoo field {logical} changed type.", "Restore the compatible field type or publish and retest a new Recipe revision.", logical))
                if required_field.get("relation_model") != field.relation:
                    issues.append(self._block("RECIPE_TARGET_RELATION_CHANGED", f"Odoo relationship {logical} points to a different model.", "Restore the compatible relationship or publish and retest a new Recipe revision.", logical))
                if bool(required_field.get("write_use")) and field.readonly:
                    issues.append(self._block("RECIPE_TARGET_FIELD_READONLY", f"Odoo field {logical} is now read-only.", "Restore write access or publish and retest a Recipe that does not write this field.", logical))
                actual_codes = {str(item[0]) for item in field.selection}
                missing_codes = sorted(set(required_field.get("required_selection_codes", ())) - actual_codes)
                if missing_codes:
                    issues.append(self._block("RECIPE_TARGET_SELECTION_MISSING", f"Odoo field {logical} no longer offers required value {missing_codes[0]}.", "Restore the choice or update and retest the Recipe value mapping.", logical))
            for field in model.fields:
                if field.required and not field.readonly and (model_name, field.name) not in provider_fields | dispositions:
                    issues.append(self._block("RECIPE_TARGET_NEW_REQUIRED_FIELD", f"Odoo now requires {model_name}.{field.name}, but this Recipe provides no value.", "Add a provider/default or publish and retest a new Recipe revision.", f"{model_name}.{field.name}"))
        assessment_hash = content_hash({"contract": contract, "current_dependencies": dependency_projection})
        return assessment_hash, issues

    def _target_binding(self, project, data_version, schema, assessment_hash, *, credential_generation, credential_storage_class, actor):
        environment = (
            TargetEnvironment.PRODUCTION
            if data_version.purpose is DataVersionPurpose.PRODUCTION
            else TargetEnvironment.TEST
        )
        environment_label = environment.value.title()
        if project.odoo_connection_mode is not OdooConnectionMode.REMOTE:
            return None, self._block("RECIPE_TARGET_NOT_REMOTE", f"Recipe application requires a current remote {environment_label} Odoo server.", f"Configure the {environment_label} data version with Remote Odoo.")
        if schema.origin is not SchemaOrigin.LIVE_API:
            return None, self._block("RECIPE_TARGET_PROBE_MISSING", f"The {environment_label} target has no accepted live schema probe.", "Check the remote connection and capture Odoo data again.")
        if not credential_generation or schema.read_credential_binding_hash != credential_generation:
            return None, self._block("TARGET_BINDING_STALE", "The current read credential generation does not match captured Odoo evidence.", "Re-probe the credential and refresh Odoo data.")
        connection_hash = target_identity_hash(connection_mode="REMOTE", base_url=project.odoo_base_url, database=project.odoo_database)
        if schema.connection_target_hash != connection_hash:
            return None, self._block("RECIPE_TARGET_IDENTITY_CHANGED", "Captured Odoo evidence belongs to another server or database.", f"Refresh Odoo data for this exact {environment_label} target.")
        bundle = self.references.get_reference_bundle(project.project_id)
        reference_hashes = tuple(item.content_hash for item in bundle.datasets) if bundle else ()
        existing = self.applications.get_target_binding(project.project_id)
        comparable = {
            "environment": environment,
            "endpoint": project.odoo_base_url,
            "database": project.odoo_database,
            "connection_target_hash": connection_hash,
            "credential_generation": credential_generation,
            "credential_storage_class": credential_storage_class,
            "principal_hash": schema.read_principal_hash,
            "permission_hash": schema.read_permission_hash,
            "context_hash": schema.read_context_hash,
            "schema_dependency_hash": assessment_hash,
            "reference_snapshot_hashes": reference_hashes,
        }
        if existing is not None and all(getattr(existing, key) == value for key, value in comparable.items()):
            return existing, None
        return TargetBinding(
            target_binding_id=str(uuid4()),
            credential_role=TargetCredentialRole.READ,
            probe_status=TargetProbeStatus.ACCEPTED,
            probed_at=schema.captured_at,
            captured_by=actor.identity,
            **comparable,
        ), None

    def _reference_issues(self, definition, project_id):
        required = tuple(dict(definition["reference_dependencies"]).get("references", ()))
        if not required:
            return []
        bundle = self.references.get_reference_bundle(project_id)
        current = {item.name: item for item in bundle.datasets} if bundle else {}
        issues = []
        for item in required:
            found = current.get(str(item["name"]))
            if found is None or found.content_hash != str(item["content_hash"]):
                issues.append(self._block("RECIPE_REFERENCE_DEPENDENCY_MISSING", f"Reference data {item['name']} is missing or changed.", "Load and confirm the exact current reference package.", str(item["logical_reference_id"])))
        return issues

    def _quality_issues(self, definition):
        """Keep target-snapshot-dependent advanced checks out of silent reuse."""

        issues = []
        for rule in dict(definition["quality"]).get("rules", ()):
            if str(rule.get("origin")) != QualityRuleSource.MANAGER_AUTHORED.value:
                issues.append(
                    self._block(
                        "RECIPE_QUALITY_SCOPE_REVIEW_REQUIRED",
                        f"Data check {rule['name']} depends on a prior approved scope.",
                        "Re-establish the current reference scope and publish a new Recipe revision before reuse.",
                        str(rule["logical_rule_id"]),
                    )
                )
        return issues

    @staticmethod
    def _quality_rules(definition, bindings, selection):
        """Compile manager-authored Recipe checks into fresh physical rules."""

        datasets = {item.dataset_id: item for item in selection.datasets}
        mapping_datasets = {
            str(item["logical_dataset_id"]): item
            for item in dict(definition["mapping"]).get("datasets", ())
        }
        field_names = {
            str(field["logical_field_id"]): str(field["target_field"])
            for dataset in mapping_datasets.values()
            for field in dataset.get("fields", ())
        }
        rules = []
        for item in dict(definition["quality"]).get("rules", ()):
            if str(item.get("origin")) != QualityRuleSource.MANAGER_AUTHORED.value:
                continue
            logical_dataset = str(item["dataset_id"])
            physical_dataset = str(bindings[logical_dataset])
            source_dataset = datasets[physical_dataset]
            rule_id = content_hash(
                {
                    "logical_rule_id": str(item["logical_rule_id"]),
                    "physical_dataset": physical_dataset,
                    "project_id": selection.project_id,
                }
            )
            rules.append(
                QualityRule(
                    rule_id=rule_id,
                    dataset=source_dataset.name,
                    family=QualityRuleFamily(str(item["kind"])),
                    name=str(item["name"]),
                    explanation=str(item["explanation"]),
                    input_fields=tuple(
                        field_names[str(value)]
                        for value in item.get("field_ids", ())
                    ),
                    parameters={
                        str(key): str(value)
                        for key, value in dict(item.get("parameters", {})).items()
                    },
                    outcome=QualityOutcomePolicy(str(item["severity"])),
                    owner_role=QualityOwnerRole(str(item["owner_role"])),
                    source=QualityRuleSource.MANAGER_AUTHORED,
                    review_by_days=(
                        int(item["review_by_days"])
                        if item.get("review_by_days") is not None
                        else None
                    ),
                    evidence_display=str(item.get("evidence_display", "masked")),
                )
            )
        return tuple(sorted(rules, key=lambda item: item.rule_id))

    def _materialize_governance(self, project_id, definition, *, actor):
        keys = tuple(
            BusinessKeyDefinition(
                key_id=f"recipe:{item['model']}:{':'.join(item['ordered_fields'])}:{':'.join(item.get('scope_fields', ())) or 'global'}",
                model=str(item["model"]),
                key_fields=tuple(str(value) for value in item["ordered_fields"]),
                scope_fields=tuple(str(value) for value in item.get("scope_fields", ())),
                description="Reused from the published Recipe target contract",
                status=BusinessKeyStatus.CONFIRMED,
            )
            for item in dict(definition["odoo_target_contract"]).get("business_keys", ())
        )
        current = self.schemas.get_schema_governance(project_id)
        current_shapes = (
            {(item.model, item.key_fields, item.scope_fields) for item in current.business_keys}
            if current else set()
        )
        desired_shapes = {(item.model, item.key_fields, item.scope_fields) for item in keys}
        if current is not None and current.catalog_hash == self.schemas.get_odoo_schema_catalog(project_id).content_hash and current_shapes == desired_shapes:
            return current
        return self.schema_workspace.govern(project_id, business_keys=keys, actor=actor)

    def _materialize_preparation(
        self,
        project_id,
        definition,
        bindings,
        *,
        actor,
    ):
        semantic_rules = tuple(
            dict(definition["source_preparation"]).get("rules", ())
        )
        if not semantic_rules:
            return None
        source = self.sources.get_source_selection(project_id)
        if source is None:
            raise RecipeApplicationError(
                "Freeze source data before materializing Recipe preparation"
            )
        current = self.preparation.get_derived_entity_plan(project_id)
        if current is not None:
            if current.source_selection_hash != source.content_hash:
                raise RecipeApplicationError(
                    "Current source preparation is stale for this data version"
                )
            return current
        rules = tuple(
            self._preparation_rule(item, bindings)
            for item in semantic_rules
        )
        plan = DerivedEntityPlan(
            plan_id=str(uuid4()),
            version=1,
            project_id=project_id,
            source_selection_hash=source.content_hash,
            rules=rules,
            updated_at=datetime.now(timezone.utc),
            updated_by=actor.identity.display_name,
        )
        self.preparation.save_derived_entity_plan(
            project_id,
            plan,
            expected_parent_version=None,
            actor=actor,
        )
        return plan

    def _preparation_rule(self, raw, bindings):
        value = dict(raw)
        value.pop("logical_rule_id", None)
        kind = str(value.pop("kind", ""))
        rule_id = str(uuid4())
        if kind == "lookup":
            return DerivedEntityRule(
                rule_id=rule_id,
                output_dataset_name=str(value["output_dataset_name"]),
                source_dataset_id=bindings[str(value["source_dataset_id"])],
                source_column_key=bindings[str(value["source_column_key"])],
                target_model=str(value["target_model"]),
                target_name_field=str(value["target_name_field"]),
                external_id_namespace=str(value["external_id_namespace"]),
                parent_separator=(
                    str(value["parent_separator"])
                    if value.get("parent_separator") is not None
                    else None
                ),
                blank_policy=str(value.get("blank_policy", "block")),
            )
        if kind == "parent_child":
            return RelatedDatasetRule(
                rule_id=rule_id,
                source_dataset_id=bindings[str(value["source_dataset_id"])],
                parent_dataset_name=str(value["parent_dataset_name"]),
                child_dataset_name=str(value["child_dataset_name"]),
                parent_key_column_key=bindings[
                    str(value["parent_key_column_key"])
                ],
                child_key_column_key=bindings[str(value["child_key_column_key"])],
                scope_column_key=(
                    bindings[str(value["scope_column_key"])]
                    if value.get("scope_column_key") is not None
                    else None
                ),
                blank_policy=str(value.get("blank_policy", "block")),
            )
        if kind in {"exact_join", "LEFT", "INNER"} or {
            "left_dataset_id",
            "right_dataset_id",
            "keys",
        }.issubset(value):
            output_keys = {
                str(item["column_key"]): self._application_column_key(
                    str(item["column_key"])
                )
                for item in value.get("output_columns", ())
            }
            return ExactJoinRule(
                rule_id=rule_id,
                output_dataset_name=str(value["output_dataset_name"]),
                left_dataset_id=bindings[str(value["left_dataset_id"])],
                right_dataset_id=bindings[str(value["right_dataset_id"])],
                keys=tuple(
                    JoinKey(
                        left_column_key=bindings[str(item["left_column_key"])],
                        right_column_key=bindings[str(item["right_column_key"])],
                        value_type=str(item.get("value_type", "string")),
                    )
                    for item in value.get("keys", ())
                ),
                output_columns=tuple(
                    StructuralOutputColumn(
                        column_key=output_keys[str(item["column_key"])],
                        source_name=str(item["source_name"]),
                        candidate_type=str(item["candidate_type"]),
                    )
                    for item in value.get("output_columns", ())
                ),
                projections=tuple(
                    StructuralProjection(
                        output_column_key=output_keys[
                            str(item["output_column_key"])
                        ],
                        source_dataset_id=bindings[
                            str(item["source_dataset_id"])
                        ],
                        source_column_key=bindings[
                            str(item["source_column_key"])
                        ],
                    )
                    for item in value.get("projections", ())
                ),
                kind=JoinKind(kind if kind in {"LEFT", "INNER"} else value.get("join_kind", "LEFT")),
                require_all_right_rows=bool(
                    value.get("require_all_right_rows", True)
                ),
            )
        if kind == "union_all" or "branches" in value:
            output_keys = {
                str(item["column_key"]): self._application_column_key(
                    str(item["column_key"])
                )
                for item in value.get("output_columns", ())
            }
            return UnionAllRule(
                rule_id=rule_id,
                output_dataset_name=str(value["output_dataset_name"]),
                output_columns=tuple(
                    StructuralOutputColumn(
                        column_key=output_keys[str(item["column_key"])],
                        source_name=str(item["source_name"]),
                        candidate_type=str(item["candidate_type"]),
                    )
                    for item in value.get("output_columns", ())
                ),
                branches=tuple(
                    UnionBranch(
                        source_dataset_id=bindings[
                            str(branch["source_dataset_id"])
                        ],
                        projections=tuple(
                            StructuralProjection(
                                output_column_key=output_keys[
                                    str(item["output_column_key"])
                                ],
                                source_dataset_id=bindings[
                                    str(item["source_dataset_id"])
                                ],
                                source_column_key=bindings[
                                    str(item["source_column_key"])
                                ],
                            )
                            for item in branch.get("projections", ())
                        ),
                    )
                    for branch in value.get("branches", ())
                ),
            )
        if kind == "group_aggregate" or "aggregates" in value:
            output_keys = {
                str(item["column_key"]): self._application_column_key(
                    str(item["column_key"])
                )
                for item in value.get("output_columns", ())
            }
            return GroupAggregateRule(
                rule_id=rule_id,
                output_dataset_name=str(value["output_dataset_name"]),
                source_dataset_id=bindings[str(value["source_dataset_id"])],
                output_columns=tuple(
                    StructuralOutputColumn(
                        column_key=output_keys[str(item["column_key"])],
                        source_name=str(item["source_name"]),
                        candidate_type=str(item["candidate_type"]),
                    )
                    for item in value.get("output_columns", ())
                ),
                group_keys=tuple(
                    GroupKey(
                        output_column_key=output_keys[
                            str(item["output_column_key"])
                        ],
                        source_column_key=bindings[
                            str(item["source_column_key"])
                        ],
                        value_type=str(item.get("value_type", "string")),
                    )
                    for item in value.get("group_keys", ())
                ),
                aggregates=tuple(
                    AggregateSpec(
                        output_column_key=output_keys[
                            str(item["output_column_key"])
                        ],
                        operation=str(item["operation"]),
                        source_column_key=(
                            bindings[str(item["source_column_key"])]
                            if item.get("source_column_key") is not None
                            else None
                        ),
                        unit=str(item.get("unit", "")),
                    )
                    for item in value.get("aggregates", ())
                ),
            )
        raise RecipeApplicationError(
            "Recipe source preparation contains an unsupported rule"
        )

    @staticmethod
    def _application_column_key(logical_id: str) -> str:
        return f"recipe:{content_hash(logical_id)[7:39]}"

    @staticmethod
    def _effective_bindings(selection, base_bindings):
        bindings = dict(base_bindings)
        used_dataset_ids = {
            value
            for logical, value in bindings.items()
            if logical.startswith("dataset:")
        }
        for dataset in selection.datasets:
            if dataset.dataset_id in used_dataset_ids:
                continue
            logical_dataset = f"dataset:{_recipe_token(dataset.name)}"
            if logical_dataset in bindings and bindings[logical_dataset] != dataset.dataset_id:
                raise RecipeApplicationError(
                    "Prepared datasets have ambiguous reusable names"
                )
            bindings[logical_dataset] = dataset.dataset_id
            for column in dataset.columns:
                logical_column = (
                    f"column:{_recipe_token(dataset.name)}."
                    f"{_recipe_token(column.source_name)}"
                )
                if logical_column in bindings and bindings[logical_column] != column.stable_key:
                    raise RecipeApplicationError(
                        "Prepared columns have ambiguous reusable names"
                    )
                bindings[logical_column] = column.stable_key
        return bindings

    def _mapping_datasets(self, definition, bindings, selection, controls, references):
        reference_by_logical = {}
        if references:
            by_name = {item.name: item for item in references.datasets}
            for item in dict(definition["reference_dependencies"]).get("references", ()):
                reference_by_logical[str(item["logical_reference_id"])] = by_name[str(item["name"])]
        controls_by_dataset: dict[str, list] = {}
        for item in dict(definition["control_definitions"]).get("controls", ()):
            controls_by_dataset.setdefault(str(item["dataset_id"]), []).append(item)
        result = []
        for dataset in dict(definition["mapping"]).get("datasets", ()):
            logical_dataset = str(dataset["logical_dataset_id"])
            physical_dataset = bindings[logical_dataset]
            fields = tuple(self._field(item, bindings, reference_by_logical) for item in dataset.get("fields", ()))
            relationships = tuple(self._relationship(item, bindings) for item in dataset.get("relationships", ()))
            definitions = tuple(
                BusinessControlDefinition(
                    control_id=str(item["logical_control_id"]),
                    name=str(item["name"]),
                    target_field=str(item["target_field"]),
                    unit=str(item.get("unit", "")),
                    tolerance=str(item.get("tolerance", "0")),
                    calculation=str(item.get("calculation", "SUM")),
                    invariant_expectation=bool(item.get("invariant_expectation")),
                )
                for item in controls_by_dataset.get(logical_dataset, ())
            )
            expectations = tuple(
                MappingControlExpectation(control_id=item.control_id, expected_total=str(controls.values[item.control_id]))
                for item in definitions
                if controls is not None and item.control_id in controls.values
            )
            result.append(DatasetMapping(
                dataset_id=physical_dataset,
                target_model=str(dataset["target_model"]),
                mode=MappingTargetMode(str(dataset["mode"]).casefold()),
                on_existing=(str(dataset["on_existing"]) if dataset.get("on_existing") is not None else None),
                source_identity_column_keys=tuple(bindings[str(value)] for value in dataset.get("source_identity_column_ids", ())),
                target_identity=tuple(self._identity(item, bindings) for item in dataset.get("identity", ())),
                target_scope=tuple(self._identity(item, bindings) for item in dataset.get("scope", ())),
                fields=fields,
                relationships=relationships,
                target_field_dispositions=tuple(TargetFieldDisposition(target_field=str(item["target_field"]), handling=TargetFieldHandling(str(item["handling"]))) for item in dataset.get("target_field_dispositions", ())),
                approved_write_fields=tuple(str(item) for item in dataset.get("approved_write_fields", ())),
                control_definitions=definitions,
                control_expectations=expectations,
            ))
        return tuple(result)

    def _field(self, item, bindings, references):
        provider = dict(item["provider"])
        source_ids = tuple(bindings[str(value)] for value in provider.get("source_column_ids", ()))
        reference = None
        kind = str(provider["kind"])
        value_source = ScalarValueSource(kind.casefold()) if kind != "REFERENCE_LOOKUP" else ScalarValueSource.SOURCE
        if kind == "REFERENCE_LOOKUP":
            current = references[str(provider["logical_reference_id"])]
            reference = ReferenceLookupMapping(
                reference_id=current.reference_id,
                reference_content_hash=current.content_hash,
                key_source_column_keys=source_ids,
                value_field=str(provider["value_field"]),
                on_blank=str(provider["on_blank"]),
                on_unknown=str(provider["on_unknown"]),
            )
        transform = dict(item.get("transform", {}))
        validation = dict(item.get("validation", {}))
        return ScalarFieldMapping(
            target_field=str(item["target_field"]),
            source_column_key=(source_ids[0] if source_ids else None),
            value_source=value_source,
            literal_value=(str(provider["literal_value"]) if provider.get("literal_value") is not None else None),
            transform=ScalarTransformPolicy(
                **{key: value for key, value in transform.items() if key != "text_steps"},
                text_steps=tuple(TextTransformStep(**dict(value)) for value in transform.get("text_steps", ())),
            ),
            validation=ScalarValidationPolicy(**validation),
            value_mappings=tuple(ValueMapping(source_value=str(value["source_value"]), target_value=str(value["target_value"])) for value in item.get("value_matches", ())),
            value_type=str(item.get("value_type", "string")),
            required=bool(item.get("required")),
            required_on_create=bool(item.get("required_on_create")),
            compare=bool(item.get("compare", True)),
            validate_only=bool(item.get("validate_only")),
            null_policy=str(item.get("null_policy", "distinct")),
            reference_lookup=reference,
            categorical_policy=(CategoricalCoveragePolicy(str(item["categorical_policy"])) if item.get("categorical_policy") else None),
        )

    def _identity(self, item, bindings):
        resolver = dict(item["resolver"]) if item.get("resolver") else None
        return IdentityComponentMapping(
            source_column_keys=tuple(bindings[str(value)] for value in item.get("source_column_ids", ())),
            target_fields=tuple(str(value) for value in item.get("target_fields", ())),
            value_type=str(item.get("value_type", "string")),
            resolver=(self._resolver(resolver, bindings) if resolver else None),
        )

    def _relationship(self, item, bindings):
        resolver_payload = {
            "origin": item.get("target_dataset_id") and "dataset" or "target_catalog",
            "target_dataset_id": item.get("target_dataset_id"),
            "target_model": item.get("target_model"),
            "target_key_mappings": item.get("target_key_mappings", ()),
            "target_scope_mappings": item.get("target_scope_mappings", ()),
            "value_matches": item.get("value_matches", ()),
        }
        return RelationshipMapping(
            target_field=str(item["target_field"]),
            kind=str(item["kind"]),
            source_column_keys=tuple(bindings[str(value)] for value in item.get("source_column_ids", ())),
            resolver=self._resolver(resolver_payload, bindings),
            compare=bool(item.get("compare", True)),
            validate_only=bool(item.get("validate_only")),
            required=bool(item.get("required")),
            required_on_create=bool(item.get("required_on_create")),
            on_missing=str(item.get("on_missing", "error")),
            on_ambiguous=str(item.get("on_ambiguous", "error")),
            operation=str(item.get("operation", "replace")),
            separator=str(item.get("separator", ";")),
            null_policy=str(item.get("null_policy", "distinct")),
            categorical_policy=(CategoricalCoveragePolicy(str(item["categorical_policy"])) if item.get("categorical_policy") else None),
        )

    def _resolver(self, payload, bindings):
        return RelationshipResolver(
            origin=ResolverOrigin(str(payload["origin"])),
            dataset_id=(bindings[str(payload["target_dataset_id"])] if payload.get("target_dataset_id") else None),
            model=(str(payload["target_model"]) if payload.get("target_model") else None),
            key_mappings=tuple(ReferenceKeyMapping(source_column_key=bindings[str(item["source_column_id"])], target_field=str(item["target_field"])) for item in payload.get("target_key_mappings", ())),
            scope_mappings=tuple(ReferenceKeyMapping(source_column_key=bindings[str(item["source_column_id"])], target_field=str(item["target_field"])) for item in payload.get("target_scope_mappings", ())),
            value_mappings=tuple(ValueMapping(source_value=str(item["source_value"]), target_value=str(item["target_value"])) for item in payload.get("value_matches", ())),
        )

    def _source_artifact_hash(self, project_id):
        selection = self.sources.get_source_selection(project_id)
        if selection is None:
            return content_hash({"source": "missing"})
        return content_hash(sorted(item.source_evidence_hash for item in selection.datasets))

    @staticmethod
    def _reused_rule_count(definition):
        mapping = dict(definition["mapping"])
        return sum(len(item.get("fields", ())) + len(item.get("relationships", ())) for item in mapping.get("datasets", ()))

    def _save_review_draft(self, review, *, actor, expected):
        now = datetime.now(timezone.utc)
        binding_hash = content_hash({"overrides": dict(sorted(review.overrides.items())), "source_bindings": dict(sorted(review.source_bindings.items()))})
        draft = RecipeApplicationDraft(
            application_id=(expected.application_id if expected else str(uuid4())),
            recipe_id=review.recipe_id,
            recipe_revision=review.recipe_revision,
            data_version_id=review.data_version.data_version_id,
            workspace_project_id=review.project.project_id,
            target_binding_hash=(review.target_binding.content_hash if review.target_binding else content_hash({"target": "pending"})),
            source_selection_hash=(self.sources.get_mapping_source_selection(review.project.project_id).content_hash if self.sources.get_mapping_source_selection(review.project.project_id) else content_hash({"source": "pending"})),
            parameter_values_hash=(review.parameter_values.content_hash if review.parameter_values else content_hash({"parameters": "pending"})),
            revision=(expected.revision + 1 if expected else 1),
            state=review.state,
            overrides=review.overrides,
            issues=review.issues,
            binding_hash=binding_hash,
            target_assessment_hash=review.target_assessment_hash,
            updated_at=now,
            updated_by=actor.identity,
        )
        self.applications.save_draft(review.project.project_id, draft, expected_revision=(expected.revision if expected else None), actor=actor)

    @staticmethod
    def _block(code, message, recovery, logical_id=""):
        return RecipeApplicationIssue(code, RecipeApplicationIssueLevel.BLOCKER, message, recovery, logical_id)

    @staticmethod
    def _info(code, message, recovery, logical_id=""):
        return RecipeApplicationIssue(code, RecipeApplicationIssueLevel.INFORMATION, message, recovery, logical_id)


def _recipe_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    token = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    if not token:
        raise RecipeApplicationError(
            "Reusable source names must contain a letter or number"
        )
    return token[:120]
