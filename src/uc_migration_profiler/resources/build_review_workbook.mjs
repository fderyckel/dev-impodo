import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const [manifestPath, workbookPath, previewDirectory] = process.argv.slice(2);
if (!manifestPath || !workbookPath) {
  throw new Error("manifest and workbook output paths are required");
}

const manifest = JSON.parse(await fs.readFile(manifestPath, "utf8"));
const workbook = Workbook.create();
const sheetNames = [
  "Dashboard",
  "Target Environment",
  "Dataset Summary",
  "Proposed Creates",
  "Proposed Updates",
  "Field Differences",
  "Unchanged",
  "Ambiguous Matches",
  "Blocked Records",
  "Reference Resolution",
  "Source Issues",
  "Metadata Coverage",
];
const sheets = Object.fromEntries(
  sheetNames.map((name) => [name, workbook.worksheets.add(name)]),
);

const colors = {
  navy: "#17324D",
  blue: "#2F6B9A",
  paleBlue: "#E8F1F8",
  green: "#2E7D5B",
  paleGreen: "#E5F4EC",
  amber: "#B7791F",
  paleAmber: "#FFF4D6",
  red: "#B43C3C",
  paleRed: "#FCE8E8",
  gray: "#5E6B75",
  paleGray: "#EEF1F3",
  white: "#FFFFFF",
  border: "#CED6DC",
};

function safeCell(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  if (
    typeof value === "string" &&
    (
      /^\d{4}-\d{2}-\d{2}(?:T.*)?$/.test(value) ||
      /^\d+(?:\.\d+)+$/.test(value)
    )
  ) {
    return `\u200B${value}`;
  }
  if (typeof value === "string" && /^[=+\-@]/.test(value)) return `'${value}`;
  return value;
}

function businessIdentity(decision) {
  return JSON.stringify(decision.business_identity);
}

function businessScope(decision) {
  return JSON.stringify(decision.business_scope || []);
}

function titleBand(sheet, title, subtitle, columns) {
  const lastColumn = columnName(columns);
  sheet.mergeCells(`A1:${lastColumn}1`);
  sheet.getRange("A1").values = [[title]];
  sheet.getRange(`A1:${lastColumn}1`).format = {
    fill: colors.navy,
    font: { bold: true, color: colors.white, size: 16 },
    verticalAlignment: "center",
  };
  sheet.getRange(`A1:${lastColumn}1`).format.rowHeight = 30;
  sheet.mergeCells(`A2:${lastColumn}2`);
  sheet.getRange("A2").values = [[subtitle]];
  sheet.getRange(`A2:${lastColumn}2`).format = {
    fill: colors.paleBlue,
    font: { color: colors.gray, italic: true },
    verticalAlignment: "center",
  };
  sheet.getRange(`A2:${lastColumn}2`).format.rowHeight = 24;
  sheet.showGridLines = false;
}

function columnName(number) {
  let result = "";
  let value = number;
  while (value > 0) {
    value -= 1;
    result = String.fromCharCode(65 + (value % 26)) + result;
    value = Math.floor(value / 26);
  }
  return result;
}

function styleDataSheet(sheet, headers, rows, tableName, accent = colors.blue) {
  const width = headers.length;
  titleBand(
    sheet,
    sheet.name,
    `${rows.length.toLocaleString()} review row${rows.length === 1 ? "" : "s"}`,
    width,
  );
  const matrix = [headers, ...rows.map((row) => row.map(safeCell))];
  const endRow = 3 + rows.length;
  const endColumn = columnName(width);
  sheet.getRange(`A3:${endColumn}${endRow}`).values = matrix;
  sheet.getRange(`A3:${endColumn}3`).format = {
    fill: accent,
    font: { bold: true, color: colors.white },
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "outside", style: "thin", color: colors.border },
  };
  sheet.getRange(`A3:${endColumn}3`).format.rowHeight = 30;
  if (rows.length > 0) {
    const table = sheet.tables.add(`A3:${endColumn}${endRow}`, true, tableName);
    table.style = "TableStyleMedium2";
    table.showFilterButton = true;
  }
  sheet.freezePanes.freezeRows(3);
  headers.forEach((header, columnIndex) => {
    const contentLengths = rows
      .slice(0, 100)
      .map((row) => String(safeCell(row[columnIndex])).length);
    const desired = Math.min(
      48,
      Math.max(13, header.length + 4, ...contentLengths.map((length) => length + 3)),
    );
    sheet
      .getRangeByIndexes(0, columnIndex, Math.max(4, rows.length + 3), 1)
      .format.columnWidth = desired;
  });
  if (rows.length > 0) {
    const bodyRange = sheet.getRange(`A4:${endColumn}${endRow}`);
    bodyRange.format = {
      verticalAlignment: "top",
      wrapText: true,
      borders: {
        insideHorizontal: { style: "thin", color: "#E5E9EC" },
      },
    };
    bodyRange.format.autofitRows();
  }
}

