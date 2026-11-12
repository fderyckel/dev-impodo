// Run with node --test tests/e2e/mapping-identity-matching-field.test.cjs.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname,
  "../../src/impodo/web/static/mapping-relation-row.js"), "utf8");

class Control {
  constructor(value = "") {
    this.value = value;
    this.dataset = {};
    this.listeners = new Map();
    this.hidden = false;
    this.disabled = false;
    this.isConnected = true;
  }
  addEventListener(name, callback) {
    this.listeners.set(name, [...(this.listeners.get(name) || []), callback]);
  }
  async fire(name) {
    for (const listener of this.listeners.get(name) || []) await listener({ type: name });
  }
  dispatchEvent(event) { return this.fire(event.type); }
  focus() { this.focused = true; }
}

function fixture(response = { ok: true, fields: [{ name: "x_asset_code", label: "Asset code" }] }) {
  const origin = new Control("target_catalog");
  const key = new Control("name-key");
  key.options = [{ value: "name-key", dataset: { matchingField: "name" } }];
  Object.defineProperty(key, "selectedOptions", {
    get: () => key.options.filter((item) => item.value === key.value),
  });
  key.querySelectorAll = (selector) => key.options.filter((option) =>
    selector === "[data-matching-field]" ? option.dataset.matchingField : option.dataset.selectedMatchingField);
  key.add = (option) => { option.parentElement = key; key.options.push(option); };
  const picker = new Control();
  const button = new Control();
  button.textContent = "Load Odoo matching fields";
  button.dataset = { endpoint: "/mapping/linked-matching-fields", targetModel: "x.work_order" };
  button.closest = () => picker;
  const status = new Control();
  const schemaReview = new Control();
  schemaReview.hidden = true;
  const controls = {
    "[data-load-linked-matching-fields]": button,
    "[data-linked-matching-key]": key,
    "[data-relationship-origin]": origin,
    "[data-linked-matching-field-status]": status,
    "[data-linked-matching-schema-review]": schemaReview,
  };
  const row = {
    dataset: { targetField: "x_asset_id" },
    querySelector: (selector) => controls[selector] || null,
    querySelectorAll: () => [],
  };
  const requests = [];
  vm.runInNewContext(source, {
    URLSearchParams,
    Event: class { constructor(type) { this.type = type; } },
    Option: class { constructor() { this.dataset = {}; } },
    window: { impodoMappingEditor: { mappingForm: { querySelector: () => ({ value: "csrf-token" }) } } },
    document: {
      addEventListener: (_name, callback) => callback(),
      querySelectorAll: (selector) => ["[data-identity-matching-field]", "[data-relationship-origin-controls]"].includes(selector) ? [row] : [],
    },
    fetch: async (url, options) => {
      requests.push({ url, method: options.method, body: Object.fromEntries(options.body) });
      return { ok: response.ok, json: async () => response };
    },
  });
  return { origin, key, button, picker, status, schemaReview, requests, response };
}

test("relational identities load a custom matching field without linked-value policy controls", async () => {
  const current = fixture();
  await current.button.fire("click");
  assert.deepEqual(current.requests, [{
    url: "/mapping/linked-matching-fields", method: "POST",
    body: { csrf_token: "csrf-token", target_model: "x.work_order", target_field: "x_asset_id" },
  }]);
  assert.equal(current.key.value, "name-key");
  const custom = current.key.options.find((item) => item.value === "odoo-field:x_asset_code");
  assert.equal(custom.textContent, "Asset code (x_asset_code)");
  assert.equal(custom.dataset.selectedMatchingField, "x_asset_code");
  assert.equal(current.key.focused, true);
  assert.equal(current.button.disabled, false);
  assert.equal(current.status.textContent, "Choose a matching field from the list above.");
});

test("failed discovery preserves the saved matching choice and permits retry", async () => {
  const current = fixture({ ok: false, detail: "Check Odoo access" });
  await current.button.fire("click");
  assert.equal(current.key.value, "name-key");
  assert.equal(current.key.options.length, 1);
  assert.equal(current.status.textContent, "Check Odoo access");
  assert.equal(current.button.disabled, false);
  assert.equal(current.button.textContent, "Retry loading Odoo matching fields");
  assert.equal(current.schemaReview.hidden, true);
  current.response.ok = true;
  current.response.fields = [{ name: "x_asset_code", label: "Asset code" }];
  await current.button.fire("click");
  assert.equal(current.button.textContent, "Load Odoo matching fields");
  assert.equal(current.key.value, "name-key");
  assert.equal(current.key.options.length, 2);
});

test("genuine Odoo changes expose review beside the field and preserve selections", async () => {
  const current = fixture({ ok: false, detail: "Review Odoo changes", review_odoo_details: true });
  await current.button.fire("click");
  assert.equal(current.schemaReview.hidden, false);
  assert.equal(current.key.value, "name-key");
  assert.equal(current.origin.value, "target_catalog");
  assert.equal(current.button.disabled, false);
  current.response.ok = true;
  current.response.fields = [];
  await current.button.fire("click");
  assert.equal(current.schemaReview.hidden, true);
});

test("a selected matching field cannot follow a switch to incoming records", async () => {
  const current = fixture();
  await current.button.fire("click");
  current.key.value = "odoo-field:x_asset_code";
  await current.key.fire("change");
  current.origin.value = "dataset";
  await current.origin.fire("change");
  assert.equal(current.key.value, "");
  assert.equal(current.picker.hidden, true);
  assert.equal(current.key.options.find((item) => item.value === "odoo-field:x_asset_code").disabled, true);
});
