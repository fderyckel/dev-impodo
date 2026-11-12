// Exercise the actual linked-value editor without a browser or listening socket.
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
    this.style = {};
    this.listeners = new Map();
    this.options = [];
  }
  addEventListener(name, callback) {
    this.listeners.set(name, [...(this.listeners.get(name) || []), callback]);
  }
  async fire(type) {
    for (const listener of this.listeners.get(type) || []) await listener({ type });
  }
  dispatchEvent(event) { return this.fire(event.type); }
  setCustomValidity(message) { this.validationMessage = message; }
  replaceChildren(...options) { this.options = options; this.value = ""; }
}

function fixture(responses = [{ ok: true, target_choices: [
  { value: "440000", label: "Suppliers (440000)" },
  { value: "441000", label: "Other creditors (441000)" },
] }]) {
  const provider = new Control("constant_existing");
  const key = new Control("code-key");
  key.options = [
    { value: "code-key", dataset: { keyFields: "code", scopeFields: "" } },
    { value: "name-key", dataset: { keyFields: "name", scopeFields: "" } },
  ];
  Object.defineProperty(key, "selectedOptions", {
    get: () => key.options.filter((option) => option.value === key.value),
  });
  const components = Array.from({ length: 2 }, (_, slot) => {
    const component = new Control();
    component.dataset.constantComponentSlot = String(slot);
    component.input = new Control();
    component.scope = new Control();
    component.label = new Control();
    component.querySelector = (selector) => {
      if (selector === "[data-constant-component-value]") return component.input;
      if (selector === "[data-constant-scope-choice]") return component.scope;
      if (selector === "[data-constant-component-label]") return component.label;
      if (selector.includes(":not([disabled])")) return component.scope.disabled
        ? component.input : component.scope;
      return null;
    };
    return component;
  });
  const choice = new Control();
  const search = new Control();
  const status = new Control();
  const check = new Control();
  const chooser = new Control();
  chooser.dataset = { endpoint: "/mapping/value-choices", datasetId: "suppliers",
    constantTargetModel: "res.partner", targetField: "property_account_payable_id",
    sourceRowCount: "150", sourceName: "Suppliers" };
  const chooserControls = {
    "[data-constant-existing-choice]": choice, "[data-constant-choice-search]": search,
    "[data-constant-choice-status]": status, "[data-check-constant-record]": check,
  };
  chooser.querySelector = (selector) => chooserControls[selector] || null;
  const rowControls = {
    "[data-relation-value-source]": provider, "[data-constant-business-key]": key,
    "[data-constant-existing-chooser]": chooser,
  };
  const row = {
    dataset: {},
    querySelector(selector) {
      if (selector.includes('data-constant-component-slot="0"')) return components[0].input;
      return rowControls[selector] || null;
    },
    querySelectorAll(selector) {
      if (selector === "[data-constant-component-row]") return components;
      if (selector === "[data-constant-component-row]:not([hidden])") return components.filter((item) => !item.hidden);
      if (selector.includes("[data-constant-scope-choice]:not([disabled])")) return components.filter((item) => !item.hidden && !item.scope.disabled).map((item) => item.scope);
      if (selector === "[data-constant-scope-choice]") return components.map((item) => item.scope);
      if (selector === "[data-constant-business-key], [data-constant-component-value]") return [key, ...components.map((item) => item.input)];
      return [];
    },
  };
  const requests = [];
  vm.runInNewContext(source, {
    FormData, URLSearchParams,
    Event: class { constructor(type) { this.type = type; } },
    window: { impodoMappingEditor: { mappingForm: { querySelector: () => ({ value: "csrf" }) } } },
    document: {
      addEventListener: (_name, callback) => callback(),
      querySelectorAll: (selector) => selector === "[data-relation-mapping-row]" ? [row] : [],
      createElement: () => new Control(),
    },
    fetch: async (_url, options) => {
      requests.push(Object.fromEntries(options.body));
      const response = responses[Math.min(requests.length - 1, responses.length - 1)];
      return { ok: response.ok, json: async () => response };
    },
  });
  return { provider, key, components, choice, search, status, check, requests };
}

test("account code uses one exact value without a second company choice", async () => {
  const current = fixture();
  assert.equal(current.components[0].label.textContent, "Exact code");
  assert.equal(current.components[0].scope.disabled, true);
  assert.equal(current.components[1].hidden, true);
  current.components[0].input.value = "440000";
  await current.check.fire("click");
  assert.equal(current.requests[0].constant_value, "440000");
  assert.equal(current.requests[0].business_key_id, "code-key");
  assert.equal(current.requests[0].target_field, "property_account_payable_id");
  current.search.value = "suppliers";
  await current.search.fire("input");
  assert.equal(current.requests.length, 1);
  assert.deepEqual(current.choice.options.map((option) => option.value), ["", "440000"]);
  current.choice.value = "440000";
  await current.choice.fire("change");
  assert.equal(current.components[0].input.value, "440000");
  assert.match(current.status.textContent, /all 150 Suppliers rows/);
});

test("changing to exact name clears prior choices and changes the value label", async () => {
  const current = fixture();
  await current.check.fire("click");
  current.key.value = "name-key";
  await current.key.fire("change");
  assert.equal(current.components[0].label.textContent, "Exact name");
  assert.equal(current.components[0].input.value, "");
  assert.equal(current.choice.options.length, 1);
  assert.equal(current.components[0].scope.disabled, true);
});

test("failed checks retain the entered account and permit a successful retry", async () => {
  const current = fixture([
    { ok: false, detail: "Choose another matching rule and check again." },
    { ok: true, target_choices: [{ value: "440000", label: "Suppliers (440000)" }] },
  ]);
  current.components[0].input.value = "440000";
  await current.check.fire("click");
  assert.equal(current.components[0].input.value, "440000");
  assert.match(current.status.textContent, /Choose another matching rule/);
  await current.check.fire("click");
  assert.equal(current.choice.options[1].value, "440000");
});