const decisions = manifest.decisions || [];
const classificationRows = (classification) =>
  decisions.filter((decision) => decision.classification === classification);

const datasetNames = [...new Set(decisions.map((decision) => decision.dataset))];
const datasetSummaryRows = datasetNames.map((dataset) => {
  const rows = decisions.filter((decision) => decision.dataset === dataset);
  const count = (classification) =>
    rows.filter((decision) => decision.classification === classification).length;
  return [
    dataset,
    rows.length,
    count("CREATE"),
    count("UPDATE"),
    count("UNCHANGED"),
    count("AMBIGUOUS"),
    count("BLOCKED"),
  ];
});

styleDataSheet(
  sheets["Dataset Summary"],
  ["Dataset", "Candidates", "CREATE", "UPDATE", "UNCHANGED", "AMBIGUOUS", "BLOCKED"],
  datasetSummaryRows,
  "DatasetSummaryTable",
);

function decisionRows(classification) {
  return classificationRows(classification).map((decision) => [
    decision.dataset,
    decision.source_row,
    businessIdentity(decision),
    businessScope(decision),
    decision.target_match_count,
    (decision.issues || []).map((issue) => issue.code).join("; "),
  ]);
}

styleDataSheet(
  sheets["Proposed Creates"],
  ["Dataset", "Source Row", "Business Identity", "Business Scope", "Target Matches", "Issues"],
  decisionRows("CREATE"),
  "CreatesTable",
  colors.green,
);
styleDataSheet(
  sheets["Proposed Updates"],
  ["Dataset", "Source Row", "Business Identity", "Business Scope", "Target Matches", "Issues"],
  decisionRows("UPDATE"),
  "UpdatesTable",
  colors.amber,
);
styleDataSheet(
  sheets["Unchanged"],
  ["Dataset", "Source Row", "Business Identity", "Business Scope", "Target Matches", "Issues"],
  decisionRows("UNCHANGED"),
  "UnchangedTable",
  colors.gray,
);
styleDataSheet(
  sheets["Ambiguous Matches"],
  ["Dataset", "Source Row", "Business Identity", "Business Scope", "Target Matches", "Issues"],
  decisionRows("AMBIGUOUS"),
  "AmbiguousTable",
  colors.red,
);
styleDataSheet(
  sheets["Blocked Records"],
  ["Dataset", "Source Row", "Business Identity", "Business Scope", "Target Matches", "Issues"],
  decisionRows("BLOCKED"),
  "BlockedTable",
  colors.red,
);

const differenceRows = decisions.flatMap((decision) =>
  (decision.differences || []).map((difference) => [
    decision.dataset,
    decision.source_row,
    businessIdentity(decision),
    businessScope(decision),
    difference.field,
    safeCell(difference.existing),
    safeCell(difference.proposed),
    difference.comparison_rule,
    difference.material,
  ]),
);
styleDataSheet(
  sheets["Field Differences"],
  [
    "Dataset",
    "Source Row",
    "Business Identity",
    "Business Scope",
    "Field",
    "Existing Target",
    "Proposed Source",
    "Comparison Rule",
    "Material",
  ],
  differenceRows,
  "DifferencesTable",
  colors.amber,
);

const referenceRows = (manifest.reference_resolutions || []).map((item) => [
  item.dataset,
  item.field,
  safeCell(item.reference),
  item.status,
  item.match_count,
  item.affected_count,
]);
styleDataSheet(
  sheets["Reference Resolution"],
  ["Dataset", "Field", "Business Reference", "Status", "Matches", "Affected Rows"],
  referenceRows,
  "ReferenceTable",
);

const issueRows = (manifest.source_issues || []).map((issue) => [
  issue.severity,
  issue.code,
  issue.dataset,
  issue.row,
  issue.field,
  issue.message,
  issue.affected_count,
]);
styleDataSheet(
  sheets["Source Issues"],
  ["Severity", "Code", "Dataset", "Source Row", "Field", "Message", "Affected Rows"],
  issueRows,
  "IssuesTable",
  colors.red,
);

const coverageRows = (manifest.metadata_coverage || []).map((item) => [
  item.dataset,
  item.model,
  item.status,
  item.requested_fields,
  item.available_fields,
]);
styleDataSheet(
  sheets["Metadata Coverage"],
  ["Dataset", "Model", "Status", "Requested Fields", "Available Fields"],
  coverageRows,
  "MetadataCoverageTable",
);

