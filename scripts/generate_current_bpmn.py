"""Generate the normative BPMN 2.0 diagrams for Impodo's current workflows.

The generated files are documentation artifacts. They are deliberately
non-executable BPMN and use only standard BPMN 2.0 elements plus BPMN DI.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from pathlib import Path
import xml.etree.ElementTree as ET


BPMN = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMNDI = "http://www.omg.org/spec/BPMN/20100524/DI"
DC = "http://www.omg.org/spec/DD/20100524/DC"
DI = "http://www.omg.org/spec/DD/20100524/DI"
XSI = "http://www.w3.org/2001/XMLSchema-instance"

for prefix, namespace in (
    ("bpmn", BPMN),
    ("bpmndi", BPMNDI),
    ("dc", DC),
    ("di", DI),
    ("xsi", XSI),
):
    ET.register_namespace(prefix, namespace)


def q(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


@dataclass(frozen=True)
class Node:
    id: str
    kind: str
    name: str
    x: int
    y: int
    width: int = 150
    height: int = 70
    lane: str | None = None
    called_element: str | None = None
    attached_to: str | None = None
    event_definition: str | None = None


@dataclass(frozen=True)
class Flow:
    id: str
    source: str
    target: str
    name: str = ""
    waypoints: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class MessageFlow:
    id: str
    source: str
    target: str
    name: str


@dataclass(frozen=True)
class ProcessSpec:
    slug: str
    process_id: str
    name: str
    nodes: tuple[Node, ...]
    flows: tuple[Flow, ...]
    lanes: tuple[tuple[str, str], ...] = (
        ("Lane_DataManager", "Data manager"),
        ("Lane_Impodo", "Impodo"),
    )
    message_flows: tuple[MessageFlow, ...] = ()
    documentation: str = ""


def event(event_id: str, kind: str, name: str, x: int, y: int, lane: str) -> Node:
    return Node(event_id, kind, name, x, y, 36, 36, lane)


def gateway(gateway_id: str, name: str, x: int, y: int, lane: str) -> Node:
    return Node(gateway_id, "exclusiveGateway", name, x, y, 50, 50, lane)


def task(task_id: str, kind: str, name: str, x: int, y: int, lane: str) -> Node:
    return Node(task_id, kind, name, x, y, 150, 70, lane)


def call(call_id: str, name: str, called: str, x: int, y: int) -> Node:
    return Node(call_id, "callActivity", name, x, y, 170, 80, None, called)


def build_specs() -> tuple[ProcessSpec, ...]:
    master = ProcessSpec(
        slug="impodo-current-workflow",
        process_id="Process_ImpodoCurrentWorkflow",
        name="Impodo current browser workflow",
        lanes=(),
        documentation=(
            "High-level current workflow. File-source projects continue through "
            "mapping, preparation, final review, disposable-target loading, and "
            "reconciliation. Odoo-source projects currently stop after bounded "
            "source capture. Project-level integrated Test planning and exact "
            "qualification are modelled separately because they consume "
            "published Recipes and a new accepted Test DataVersion rather than "
            "extending one authoring workspace."
        ),
        nodes=(
            Node("Start_Current", "startEvent", "Migration project needed", 80, 190, 36, 36),
            call("Call_Setup", "Project setup", "Process_ProjectSetup", 160, 168),
            Node("Gateway_SourceMode", "exclusiveGateway", "Source mode?", 380, 183, 50, 50),
            call("Call_FileSource", "Source data", "Process_SourceData", 500, 80),
            call("Call_FileOdooData", "Odoo data", "Process_OdooData", 720, 80),
            call("Call_Match", "Match data", "Process_MatchData", 940, 80),
            call("Call_Prepare", "Prepare data", "Process_PrepareData", 1160, 80),
            call("Call_Review", "Final review", "Process_FinalReview", 1380, 80),
            call("Call_Load", "Load into Odoo", "Process_LoadOdoo", 1600, 80),
            Node("End_File", "endEvent", "Reconciled or needs attention", 1820, 102, 36, 36),
            call("Call_OdooDataSource", "Odoo source data", "Process_OdooData", 500, 290),
            call("Call_OdooCapture", "Freeze Odoo records", "Process_SourceData", 740, 290),
            Node("End_OdooBoundary", "endEvent", "Current Odoo-source boundary", 980, 312, 36, 36),
        ),
        flows=(
            Flow("Flow_M_01", "Start_Current", "Call_Setup"),
            Flow("Flow_M_02", "Call_Setup", "Gateway_SourceMode"),
            Flow("Flow_M_03", "Gateway_SourceMode", "Call_FileSource", "Files"),
            Flow("Flow_M_04", "Call_FileSource", "Call_FileOdooData"),
            Flow("Flow_M_05", "Call_FileOdooData", "Call_Match"),
            Flow("Flow_M_06", "Call_Match", "Call_Prepare"),
            Flow("Flow_M_07", "Call_Prepare", "Call_Review"),
            Flow("Flow_M_08", "Call_Review", "Call_Load"),
            Flow("Flow_M_09", "Call_Load", "End_File"),
            Flow("Flow_M_10", "Gateway_SourceMode", "Call_OdooDataSource", "Odoo", ((405, 233), (405, 330), (500, 330))),
            Flow("Flow_M_11", "Call_OdooDataSource", "Call_OdooCapture"),
            Flow("Flow_M_12", "Call_OdooCapture", "End_OdooBoundary"),
        ),
    )

    setup = ProcessSpec(
        slug="00-project-setup",
        process_id="Process_ProjectSetup",
        name="Project setup",
        documentation="Current project creation and registration workflow.",
        nodes=(
            event("Start_Setup", "startEvent", "New project selected", 80, 130, "Lane_DataManager"),
            task("Task_SourceMode", "userTask", "Choose file or Odoo source", 150, 110, "Lane_DataManager"),
            task("Task_Governance", "userTask", "Enter purpose, owners, classification, and retention", 350, 110, "Lane_DataManager"),
            task("Task_Target", "userTask", "Configure exact Local or Remote Odoo target", 550, 110, "Lane_DataManager"),
            gateway("Gateway_TestConnection", "Test read connection?", 760, 120, "Lane_DataManager"),
            task("Task_TestConnection", "serviceTask", "Test narrow read-only Odoo connection", 860, 340, "Lane_Impodo"),
            gateway("Gateway_FileMode", "File source?", 1060, 120, "Lane_DataManager"),
            task("Task_AddFiles", "userTask", "Add governed CSV or XLSX files", 1160, 90, "Lane_DataManager"),
            task("Task_ReviewSetup", "userTask", "Review complete setup", 1360, 110, "Lane_DataManager"),
            task("Task_ValidateSetup", "serviceTask", "Validate ownership, governance, source, and target", 1560, 340, "Lane_Impodo"),
            gateway("Gateway_SetupValid", "Registration valid?", 1770, 350, "Lane_Impodo"),
            task("Task_CorrectSetup", "userTask", "Correct setup", 1730, 110, "Lane_DataManager"),
            task("Task_Register", "serviceTask", "Register boundary, increment revision, and audit", 1880, 340, "Lane_Impodo"),
            event("End_Registered", "endEvent", "Project registered", 2080, 357, "Lane_Impodo"),
        ),
        flows=(
            Flow("Flow_S_01", "Start_Setup", "Task_SourceMode"),
            Flow("Flow_S_02", "Task_SourceMode", "Task_Governance"),
            Flow("Flow_S_03", "Task_Governance", "Task_Target"),
            Flow("Flow_S_04", "Task_Target", "Gateway_TestConnection"),
            Flow("Flow_S_05", "Gateway_TestConnection", "Task_TestConnection", "Yes"),
            Flow("Flow_S_06", "Gateway_TestConnection", "Gateway_FileMode", "No", ((785, 145), (1085, 145))),
            Flow("Flow_S_07", "Task_TestConnection", "Gateway_FileMode"),
            Flow("Flow_S_08", "Gateway_FileMode", "Task_AddFiles", "Yes"),
            Flow("Flow_S_09", "Gateway_FileMode", "Task_ReviewSetup", "No", ((1085, 170), (1085, 230), (1435, 230), (1435, 180))),
            Flow("Flow_S_10", "Task_AddFiles", "Task_ReviewSetup"),
            Flow("Flow_S_11", "Task_ReviewSetup", "Task_ValidateSetup"),
            Flow("Flow_S_12", "Task_ValidateSetup", "Gateway_SetupValid"),
            Flow("Flow_S_13", "Gateway_SetupValid", "Task_Register", "Yes"),
            Flow("Flow_S_14", "Gateway_SetupValid", "Task_CorrectSetup", "No"),
            Flow("Flow_S_15", "Task_CorrectSetup", "Task_ReviewSetup", "Retry", ((1730, 145), (1660, 145), (1660, 80), (1435, 80), (1435, 110))),
            Flow("Flow_S_16", "Task_Register", "End_Registered"),
        ),
        message_flows=(
            MessageFlow("Message_S_Test", "Task_TestConnection", "Participant_Odoo", "Read-only connection probe"),
            MessageFlow("Message_S_TestResponse", "Participant_Odoo", "Task_TestConnection", "Probe response"),
        ),
    )

    source = ProcessSpec(
        slug="01-source-data",
        process_id="Process_SourceData",
        name="Source data",
        documentation="Current file-source freeze and bounded Odoo-source capture variants.",
        nodes=(
            event("Start_Source", "startEvent", "Source responsibility opened", 70, 130, "Lane_DataManager"),
            gateway("Gateway_SourceVariant", "Source mode?", 150, 120, "Lane_DataManager"),
            task("Task_CheckFiles", "userTask", "Select Check source files", 260, 80, "Lane_DataManager"),
            task("Task_InspectFiles", "serviceTask", "Inspect structure, counts, types, samples, and warnings", 460, 330, "Lane_Impodo"),
            task("Task_ReviewFiles", "userTask", "Review inspection and configure intended tables", 660, 80, "Lane_DataManager"),
            gateway("Gateway_FilesCorrect", "Source correct?", 870, 90, "Lane_DataManager"),
            task("Task_ReplaceFile", "userTask", "Replace incorrect file before freeze", 830, 190, "Lane_DataManager"),
            task("Task_FreezeTables", "serviceTask", "Freeze selected tables and publish source evidence", 980, 330, "Lane_Impodo"),
            gateway("Gateway_RelatedPlan", "Prepare related datasets?", 1180, 90, "Lane_DataManager"),
            task("Task_RelatedPlan", "userTask", "Define related datasets", 1290, 80, "Lane_DataManager"),
            event("End_FileSource", "endEvent", "File source complete", 1490, 97, "Lane_DataManager"),
            task("Task_DefineOdooSelection", "userTask", "Define bounded Odoo record selection", 300, 190, "Lane_DataManager"),
            task("Task_ReviewEstimate", "userTask", "Review estimate and selection rules", 500, 190, "Lane_DataManager"),
            task("Task_CaptureOdoo", "serviceTask", "Read selected Odoo records", 700, 420, "Lane_Impodo"),
            task("Task_PublishOdooSource", "serviceTask", "Publish immutable Odoo source snapshot", 920, 420, "Lane_Impodo"),
            event("End_OdooSource", "endEvent", "Current Odoo-source boundary", 1140, 437, "Lane_Impodo"),
        ),
        flows=(
            Flow("Flow_SRC_01", "Start_Source", "Gateway_SourceVariant"),
            Flow("Flow_SRC_02", "Gateway_SourceVariant", "Task_CheckFiles", "Files"),
            Flow("Flow_SRC_03", "Task_CheckFiles", "Task_InspectFiles"),
            Flow("Flow_SRC_04", "Task_InspectFiles", "Task_ReviewFiles"),
            Flow("Flow_SRC_05", "Task_ReviewFiles", "Gateway_FilesCorrect"),
            Flow("Flow_SRC_06", "Gateway_FilesCorrect", "Task_ReplaceFile", "No"),
            Flow("Flow_SRC_07", "Task_ReplaceFile", "Task_CheckFiles", "Recheck", ((830, 225), (600, 225), (600, 60), (335, 60), (335, 80))),
            Flow("Flow_SRC_08", "Gateway_FilesCorrect", "Task_FreezeTables", "Yes"),
            Flow("Flow_SRC_09", "Task_FreezeTables", "Gateway_RelatedPlan"),
            Flow("Flow_SRC_10", "Gateway_RelatedPlan", "Task_RelatedPlan", "Yes"),
            Flow("Flow_SRC_10B", "Gateway_RelatedPlan", "End_FileSource", "No", ((1205, 140), (1205, 180), (1508, 180), (1508, 133))),
            Flow("Flow_SRC_10C", "Task_RelatedPlan", "End_FileSource"),
            Flow("Flow_SRC_11", "Gateway_SourceVariant", "Task_DefineOdooSelection", "Odoo", ((175, 170), (175, 225), (300, 225))),
            Flow("Flow_SRC_12", "Task_DefineOdooSelection", "Task_ReviewEstimate"),
            Flow("Flow_SRC_13", "Task_ReviewEstimate", "Task_CaptureOdoo"),
            Flow("Flow_SRC_14", "Task_CaptureOdoo", "Task_PublishOdooSource"),
            Flow("Flow_SRC_15", "Task_PublishOdooSource", "End_OdooSource"),
        ),
        message_flows=(
            MessageFlow("Message_SRC_Read", "Task_CaptureOdoo", "Participant_Odoo", "Bounded record read"),
            MessageFlow("Message_SRC_Response", "Participant_Odoo", "Task_CaptureOdoo", "Selected records"),
        ),
    )

    odoo_data = ProcessSpec(
        slug="02-odoo-data",
        process_id="Process_OdooData",
        name="Odoo data",
        documentation="Current read-only Odoo model, field, and identity governance workflow.",
        nodes=(
            event("Start_OdooData", "startEvent", "Odoo data opened", 70, 130, "Lane_DataManager"),
            task("Task_ShowModels", "userTask", "Select Show available Odoo data", 140, 110, "Lane_DataManager"),
            task("Task_ReadCatalog", "serviceTask", "Read bounded Odoo model catalogue", 340, 330, "Lane_Impodo"),
            task("Task_SelectModels", "userTask", "Choose exact record types in scope", 540, 110, "Lane_DataManager"),
            task("Task_ReadFields", "serviceTask", "Read selected fields and relationships", 740, 330, "Lane_Impodo"),
            task("Task_ReviewSchema", "userTask", "Review fields, types, requirements, selections, and relations", 940, 110, "Lane_DataManager"),
            gateway("Gateway_OdooSourceMode", "Source mode?", 1150, 120, "Lane_DataManager"),
            task("Task_BusinessKeys", "userTask", "Choose portable business keys", 1260, 80, "Lane_DataManager"),
            task("Task_ConfirmSchema", "serviceTask", "Confirm schema and matching governance", 1460, 330, "Lane_Impodo"),
            event("End_FileOdooData", "endEvent", "Match data available", 1660, 347, "Lane_Impodo"),
            task("Task_EligibleFields", "userTask", "Confirm eligible capture fields", 1260, 190, "Lane_DataManager"),
            task("Task_SaveCapturePlan", "serviceTask", "Save bounded capture-plan revision", 1460, 420, "Lane_Impodo"),
            event("End_OdooCapturePlan", "endEvent", "Freeze Odoo records next", 1660, 437, "Lane_Impodo"),
        ),
        flows=(
            Flow("Flow_OD_01", "Start_OdooData", "Task_ShowModels"),
            Flow("Flow_OD_02", "Task_ShowModels", "Task_ReadCatalog"),
            Flow("Flow_OD_03", "Task_ReadCatalog", "Task_SelectModels"),
            Flow("Flow_OD_04", "Task_SelectModels", "Task_ReadFields"),
            Flow("Flow_OD_05", "Task_ReadFields", "Task_ReviewSchema"),
            Flow("Flow_OD_06", "Task_ReviewSchema", "Gateway_OdooSourceMode"),
            Flow("Flow_OD_07", "Gateway_OdooSourceMode", "Task_BusinessKeys", "Files"),
            Flow("Flow_OD_08", "Task_BusinessKeys", "Task_ConfirmSchema"),
            Flow("Flow_OD_09", "Task_ConfirmSchema", "End_FileOdooData"),
            Flow("Flow_OD_10", "Gateway_OdooSourceMode", "Task_EligibleFields", "Odoo"),
            Flow("Flow_OD_11", "Task_EligibleFields", "Task_SaveCapturePlan"),
            Flow("Flow_OD_12", "Task_SaveCapturePlan", "End_OdooCapturePlan"),
        ),
        message_flows=(
            MessageFlow("Message_OD_Catalog", "Task_ReadCatalog", "Participant_Odoo", "Read model metadata"),
            MessageFlow("Message_OD_CatalogResponse", "Participant_Odoo", "Task_ReadCatalog", "Model catalogue"),
            MessageFlow("Message_OD_Fields", "Task_ReadFields", "Participant_Odoo", "Read fields_get metadata"),
            MessageFlow("Message_OD_FieldsResponse", "Participant_Odoo", "Task_ReadFields", "Field metadata"),
        ),
    )

    match = ProcessSpec(
        slug="03-match-data",
        process_id="Process_MatchData",
        name="Match data",
        documentation="Current file-source mapping authoring, validation, impact review, and confirmation workflow.",
        nodes=(
            event("Start_Match", "startEvent", "Source and Odoo data complete", 70, 130, "Lane_DataManager"),
            task("Task_DatasetMode", "userTask", "Choose dataset target mode and identity", 140, 110, "Lane_DataManager"),
            task("Task_MapFields", "userTask", "Map writable values and deliberate omissions", 340, 110, "Lane_DataManager"),
            task("Task_MapRules", "userTask", "Configure transformations and selection values", 540, 110, "Lane_DataManager"),
            task("Task_MapRelations", "userTask", "Map relations with portable keys", 740, 110, "Lane_DataManager"),
            task("Task_SaveDraft", "userTask", "Save mapping draft", 940, 110, "Lane_DataManager"),
            task("Task_CheckMapping", "serviceTask", "Validate exact mapping revision", 1140, 330, "Lane_Impodo"),
            task("Task_Impact", "serviceTask", "Publish transformation impact", 1340, 330, "Lane_Impodo"),
            task("Task_ReviewImpact", "userTask", "Review findings and rule effects", 1540, 110, "Lane_DataManager"),
            gateway("Gateway_MappingValid", "Blocking finding?", 1750, 120, "Lane_DataManager"),
            task("Task_ReviseMapping", "userTask", "Revise mapping", 1710, 210, "Lane_DataManager"),
            task("Task_ConfirmMapping", "userTask", "Confirm field matches", 1860, 110, "Lane_DataManager"),
            task("Task_PublishMapping", "serviceTask", "Publish immutable confirmed revision", 2060, 330, "Lane_Impodo"),
            event("End_Match", "endEvent", "Match data complete", 2260, 347, "Lane_Impodo"),
        ),
        flows=(
            Flow("Flow_MAP_01", "Start_Match", "Task_DatasetMode"),
            Flow("Flow_MAP_02", "Task_DatasetMode", "Task_MapFields"),
            Flow("Flow_MAP_03", "Task_MapFields", "Task_MapRules"),
            Flow("Flow_MAP_04", "Task_MapRules", "Task_MapRelations"),
            Flow("Flow_MAP_05", "Task_MapRelations", "Task_SaveDraft"),
            Flow("Flow_MAP_06", "Task_SaveDraft", "Task_CheckMapping"),
            Flow("Flow_MAP_07", "Task_CheckMapping", "Task_Impact"),
            Flow("Flow_MAP_08", "Task_Impact", "Task_ReviewImpact"),
            Flow("Flow_MAP_09", "Task_ReviewImpact", "Gateway_MappingValid"),
            Flow("Flow_MAP_10", "Gateway_MappingValid", "Task_ReviseMapping", "Yes"),
            Flow("Flow_MAP_11", "Task_ReviseMapping", "Task_SaveDraft", "Recheck", ((1710, 245), (1450, 245), (1450, 70), (1015, 70), (1015, 110))),
            Flow("Flow_MAP_12", "Gateway_MappingValid", "Task_ConfirmMapping", "No"),
            Flow("Flow_MAP_13", "Task_ConfirmMapping", "Task_PublishMapping"),
            Flow("Flow_MAP_14", "Task_PublishMapping", "End_Match"),
        ),
    )

    prepare = ProcessSpec(
        slug="04-prepare-data",
        process_id="Process_PrepareData",
        name="Prepare data",
        documentation="Current canonical preparation, quality, duplicate resolution, normalization, and approval workflow.",
        nodes=(
            event("Start_Prepare", "startEvent", "Confirmed mapping current", 70, 130, "Lane_DataManager"),
            task("Task_StartPrepare", "userTask", "Select Prepare data", 140, 110, "Lane_DataManager"),
            task("Task_RunPrepare", "serviceTask", "Prepare every frozen row", 340, 330, "Lane_Impodo"),
            gateway("Gateway_JobOutcome", "Job outcome?", 550, 340, "Lane_Impodo"),
            task("Task_InspectFailure", "userTask", "Understand failed or cancelled attempt", 510, 190, "Lane_DataManager"),
            task("Task_PublishCanonical", "serviceTask", "Publish canonical and prepared evidence", 660, 330, "Lane_Impodo"),
            task("Task_Quality", "serviceTask", "Evaluate quality and complete row accounting", 860, 330, "Lane_Impodo"),
            task("Task_ReviewPrepare", "userTask", "Review totals, warnings, quarantine, and failures", 1080, 110, "Lane_DataManager"),
            gateway("Gateway_ResolutionNeeded", "Resolution required?", 1290, 120, "Lane_DataManager"),
            task("Task_Resolve", "userTask", "Resolve duplicates and normalization decisions", 1400, 190, "Lane_DataManager"),
            task("Task_Reprepare", "serviceTask", "Regenerate affected prepared evidence", 1600, 420, "Lane_Impodo"),
            task("Task_ApprovePrepared", "userTask", "Approve resolved prepared data", 1700, 80, "Lane_DataManager"),
            task("Task_FreezePrepared", "serviceTask", "Advance current prepared pointer", 1900, 330, "Lane_Impodo"),
            event("End_Prepare", "endEvent", "Prepare data complete", 2100, 347, "Lane_Impodo"),
        ),
        flows=(
            Flow("Flow_PREP_01", "Start_Prepare", "Task_StartPrepare"),
            Flow("Flow_PREP_02", "Task_StartPrepare", "Task_RunPrepare"),
            Flow("Flow_PREP_03", "Task_RunPrepare", "Gateway_JobOutcome"),
            Flow("Flow_PREP_04", "Gateway_JobOutcome", "Task_InspectFailure", "Failed or cancelled"),
            Flow("Flow_PREP_05", "Task_InspectFailure", "Task_StartPrepare", "Retry after understanding", ((510, 225), (430, 225), (430, 70), (215, 70), (215, 110))),
            Flow("Flow_PREP_06", "Gateway_JobOutcome", "Task_PublishCanonical", "Succeeded"),
            Flow("Flow_PREP_07", "Task_PublishCanonical", "Task_Quality"),
            Flow("Flow_PREP_08", "Task_Quality", "Task_ReviewPrepare"),
            Flow("Flow_PREP_09", "Task_ReviewPrepare", "Gateway_ResolutionNeeded"),
            Flow("Flow_PREP_10", "Gateway_ResolutionNeeded", "Task_ApprovePrepared", "No"),
            Flow("Flow_PREP_11", "Gateway_ResolutionNeeded", "Task_Resolve", "Yes"),
            Flow("Flow_PREP_12", "Task_Resolve", "Task_Reprepare"),
            Flow("Flow_PREP_13", "Task_Reprepare", "Task_Quality", "Re-evaluate", ((1600, 455), (1500, 455), (1500, 500), (935, 500), (935, 400))),
            Flow("Flow_PREP_15", "Task_ApprovePrepared", "Task_FreezePrepared"),
            Flow("Flow_PREP_16", "Task_FreezePrepared", "End_Prepare"),
        ),
    )

    review = ProcessSpec(
        slug="05-final-review",
        process_id="Process_FinalReview",
        name="Final review",
        documentation="Current read-only target comparison and execution-snapshot freeze workflow.",
        nodes=(
            event("Start_Review", "startEvent", "Prepared data complete", 70, 130, "Lane_DataManager"),
            task("Task_CheckRows", "userTask", "Select Check all rows", 140, 110, "Lane_DataManager"),
            task("Task_ReadTarget", "serviceTask", "Read bounded current Odoo evidence", 340, 330, "Lane_Impodo"),
            task("Task_Classify", "serviceTask", "Match identities and classify every eligible row", 540, 330, "Lane_Impodo"),
            task("Task_ReviewDiffs", "userTask", "Review totals, field differences, and relationships", 740, 110, "Lane_DataManager"),
            gateway("Gateway_Ready", "Rows clear?", 950, 90, "Lane_DataManager"),
            gateway("Gateway_Expected", "Actions expected?", 1080, 90, "Lane_DataManager"),
            task("Task_ReturnUpstream", "userTask", "Return to earliest affected upstream stage", 920, 190, "Lane_DataManager"),
            event("End_NeedsAttention", "endEvent", "Needs attention", 1120, 207, "Lane_DataManager"),
            task("Task_FreezeExecution", "serviceTask", "Freeze exact actions, fields, dependencies, target, and hashes", 1230, 330, "Lane_Impodo"),
            event("End_Ready", "endEvent", "READY; load stage available", 1450, 347, "Lane_Impodo"),
        ),
        flows=(
            Flow("Flow_REV_01", "Start_Review", "Task_CheckRows"),
            Flow("Flow_REV_02", "Task_CheckRows", "Task_ReadTarget"),
            Flow("Flow_REV_03", "Task_ReadTarget", "Task_Classify"),
            Flow("Flow_REV_04", "Task_Classify", "Task_ReviewDiffs"),
            Flow("Flow_REV_05", "Task_ReviewDiffs", "Gateway_Ready"),
            Flow("Flow_REV_06", "Gateway_Ready", "Task_ReturnUpstream", "No", ((975, 140), (975, 190))),
            Flow("Flow_REV_07", "Gateway_Ready", "Gateway_Expected", "Yes"),
            Flow("Flow_REV_08", "Gateway_Expected", "Task_ReturnUpstream", "No", ((1105, 140), (1105, 175), (995, 175), (995, 190))),
            Flow("Flow_REV_09", "Task_ReturnUpstream", "End_NeedsAttention"),
            Flow("Flow_REV_10", "Gateway_Expected", "Task_FreezeExecution", "Yes"),
            Flow("Flow_REV_11", "Task_FreezeExecution", "End_Ready"),
        ),
        message_flows=(
            MessageFlow("Message_REV_Read", "Task_ReadTarget", "Participant_Odoo", "Read reviewed model and key scope"),
            MessageFlow("Message_REV_Response", "Participant_Odoo", "Task_ReadTarget", "Current target evidence"),
        ),
    )

    load = ProcessSpec(
        slug="06-load-into-odoo",
        process_id="Process_LoadOdoo",
        name="Load into Odoo",
        documentation="Current disposable-target execution and read-back reconciliation workflow.",
        nodes=(
            event("Start_Load", "startEvent", "Current READY report", 70, 130, "Lane_DataManager"),
            task("Task_ShowPreview", "serviceTask", "Show target, hash, totals, fields, and dependency order", 140, 330, "Lane_Impodo"),
            gateway("Gateway_Writes", "Any writes?", 350, 340, "Lane_Impodo"),
            task("Task_NoWrite", "serviceTask", "Record unchanged completion", 460, 420, "Lane_Impodo"),
            event("End_NoWrite", "endEvent", "Complete; no writes", 660, 437, "Lane_Impodo"),
            task("Task_ReviewLoad", "userTask", "Review preview and provide separate write key", 460, 110, "Lane_DataManager"),
            task("Task_ExplicitLoad", "userTask", "Select Load into Odoo once", 660, 110, "Lane_DataManager"),
            task("Task_Revalidate", "serviceTask", "Revalidate snapshot, target, actor, permissions, and key bindings", 860, 330, "Lane_Impodo"),
            gateway("Gateway_Current", "All bindings current?", 1070, 340, "Lane_Impodo"),
            event("End_Stale", "endEvent", "Stop before target I/O", 1030, 437, "Lane_Impodo"),
            task("Task_Journal", "serviceTask", "Commit run and planned row attempts", 1180, 330, "Lane_Impodo"),
            task("Task_Write", "serviceTask", "Execute bounded dependency-ordered write batch", 1380, 330, "Lane_Impodo"),
            gateway("Gateway_Transport", "Transport outcome?", 1590, 340, "Lane_Impodo"),
            task("Task_RecordAccepted", "serviceTask", "Record accepted result", 1700, 300, "Lane_Impodo"),
            task("Task_RecordRejected", "serviceTask", "Record rejected or partial result", 1700, 390, "Lane_Impodo"),
            task("Task_RecordUnknown", "serviceTask", "Record outcome unknown and stop later writes", 1700, 210, "Lane_Impodo"),
            gateway("Gateway_MoreBatches", "More reviewed batches?", 1910, 340, "Lane_Impodo"),
            task("Task_ReadBack", "serviceTask", "Read back exact affected records", 2040, 330, "Lane_Impodo"),
            task("Task_Reconcile", "serviceTask", "Publish immutable reconciliation result", 2240, 330, "Lane_Impodo"),
            gateway("Gateway_Verified", "Expected target state proved?", 2450, 340, "Lane_Impodo"),
            event("End_Reconciled", "endEvent", "Reconciled complete", 2590, 287, "Lane_Impodo"),
            task("Task_Fallout", "userTask", "Review fallout; do not blindly retry", 2530, 110, "Lane_DataManager"),
            event("End_Fallout", "endEvent", "Needs attention", 2740, 127, "Lane_DataManager"),
        ),
        flows=(
            Flow("Flow_LOAD_01", "Start_Load", "Task_ShowPreview"),
            Flow("Flow_LOAD_02", "Task_ShowPreview", "Gateway_Writes"),
            Flow("Flow_LOAD_03", "Gateway_Writes", "Task_NoWrite", "No"),
            Flow("Flow_LOAD_04", "Task_NoWrite", "End_NoWrite"),
            Flow("Flow_LOAD_05", "Gateway_Writes", "Task_ReviewLoad", "Yes"),
            Flow("Flow_LOAD_06", "Task_ReviewLoad", "Task_ExplicitLoad"),
            Flow("Flow_LOAD_07", "Task_ExplicitLoad", "Task_Revalidate"),
            Flow("Flow_LOAD_08", "Task_Revalidate", "Gateway_Current"),
            Flow("Flow_LOAD_09", "Gateway_Current", "End_Stale", "No"),
            Flow("Flow_LOAD_10", "Gateway_Current", "Task_Journal", "Yes"),
            Flow("Flow_LOAD_11", "Task_Journal", "Task_Write"),
            Flow("Flow_LOAD_12", "Task_Write", "Gateway_Transport"),
            Flow("Flow_LOAD_13", "Gateway_Transport", "Task_RecordAccepted", "Accepted"),
            Flow("Flow_LOAD_14", "Gateway_Transport", "Task_RecordRejected", "Rejected or partial"),
            Flow("Flow_LOAD_15", "Gateway_Transport", "Task_RecordUnknown", "Timeout or lost response"),
            Flow("Flow_LOAD_16", "Task_RecordAccepted", "Gateway_MoreBatches"),
            Flow("Flow_LOAD_17", "Task_RecordRejected", "Task_ReadBack"),
            Flow("Flow_LOAD_18", "Task_RecordUnknown", "Task_ReadBack"),
            Flow("Flow_LOAD_19", "Gateway_MoreBatches", "Task_Write", "Yes", ((1935, 390), (1935, 505), (1455, 505), (1455, 400))),
            Flow("Flow_LOAD_20", "Gateway_MoreBatches", "Task_ReadBack", "No"),
            Flow("Flow_LOAD_21", "Task_ReadBack", "Task_Reconcile"),
            Flow("Flow_LOAD_22", "Task_Reconcile", "Gateway_Verified"),
            Flow("Flow_LOAD_23", "Gateway_Verified", "End_Reconciled", "Yes"),
            Flow("Flow_LOAD_24", "Gateway_Verified", "Task_Fallout", "No"),
            Flow("Flow_LOAD_25", "Task_Fallout", "End_Fallout"),
        ),
        message_flows=(
            MessageFlow("Message_LOAD_Write", "Task_Write", "Participant_Odoo", "Supported Odoo 19 API write"),
            MessageFlow("Message_LOAD_WriteResponse", "Participant_Odoo", "Task_Write", "Write response"),
            MessageFlow("Message_LOAD_ReadBack", "Task_ReadBack", "Participant_Odoo", "Read exact affected scope"),
            MessageFlow("Message_LOAD_ReadBackResponse", "Participant_Odoo", "Task_ReadBack", "Current record state"),
        ),
    )

    integrated_test = ProcessSpec(
        slug="07-integrated-test-run",
        process_id="Process_IntegratedTestRun",
        name="Integrated multi-Recipe Test run",
        documentation=(
            "Current Project-level planning, fresh Recipe application "
            "materialization, and exact CutoverPlan revision binding."
        ),
        nodes=(
            event("Start_IT", "startEvent", "Accepted Test package and Recipes", 70, 130, "Lane_DataManager"),
            task("Task_IT_Select", "userTask", "Select Test DataVersion, target evidence, Recipes, and order", 150, 110, "Lane_DataManager"),
            task("Task_IT_Validate", "serviceTask", "Validate exact revisions, dependencies, and write ownership", 380, 330, "Lane_Impodo"),
            gateway("Gateway_IT_Valid", "Plan valid?", 600, 340, "Lane_Impodo"),
            event("End_IT_Invalid", "endEvent", "Correct plan before provisioning", 720, 127, "Lane_DataManager"),
            task("Task_IT_Union", "serviceTask", "Build one union requirement and target projection", 720, 330, "Lane_Impodo"),
            task("Task_IT_Provision", "serviceTask", "Create one isolated application workspace per Recipe", 940, 330, "Lane_Impodo"),
            task("Task_IT_Compile", "serviceTask", "Create fresh mappings and focused current issues", 1160, 330, "Lane_Impodo"),
            gateway("Gateway_IT_Ready", "All applications Ready?", 1380, 340, "Lane_Impodo"),
            task("Task_IT_Review", "userTask", "Review the owning application issues", 1500, 110, "Lane_DataManager"),
            event("End_IT_Blocked", "endEvent", "Blocked; fresh evidence retained", 1710, 127, "Lane_DataManager"),
            event("End_IT_Ready", "endEvent", "Ready for application work", 1530, 357, "Lane_Impodo"),
        ),
        flows=(
            Flow("Flow_IT_01", "Start_IT", "Task_IT_Select"),
            Flow("Flow_IT_02", "Task_IT_Select", "Task_IT_Validate"),
            Flow("Flow_IT_03", "Task_IT_Validate", "Gateway_IT_Valid"),
            Flow("Flow_IT_04", "Gateway_IT_Valid", "End_IT_Invalid", "No"),
            Flow("Flow_IT_05", "Gateway_IT_Valid", "Task_IT_Union", "Yes"),
            Flow("Flow_IT_06", "Task_IT_Union", "Task_IT_Provision"),
            Flow("Flow_IT_07", "Task_IT_Provision", "Task_IT_Compile"),
            Flow("Flow_IT_08", "Task_IT_Compile", "Gateway_IT_Ready"),
            Flow("Flow_IT_09", "Gateway_IT_Ready", "End_IT_Ready", "Yes"),
            Flow("Flow_IT_10", "Gateway_IT_Ready", "Task_IT_Review", "No"),
            Flow("Flow_IT_11", "Task_IT_Review", "End_IT_Blocked"),
        ),
    )

    integrated_qualification = ProcessSpec(
        slug="08-integrated-qualification",
        process_id="Process_IntegratedQualification",
        name="Integrated Test qualification",
        documentation=(
            "Current M5 ordered application execution, exact Test qualification, "
            "and separate rollout-candidate selection. No Production authority."
        ),
        nodes=(
            event("Start_IQ", "startEvent", "Integrated Test plan ready", 70, 130, "Lane_DataManager"),
            task("Task_IQ_Complete", "userTask", "Complete and verify each application in required order", 150, 110, "Lane_DataManager"),
            task("Task_IQ_Guard", "serviceTask", "Block downstream write until predecessors reconcile", 390, 330, "Lane_Impodo"),
            task("Task_IQ_Review", "userTask", "Review exact integrated evidence", 620, 110, "Lane_DataManager"),
            task("Task_IQ_Check", "serviceTask", "Check package, application evidence, order, and controls", 840, 330, "Lane_Impodo"),
            gateway("Gateway_IQ_Ready", "All evidence complete?", 1070, 340, "Lane_Impodo"),
            task("Task_IQ_Recover", "userTask", "Open named application and complete recovery action", 1190, 110, "Lane_DataManager"),
            task("Task_IQ_Qualify", "userTask", "Qualify exact integrated Test", 1190, 210, "Lane_DataManager"),
            task("Task_IQ_Publish", "serviceTask", "Encrypt and publish application and plan qualification", 1410, 330, "Lane_Impodo"),
            gateway("Gateway_IQ_Select", "Select rollout candidate?", 1630, 120, "Lane_DataManager"),
            task("Task_IQ_Select", "userTask", "Select qualified plan revision", 1760, 90, "Lane_DataManager"),
            task("Task_IQ_Record", "serviceTask", "Record Project rollout selection without Production authority", 1970, 330, "Lane_Impodo"),
            event("End_IQ_Selected", "endEvent", "Rollout candidate selected", 2190, 347, "Lane_Impodo"),
            event("End_IQ_Qualified", "endEvent", "Qualified; not selected", 1780, 217, "Lane_DataManager"),
        ),
        flows=(
            Flow("Flow_IQ_01", "Start_IQ", "Task_IQ_Complete"),
            Flow("Flow_IQ_02", "Task_IQ_Complete", "Task_IQ_Guard"),
            Flow("Flow_IQ_03", "Task_IQ_Guard", "Task_IQ_Review"),
            Flow("Flow_IQ_04", "Task_IQ_Review", "Task_IQ_Check"),
            Flow("Flow_IQ_05", "Task_IQ_Check", "Gateway_IQ_Ready"),
            Flow("Flow_IQ_06", "Gateway_IQ_Ready", "Task_IQ_Recover", "No"),
            Flow("Flow_IQ_07", "Task_IQ_Recover", "Task_IQ_Complete", "Retry", ((1190, 145), (1130, 145), (1130, 70), (225, 70), (225, 110))),
            Flow("Flow_IQ_08", "Gateway_IQ_Ready", "Task_IQ_Qualify", "Yes"),
            Flow("Flow_IQ_09", "Task_IQ_Qualify", "Task_IQ_Publish"),
            Flow("Flow_IQ_10", "Task_IQ_Publish", "Gateway_IQ_Select"),
            Flow("Flow_IQ_11", "Gateway_IQ_Select", "Task_IQ_Select", "Yes"),
            Flow("Flow_IQ_12", "Task_IQ_Select", "Task_IQ_Record"),
            Flow("Flow_IQ_13", "Task_IQ_Record", "End_IQ_Selected"),
            Flow("Flow_IQ_14", "Gateway_IQ_Select", "End_IQ_Qualified", "No"),
        ),
    )

    return (
        master,
        setup,
        source,
        odoo_data,
        match,
        prepare,
        review,
        load,
        integrated_test,
        integrated_qualification,
    )


def _node_element(process: ET.Element, node: Node) -> ET.Element:
    attributes = {"id": node.id, "name": node.name}
    if node.called_element:
        attributes["calledElement"] = node.called_element
    if node.attached_to:
        attributes["attachedToRef"] = node.attached_to
    element = ET.SubElement(process, q(BPMN, node.kind), attributes)
    if node.event_definition:
        ET.SubElement(element, q(BPMN, node.event_definition), {"id": f"{node.id}_{node.event_definition}"})
    return element


def _default_waypoints(source: Node, target: Node) -> tuple[tuple[int, int], ...]:
    source_right = (source.x + source.width, source.y + source.height // 2)
    target_left = (target.x, target.y + target.height // 2)
    if target.x >= source.x:
        return source_right, target_left
    detour_y = max(source.y + source.height, target.y + target.height) + 45
    return source_right, (source_right[0] + 25, detour_y), (target.x - 25, detour_y), target_left


def render(spec: ProcessSpec) -> str:
    definitions = ET.Element(
        q(BPMN, "definitions"),
        {
            "id": f"Definitions_{spec.process_id}",
            "targetNamespace": "https://impodo.dev/bpmn/current",
            "exporter": "Impodo documentation generator",
            "exporterVersion": "1.0",
        },
    )
    process = ET.SubElement(
        definitions,
        q(BPMN, "process"),
        {"id": spec.process_id, "name": spec.name, "isExecutable": "false"},
    )
    if spec.documentation:
        ET.SubElement(process, q(BPMN, "documentation")).text = spec.documentation

    node_elements: dict[str, ET.Element] = {}
    nodes = {node.id: node for node in spec.nodes}

    if spec.lanes:
        lane_set = ET.SubElement(process, q(BPMN, "laneSet"), {"id": f"LaneSet_{spec.process_id}"})
        for lane_id, lane_name in spec.lanes:
            lane = ET.SubElement(lane_set, q(BPMN, "lane"), {"id": lane_id, "name": lane_name})
            for node in spec.nodes:
                if node.lane == lane_id:
                    ET.SubElement(lane, q(BPMN, "flowNodeRef")).text = node.id

    for node in spec.nodes:
        node_elements[node.id] = _node_element(process, node)

    for flow in spec.flows:
        attributes = {"id": flow.id, "sourceRef": flow.source, "targetRef": flow.target}
        if flow.name:
            attributes["name"] = flow.name
        ET.SubElement(process, q(BPMN, "sequenceFlow"), attributes)
        ET.SubElement(node_elements[flow.source], q(BPMN, "outgoing")).text = flow.id
        ET.SubElement(node_elements[flow.target], q(BPMN, "incoming")).text = flow.id

    collaboration_id = f"Collaboration_{spec.process_id}"
    collaboration = ET.SubElement(definitions, q(BPMN, "collaboration"), {"id": collaboration_id})
    participant_id = f"Participant_{spec.process_id}"
    ET.SubElement(
        collaboration,
        q(BPMN, "participant"),
        {"id": participant_id, "name": spec.name, "processRef": spec.process_id},
    )
    if spec.message_flows:
        ET.SubElement(
            collaboration,
            q(BPMN, "participant"),
            {"id": "Participant_Odoo", "name": "Exact Odoo 19 target"},
        )
        for message in spec.message_flows:
            ET.SubElement(
                collaboration,
                q(BPMN, "messageFlow"),
                {"id": message.id, "name": message.name, "sourceRef": message.source, "targetRef": message.target},
            )

    x_offset = 70 if spec.lanes else 0
    display_nodes = {node.id: replace(node, x=node.x + x_offset) for node in spec.nodes}
    max_x = max(node.x + node.width for node in display_nodes.values()) + 100
    participant_y = 50
    participant_height = 470 if spec.lanes else 410
    diagram = ET.SubElement(definitions, q(BPMNDI, "BPMNDiagram"), {"id": f"Diagram_{spec.process_id}"})
    plane = ET.SubElement(
        diagram,
        q(BPMNDI, "BPMNPlane"),
        {"id": f"Plane_{spec.process_id}", "bpmnElement": collaboration_id},
    )
    participant_shape = ET.SubElement(
        plane,
        q(BPMNDI, "BPMNShape"),
        {"id": f"Shape_{participant_id}", "bpmnElement": participant_id, "isHorizontal": "true"},
    )
    ET.SubElement(participant_shape, q(DC, "Bounds"), {"x": "30", "y": str(participant_y), "width": str(max_x), "height": str(participant_height)})

    if spec.lanes:
        lane_geometry = (("Lane_DataManager", 50, 230), ("Lane_Impodo", 280, 240))
        for lane_id, lane_y, lane_height in lane_geometry:
            lane_shape = ET.SubElement(
                plane,
                q(BPMNDI, "BPMNShape"),
                {"id": f"Shape_{lane_id}", "bpmnElement": lane_id, "isHorizontal": "true"},
            )
            ET.SubElement(lane_shape, q(DC, "Bounds"), {"x": "60", "y": str(lane_y), "width": str(max_x - 30), "height": str(lane_height)})

    for node in display_nodes.values():
        shape_attributes = {"id": f"Shape_{node.id}", "bpmnElement": node.id}
        if node.kind.endswith("Gateway"):
            shape_attributes["isMarkerVisible"] = "true"
        shape = ET.SubElement(plane, q(BPMNDI, "BPMNShape"), shape_attributes)
        ET.SubElement(
            shape,
            q(DC, "Bounds"),
            {"x": str(node.x), "y": str(node.y), "width": str(node.width), "height": str(node.height)},
        )

    for flow in spec.flows:
        edge = ET.SubElement(plane, q(BPMNDI, "BPMNEdge"), {"id": f"Edge_{flow.id}", "bpmnElement": flow.id})
        points = (
            tuple((x + x_offset, y) for x, y in flow.waypoints)
            if flow.waypoints
            else _default_waypoints(display_nodes[flow.source], display_nodes[flow.target])
        )
        for x, y in points:
            ET.SubElement(edge, q(DI, "waypoint"), {"x": str(x), "y": str(y)})

    if spec.message_flows:
        odoo_shape = ET.SubElement(
            plane,
            q(BPMNDI, "BPMNShape"),
            {"id": "Shape_Participant_Odoo", "bpmnElement": "Participant_Odoo", "isHorizontal": "true"},
        )
        ET.SubElement(odoo_shape, q(DC, "Bounds"), {"x": "30", "y": "560", "width": str(max_x), "height": "110"})
        for index, message in enumerate(spec.message_flows):
            edge = ET.SubElement(plane, q(BPMNDI, "BPMNEdge"), {"id": f"Edge_{message.id}", "bpmnElement": message.id})
            if message.source == "Participant_Odoo":
                target = display_nodes[message.target]
                points = ((target.x + target.width // 2 + 50, 560), (target.x + target.width // 2 + 50, target.y + target.height))
            else:
                source = display_nodes[message.source]
                points = ((source.x + source.width // 2 - 50, source.y + source.height), (source.x + source.width // 2 - 50, 560))
            for x, y in points:
                ET.SubElement(edge, q(DI, "waypoint"), {"x": str(x), "y": str(y)})

    ET.indent(definitions, space="  ")
    xml = ET.tostring(definitions, encoding="unicode", xml_declaration=False)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Fail when generated files differ")
    args = parser.parse_args()

    output_dir = Path(__file__).resolve().parents[1] / "docs" / "bpmn" / "current"
    output_dir.mkdir(parents=True, exist_ok=True)
    mismatches: list[str] = []
    for spec in build_specs():
        path = output_dir / f"{spec.slug}.bpmn"
        expected = render(spec)
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != expected:
                mismatches.append(str(path))
        else:
            path.write_text(expected, encoding="utf-8")

    if mismatches:
        print("BPMN files need regeneration:")
        for mismatch in mismatches:
            print(f"- {mismatch}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
