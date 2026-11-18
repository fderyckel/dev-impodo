// Run with node --test tests/e2e/mapping-save-recovery.test.cjs.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(
  path.join(__dirname, "../../src/impodo/web/static/mapping-save-recovery.js"),
  "utf8"
);

function editor(outcomes, waitMs = 1000, inlineReview = true, options = {}) {
  const elements = new Map();
  const element = (selector) => {
    if (!elements.has(selector)) {
      elements.set(selector, {
        hidden: true,
        textContent: "",
        parentElement: { hidden: true },
        classList: { add() {}, remove() {} },
        focus() {},
        addEventListener(event, fn) { this[event] = fn; },
      });
    }
    return elements.get(selector);
  };
  let calls = 0;
  let dirty = true;
  let versions;
  const navigations = [];
  const events = [];
  const accepted = [];
  const copies = [];
  const requests = [];
  const window = { setTimeout, clearTimeout, location: { href: "https://impodo.test/mapping" } };
  vm.runInNewContext(source, {
    window, AbortController,
    document: { dispatchEvent: (event) => { events.push(event); return !inlineReview; } },
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
    navigator: { clipboard: { writeText: async (value) => { copies.push(JSON.parse(value)); } } },
    fetch: async (url, request) => {
      requests.push([request.method, url]);
      const outcome = outcomes[Math.min(calls++, outcomes.length - 1)];
      if (outcome instanceof Error) throw outcome;
      return { ok: true, text: async () => JSON.stringify(outcome) };
    },
  });
  const recovery = window.impodoMappingSaveRecovery.create({
    mappingForm: {
      dataset: {
        mutationReceiptUrl: "/mapping/mutation-receipts/",
        mutationReceiptWaitMs: String(waitMs),
        mutationReceiptPollMs: "250",
      },
      querySelector: element,
    },
    saveStatus: element("status"),
    updateMappingVersionFields: (payload) => { versions = payload; },
    navigateToMappingResult: (url) => navigations.push(url),
    setDirty: (value) => { dirty = value; },
    onCommitted: (operation) => accepted.push(operation),
    hasNewerEdits: () => Boolean(options.newerEdits),
    currentEntries: options.currentEntries,
  });
  return {
    recovery, element, navigations, events, accepted, copies, requests,
    get calls() { return calls; },
    get dirty() { return dirty; },
    get versions() { return versions; },
  };
}

const operation = { operationId: "check-1", action: "draft" };
const committed = {
  operation_id: operation.operationId,
  status: "committed",
  expected_working_draft_version: 16,
  expected_parent_version: 3,
  redirect_url: "/mapping#rows-to-use-review",
};

test("a transient receipt failure keeps waiting and applies the saved versions", async () => {
  const page = editor([
    { status: "pending" }, new Error("temporary connection failure"), committed,
  ]);
  assert.equal(await page.recovery.resolveMutationOutcome(operation), "committed");
  assert.equal(page.calls, 3);
  assert.equal(page.versions.expected_working_draft_version, 16);
  assert.equal(page.versions.expected_parent_version, 3);
  assert.equal(page.dirty, false);
  assert.deepEqual(page.navigations, [committed.redirect_url]);
  assert.equal(page.element("[data-mapping-save-outcome]").hidden, true);
  assert.deepEqual(page.accepted, [operation]);
});

test("a lost check response and a direct response open the same saved decision once", async () => {
  for (const url of [
    "/mapping?step=confirm#confirm-matches",
    "/mapping?step=check#next-step-blockers",
    "/mapping?step=confirm#rows-to-use-review",
  ]) {
    const result = { ...committed, redirect_url: url };
    const direct = editor([]);
    direct.recovery.applyMutationOutcome(result, operation);
    const recovered = editor([{ status: "pending" }, result]);
    const pending = recovered.recovery.resolveMutationOutcome(operation);
    await new Promise(setImmediate);
    assert.equal(recovered.element("status").textContent, "Still checking matches...");
    await pending;
    assert.deepEqual(direct.navigations, [url]);
    assert.deepEqual(recovered.navigations, [url]);
    assert.deepEqual(recovered.requests, Array(2).fill(["GET", "/mapping/mutation-receipts/check-1"]));
  }
});

test("a check that finishes during a new edit leaves the edited form open", async () => {
  const page = editor([committed], 1000, true, { newerEdits: true });
  await page.recovery.resolveMutationOutcome(operation);
  assert.equal(page.dirty, true);
  assert.deepEqual(page.navigations, []);
  assert.match(page.element("status").textContent, /newer edits/);
});