const environment = manifest.target_environment || {};
const environmentRows = [
  ["Environment", environment.environment],
  ["Database", environment.database],
  ["Odoo Version", environment.odoo_version],
  ["Snapshot Timestamp", environment.snapshot_timestamp],
  ["Profile ID", manifest.profile?.id],
  ["Profile Version", manifest.profile?.version],
  ["Semantic Hash", manifest.semantic_hash],
  ["Metadata Snapshot Hash", manifest.snapshot_hashes?.metadata],
  ["Record Snapshot Hash", manifest.snapshot_hashes?.records],
  ["Module Versions", environment.module_versions],
  ["Source Hashes", manifest.source_hashes],
];
styleDataSheet(
  sheets["Target Environment"],
  ["Attribute", "Value"],
  environmentRows,
  "EnvironmentTable",
);

const dashboard = sheets["Dashboard"];
titleBand(
  dashboard,
  "UC Odoo Read-only Preflight",
  "Review evidence only — this workbook cannot write to Odoo",
  8,
);
dashboard.getRange("A4:B9").values = [
  ["Classification", "Count"],
  ["CREATE", null],
  ["UPDATE", null],
  ["UNCHANGED", null],
  ["AMBIGUOUS", null],
  ["BLOCKED", null],
];
dashboard.getRange("B5").formulas = [["=SUM('Dataset Summary'!C4:C500)"]];
dashboard.getRange("B6").formulas = [["=SUM('Dataset Summary'!D4:D500)"]];
dashboard.getRange("B7").formulas = [["=SUM('Dataset Summary'!E4:E500)"]];
dashboard.getRange("B8").formulas = [["=SUM('Dataset Summary'!F4:F500)"]];
dashboard.getRange("B9").formulas = [["=SUM('Dataset Summary'!G4:G500)"]];
dashboard.getRange("A4:B4").format = {
  fill: colors.blue,
  font: { bold: true, color: colors.white },
};
dashboard.getRange("A5:A9").format.font = { bold: true };
dashboard.getRange("B5:B9").format.numberFormat = "#,##0";
dashboard.getRange("A4:B9").format.borders = {
  preset: "outside",
  style: "thin",
  color: colors.border,
};
dashboard.getRange("D4:H9").values = [
  ["Run assurance", "", "", "", ""],
  ["Connector capability", "Read only", "", "", ""],
  ["Portable IDs", "Numeric Odoo IDs excluded", "", "", ""],
  ["Profile", `${manifest.profile?.id} ${manifest.profile?.version}`, "", "", ""],
  ["Target", `${environment.environment} / ${environment.database}`, "", "", ""],
  ["Semantic hash", manifest.semantic_hash, "", "", ""],
];
dashboard.mergeCells("D4:H4");
for (let row = 5; row <= 9; row += 1) dashboard.mergeCells(`E${row}:H${row}`);
dashboard.getRange("D4:H4").format = {
  fill: colors.navy,
  font: { bold: true, color: colors.white },
};
dashboard.getRange("D5:D9").format = {
  fill: colors.paleBlue,
  font: { bold: true, color: colors.navy },
};
dashboard.getRange("D4:H9").format.wrapText = true;
dashboard.getRange("A1:H12").format.columnWidth = 16;
dashboard.getRange("D1:D12").format.columnWidth = 22;
dashboard.getRange("E1:H12").format.columnWidth = 17;
dashboard.freezePanes.freezeRows(2);
const chart = dashboard.charts.add("bar", dashboard.getRange("A4:B9"));
chart.title = "Preflight classifications";
chart.hasLegend = false;
chart.setPosition("A12", "H29");

if (previewDirectory) {
  await fs.mkdir(previewDirectory, { recursive: true });
  for (const name of sheetNames) {
    const preview = await workbook.render({
      sheetName: name,
      autoCrop: "all",
      scale: 1,
      format: "png",
    });
    const safeName = name.toLowerCase().replace(/[^a-z0-9]+/g, "-");
    await fs.writeFile(
      `${previewDirectory}/${safeName}.png`,
      new Uint8Array(await preview.arrayBuffer()),
    );
  }
}

const dashboardCheck = await workbook.inspect({
  kind: "table",
  range: "Dashboard!A1:H29",
  include: "values,formulas",
  tableMaxRows: 30,
  tableMaxCols: 8,
  maxChars: 4000,
});
const errorCheck = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "final formula error scan",
  maxChars: 1000,
});
console.log(dashboardCheck.ndjson);
console.log(errorCheck.ndjson);

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(workbookPath);
