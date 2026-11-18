// Exercise the shipped form and receipt handlers with a delayed transport.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { setImmediate: settle } = require("node:timers/promises");
const test = require("node:test");
const vm = require("node:vm");

const mapping = "/workspaces/one/mapping";
const decision = mapping + "?step=confirm#confirm-matches";

function openEditor(delayAt, { missing = false, manual = false } = {}) {
  const sent = [];
  const retry = { hidden: true, addEventListener(name, handler) { this[name] = handler; } };
  const listeners = new Map();
  const documentEvents = new Map();
  const requests = [];
  const navigations = [];
  const status = { dataset: {}, classList: { add() {}, remove() {} } };
  const form = {
    dataset: { mutationReceiptUrl: mapping + "/mutation-receipts/", mutationReceiptWaitMs: manual ? "0" : "180000" },
    addEventListener(name, handler) { listeners.set(name, handler); },
    querySelectorAll() { return []; },
    querySelector(selector) {
      if (selector === "[data-retry-missing-save]") return retry;
      return selector === "[data-mapping-save-status]" ? status : null;
    },
    getAttribute() { return mapping + "/save"; },
    setAttribute() {},
    removeAttribute() {},
  };
  class Field {
    name = "field";
    value = "Submitted";
    closest() { return null; }
  }
  const field = new Field();
  const window = {
    setTimeout, clearTimeout, impodoMappingSourceColumns: {}, addEventListener() {},
    crypto: { randomUUID: () => "original-operation" },
    location: { href: "https://impodo.test" + mapping + "?step=check",
      assign(url) { navigations.push(url.pathname + url.search + url.hash); } },
  };
  const document = {
    querySelector() { return form; },
    querySelectorAll() { return []; },
    addEventListener(name, handler) {
      if (name === "DOMContentLoaded") handler();
      else documentEvents.set(name, handler);
    },
  };
  let bodyStarted = false;
  const fetch = async (url, options) => {
    requests.push([options.method, url]);
    if (options.method === "POST") {
      sent.push(options.body);
      if (sent.length === 2) return { ok: true, text: async () => JSON.stringify({ status: "committed", redirect_url: decision }) };
      const submitted = Object.fromEntries(JSON.parse(options.body).entries);
      assert.equal(submitted.action, "draft");
      assert.equal(submitted.field, "Submitted");
      assert.equal(submitted.operation_id, "original-operation");
      if (delayAt === "gateway") {
        return { ok: false, status: 503, text: async () => JSON.stringify({ detail: "Unavailable" }) };
      }
      const waitForAbort = () => new Promise((resolve, reject) => {
        options.signal.addEventListener("abort", () => reject(options.signal.reason), { once: true });
      });
      if (delayAt === "headers") return waitForAbort();
      return { ok: true, text() { bodyStarted = true; return waitForAbort(); } };
    }
    const status = requests.length === 2 ? (missing ? "not_found" : "pending") : "committed";
    return { ok: true, text: async () => JSON.stringify({
      status, operation_id: "original-operation", redirect_url: decision,
    }) };
  };
  const sandbox = vm.createContext({
    document, window, fetch, URL, AbortController, Date, HTMLInputElement: Field,
    FormData: class extends URLSearchParams { constructor() { super([[field.name, field.value]]); } },
  });
  for (const name of ["mapping-save-recovery", "mapping-editor"]) {
    const path = join(__dirname, "../../src/impodo/web/static", name + ".js");
    vm.runInContext(readFileSync(path, "utf8"), sandbox, { filename: name + ".js" });
  }
  return {
    requests, navigations, sent, retry, field, status, editor: window.impodoMappingEditor,
    bodyStarted: () => bodyStarted,
    submit: () => listeners.get("submit")({ preventDefault() {}, submitter: { value: "draft" } }),
    disconnect: () => documentEvents.get("impodo:server-disconnected")(),
    edit(value) { field.value = value; listeners.get("input")({ target: field }); },
  };
}

for (const delayAt of ["headers", "body"]) {
  test(`a check waiting for response ${delayAt} at 15 seconds recovers the original operation`, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
    const page = openEditor(delayAt);
    const submission = page.submit();
    await settle();
    assert.equal(page.status.textContent, "Checking matches...");
    assert.equal(page.bodyStarted(), delayAt === "body");
    t.mock.timers.tick(14999);
    await settle();
    const post = ["POST", mapping + "/save"];
    const receipt = ["GET", mapping + "/mutation-receipts/original-operation"];
    assert.deepEqual(page.requests, [post]);
    if (delayAt === "headers") {
      page.disconnect();
      assert.equal(page.editor.getState().busy, true);
      await page.submit();
      assert.deepEqual(page.requests, [post]);
    }
    t.mock.timers.tick(1);
    await settle();
    assert.deepEqual(page.requests, [post, receipt]);
    assert.equal(page.status.textContent, "Still checking matches...");
    await page.submit();
    if (delayAt === "body") page.edit("Newer unsaved value");
    t.mock.timers.tick(1500);
    await submission;
    assert.deepEqual(page.requests, [post, receipt, receipt]);
    assert.equal(page.editor.getState().busy, false);
    if (delayAt === "body") {
      assert.equal(page.field.value, "Newer unsaved value");
      assert.equal(page.editor.getState().dirty, true);
      assert.deepEqual(page.navigations, []);
    } else {
      assert.deepEqual(page.navigations, [decision]);
      assert.equal(page.editor.getState().dirty, false);
    }
  });
}

test("a missing first receipt can commit during read-back without a second POST", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  const page = openEditor("headers", { missing: true });
  const saving = page.submit();
  await settle();
  t.mock.timers.tick(15000);
  await settle();
  assert.match(page.status.textContent, /Waiting for Impodo to record/);
  t.mock.timers.tick(1500);
  await saving;
  assert.deepEqual(page.requests.map(([method]) => method), ["POST", "GET", "GET"]);
  assert.deepEqual(page.navigations, [decision]);
});

test("an explicit retry after a missing receipt uses the original form and ID", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  const page = openEditor("headers", { missing: true, manual: true });
  const saving = page.submit();
  await settle();
  t.mock.timers.tick(15000);
  await saving;
  assert.equal(page.retry.hidden, false);
  assert.equal(page.editor.getState().busy, true);
  page.edit("Newer unsaved value");
  await page.submit();
  assert.equal(page.sent.length, 1);
  await page.retry.click();
  assert.equal(page.sent.length, 2);
  assert.equal(page.sent[0], page.sent[1]);
  assert.equal(page.editor.getState().dirty, true);
  assert.equal(page.editor.getState().busy, false);
  assert.deepEqual(page.navigations, []);
});

test("a server error without a receipt checks the original operation before offering recovery", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  const page = openEditor("gateway");
  const saving = page.submit();
  await settle();
  assert.deepEqual(page.requests.map(([method]) => method), ["POST", "GET"]);
  assert.equal(page.retry.hidden, true);
  t.mock.timers.tick(1500);
  await saving;
  assert.deepEqual(page.navigations, [decision]);
  assert.equal(page.sent.length, 1);
});