test("a superseded check preserves edits and copies the current form before explicit reload", async () => {
  const reload = "/mapping?step=check";
  const result = { ...committed, follow_up_status: "superseded",
    message: "The saved check has changed.", recovery: { reload_url: reload } };
  let currentValue = "Submitted";
  const page = editor([result], 1000, true, {
    newerEdits: true,
    currentEntries: (action) => [["action", action], ["field", currentValue], ["csrf_token", "restricted"]],
  });
  const submitted = page.recovery.createOperation({ action: "draft", entries: [["field", currentValue]] });
  result.operation_id = submitted.operationId;
  currentValue = "Newer edit";
  assert.equal(await page.recovery.resolveMutationOutcome(submitted), "conflict");
  assert.deepEqual(page.navigations, []);
  assert.equal(page.versions, undefined);
  assert.equal(page.dirty, true);
  assert.equal(page.recovery.hasUnresolvedOperation(), true);
  assert.equal(page.element("[data-mapping-conflict-recovery]").hidden, false);
  await page.element("[data-copy-mapping-edits]").click();
  assert.deepEqual(page.copies[0].entries, [["action", "draft"], ["field", "Newer edit"]]);
  page.element("[data-reload-saved-mapping]").click();
  assert.deepEqual(page.navigations, [reload]);
});

test("an outage reaches the deadline, retains edits, and prevents another write", async () => {
  const page = editor([new Error("connection unavailable")], 300);
  assert.equal(await page.recovery.resolveMutationOutcome(operation), "pending");
  assert.ok(page.calls >= 2);
  assert.equal(page.dirty, true);
  assert.deepEqual(page.navigations, []);
  assert.equal(page.recovery.blockIfNeeded(), true);
  assert.equal(page.element("[data-mapping-unknown-recovery]").hidden, false);
  assert.deepEqual(page.accepted, []);
});

test("a confirmed version conflict stops polling and preserves unsaved edits", async () => {
  const page = editor([{
    status: "rejected", failure_code: "MAPPING_VERSION_CONFLICT",
  }]);
  assert.equal(await page.recovery.resolveMutationOutcome(operation), "conflict");
  assert.equal(page.calls, 1);
  assert.equal(page.dirty, true);
  assert.equal(page.recovery.blockIfNeeded(), true);
  assert.equal(page.element("[data-mapping-conflict-recovery]").hidden, false);
  assert.deepEqual(page.accepted, []);
});

test("a stale supporting review opens inline recovery from a receipt without navigation", async () => {
  const page = editor([{
    status: "rejected", failure_code: "SUPPORTING_REVIEW_REQUIRED",
    operation_id: operation.operationId,
  }]);
  assert.equal(await page.recovery.resolveMutationOutcome(operation), "rejected");
  assert.equal(page.dirty, true);
  assert.deepEqual(page.navigations, []);
  assert.equal(page.recovery.blockIfNeeded(), false);
  assert.equal(page.element("[data-mapping-save-outcome]").hidden, true);
  assert.equal(page.events[0].type, "impodo:related-review-required");
  assert.equal(page.events[0].detail.operationId, operation.operationId);
});


test("stale review keeps ordinary error guidance when inline recovery is unavailable", async () => {
  const page = editor([{status: "rejected", failure_code: "SUPPORTING_REVIEW_REQUIRED",
    detail: "Review related-data choices in Odoo data."}], 1000, false);
  assert.equal(await page.recovery.resolveMutationOutcome(operation), "rejected");
  assert.equal(page.element("[data-mapping-save-outcome]").hidden, false);
  assert.match(page.element("[data-mapping-save-outcome-message]").textContent, /Review related-data/);
  assert.equal(page.dirty, true);
});

test("a field conflict keeps its exact correction data through receipt recovery", async () => {
  const conflicts = [{title: "Bill of Material · Product", field: "product_id", fix_url: "/mapping?relation_target=product_id"}];
  const page = editor([{status: "rejected", failure_code: "SUPPORTING_MAPPING_CONFLICT",
    detail: "The Product field conflicts with its saved source.", supporting_conflicts: conflicts}]);
  assert.equal(await page.recovery.resolveMutationOutcome(operation), "rejected");
  assert.equal(page.events[0].detail.mappingConflict, true);
  assert.deepEqual(JSON.parse(JSON.stringify(page.events[0].detail.conflicts)), conflicts);
  assert.match(page.events[0].detail.message, /Product/);
  assert.equal(page.dirty, true);
  assert.deepEqual(page.navigations, []);
});
