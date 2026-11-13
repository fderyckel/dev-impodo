"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "../../src/impodo/web/static/schema-scope-fields.js"), "utf8");
function fixture(initial = []) {
  let changes = 0;
  const controls = [
    ["name", "Location Name"], ["company_id", "Company"], ["location_id", "Parent Location"],
  ].map(([value, label]) => ({
    value, checked: initial.includes(value), disabled: false,
    dataset: { fieldLabel: label }, listeners: {},
    addEventListener(name, handler) { this.listeners[name] = handler; },
  }));
  const input = { value: initial.join(", ") };
  const summary = { textContent: "" };
  const context = { window: {} };
  vm.runInNewContext(script, context);
  const controller = context.window.ImpodoSchemaScopes.bind({
    querySelectorAll: () => controls, querySelector: () => summary,
  }, input, () => { changes += 1; });
  return {
    controls, input, summary, controller, changes: () => changes,
    toggle(value, checked) {
      const control = controls.find((item) => item.value === value);
      control.checked = checked;
      control.listeners.change();
    },
  };
}
test("Parent Location and Company can both be selected with labelled checkboxes", () => {
  const view = fixture();
  view.toggle("location_id", true);
  view.toggle("company_id", true);
  assert.equal(view.input.value, "location_id, company_id");
  assert.equal(view.summary.textContent, "Parent Location, Company");
  assert.equal(view.changes(), 2);
});
test("existing scope order survives changes independently of checkbox display order", () => {
  const view = fixture(["location_id", "company_id"]);
  view.toggle("name", true);
  assert.equal(view.input.value, "location_id, company_id, name");
  view.toggle("company_id", false);
  assert.equal(view.input.value, "location_id, name");
});
test("clearing every scope records an explicit empty selection", () => {
  const view = fixture(["location_id", "company_id"]);
  view.toggle("location_id", false);
  view.toggle("company_id", false);
  assert.equal(view.input.value, "");
  assert.equal(view.summary.textContent, "No additional scope");
});
test("matching fields cannot be newly selected as scope and existing conflicts stay correctable", () => {
  const view = fixture(["location_id"]);
  view.controller.setMatchingFields(["name", "location_id"]);
  assert.equal(view.controls[0].disabled, true);
  assert.equal(view.controls[2].disabled, false);
  view.controller.setMatchingFields(["company_id"]);
  assert.equal(view.controls[0].disabled, false);
});
test("programmatic scope updates preserve order and labels", () => {
  const view = fixture();
  view.controller.setFields(["location_id", "company_id"]);
  assert.equal(view.input.value, "location_id, company_id");
  assert.equal(view.summary.textContent, "Parent Location, Company");
  assert.equal(view.controls[1].checked, true);
  assert.equal(view.controls[2].checked, true);
});
