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

function editor(outcomes, waitMs = 1000) {
  const elements = new Map();
  const element = (selector) => {
    if (!elements.has(selector)) {
      elements.set(selector, {
        hidden: true,
        textContent: "",
        parentElement: { hidden: true },
        classList: { add() {}, remove() {} },
        focus() {},
        addEventListener() {},
      });
    }
    return elements.get(selector);
  };
  let calls = 0;
  let dirty = true;
  let versions;
  const navigations = [];
  const window = { setTimeout, clearTimeout };
  vm.runInNewContext(source, {
    window, AbortController,
    fetch: async () => {
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
  });
  return {
    recovery, element, navigations,
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
});

test("an outage reaches the deadline, retains edits, and prevents another write", async () => {
  const page = editor([new Error("connection unavailable")], 300);
  assert.equal(await page.recovery.resolveMutationOutcome(operation), "pending");
  assert.ok(page.calls >= 2);
  assert.equal(page.dirty, true);
  assert.deepEqual(page.navigations, []);
  assert.equal(page.recovery.blockIfNeeded(), true);
  assert.equal(page.element("[data-mapping-unknown-recovery]").hidden, false);
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
});
