// Run with node --test tests/e2e/attention-export.test.cjs.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname,
  "../../src/impodo/web/static/attention-export.js"), "utf8");

function fixture(responses) {
  const status = { dataset: { endpoint: "/summary/attention/counts?quality=saved" } };
  const retry = { hidden: true, disabled: false,
    addEventListener: (_name, callback) => { retry.click = callback; } };
  const requests = [];
  const problems = { items: [], replaceChildren() { this.items = []; },
    append(item) { this.items.push(item); } };
  const preview = { hidden: true, querySelector: () => problems };
  vm.runInNewContext(source, {
    document: { querySelectorAll: () => [{ querySelector: (selector) =>
      selector === "[data-attention-export-counts]" ? status : retry }],
      querySelector: () => preview, createElement: () => ({ textContent: "" }) },
    fetch: async (url, options) => {
      requests.push({ url, cache: options.cache });
      const response = responses.shift();
      if (response instanceof Error) throw response;
      return { ok: response.ok, json: async () => ({problems: [], ...response}) };
    },
  });
  return { status, retry, requests, preview, problems };
}

test("counts describe the complete saved export beside the download", async () => {
  const current = fixture([{ ok: true, affected_records: 2, findings: 4,
    problems: [{ affected_records: 2, problem: "Identifier repeats",
      action: "Review the related rows", correction: "Source data" }] }]);
  await new Promise(setImmediate);
  assert.match(current.status.textContent, /2 affected records · 4 findings across all tables and pages/);
  assert.deepEqual(current.requests, [{ url: "/summary/attention/counts?quality=saved", cache: "no-store" }]);
  assert.equal(current.retry.hidden, true);
  assert.equal(current.preview.hidden, false);
  assert.match(current.problems.items[0].textContent, /Review the related rows/);
});

test("failed counts keep the download available and allow a retry", async () => {
  const current = fixture([
    new Error("Connection interrupted"),
    { ok: true, affected_records: 3, findings: 5 },
  ]);
  await new Promise(setImmediate);
  assert.equal(current.status.textContent, "Connection interrupted");
  assert.equal(current.retry.hidden, false);
  await current.retry.click();
  assert.match(current.status.textContent, /3 affected records · 5 findings/);
  assert.equal(current.retry.hidden, true);
  assert.equal(current.retry.disabled, false);
});

test("stale evidence tells the operator to reopen Final review", async () => {
  const current = fixture([{ ok: false, detail: "Reopen Final review before exporting." }]);
  await new Promise(setImmediate);
  assert.equal(current.status.textContent, "Reopen Final review before exporting.");
  assert.equal(current.retry.hidden, false);
});

test("one affected record and one finding use singular labels", async () => {
  const current = fixture([{ ok: true, affected_records: 1, findings: 1 }]);
  await new Promise(setImmediate);
  assert.match(current.status.textContent, /1 affected record · 1 finding across/);
});
