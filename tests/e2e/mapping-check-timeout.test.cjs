// Exercise the shipped form and receipt handlers with a delayed transport.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { setImmediate: settle } = require("node:timers/promises");
const test = require("node:test");
const vm = require("node:vm");

const mapping = "/workspaces/one/mapping";
const decision = mapping + "?step=confirm#confirm-matches";

function openEditor(delayAt) {
  const listeners = new Map();
  const documentEvents = new Map();
  const requests = [];
  const navigations = [];
  const status = { dataset: {}, classList: { add() {}, remove() {} } };
  const form = {
    dataset: { mutationReceiptUrl: mapping + "/mutation-receipts/" },
    addEventListener(name, handler) { listeners.set(name, handler); },
    querySelectorAll() { return []; },
    querySelector(selector) { return selector === "[data-mapping-save-status]" ? status : null; },
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
      const submitted = Object.fromEntries(JSON.parse(options.body).entries);
      assert.equal(submitted.action, "draft");
      assert.equal(submitted.field, "Submitted");
      assert.equal(submitted.operation_id, "original-operation");
      const waitForAbort = () => new Promise((resolve, reject) => {
        options.signal.addEventListener("abort", () => reject(options.signal.reason), { once: true });
      });
      if (delayAt === "headers") return waitForAbort();
      return { ok: true, text() { bodyStarted = true; return waitForAbort(); } };
    }
    const status = requests.length === 2 ? "pending" : "committed";
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
    requests, navigations, field, status, editor: window.impodoMappingEditor,
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
