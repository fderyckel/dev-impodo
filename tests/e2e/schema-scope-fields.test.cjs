"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "../../src/impodo/web/static/schema-scope-fields.js"), "utf8");
function fixture(initial = []) {
  let changes = 0;
  const element = () => ({
    dataset: {}, attributes: {}, listeners: {}, children: [], hidden: false,
    classList: { toggle() {} },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(name, handler) { this.listeners[name] = handler; },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    focus() { this.focused = true; },
  });
  const document = { createElement: element };
  const controls = [
    ["name", "Location Name"], ["company_id", "Company"], ["location_id", "Parent Location"],
  ].map(([value, label]) => {
    const row = element();
    return { ...element(), value, checked: initial.includes(value), disabled: false,
      dataset: { fieldLabel: label }, row, closest: () => row };
  });
  const input = { value: initial.join(", ") };
  const summary = element();
  const selected = { ...element(), ownerDocument: document };
  const none = element(), clear = element(), search = element();
  const tools = element(), noResults = element(), disclosure = element();
  const selectors = {
    "[data-key-scope-selection]": summary, "[data-scope-selected]": selected,
    "[data-scope-none]": none, "[data-scope-clear]": clear,
    "[data-scope-search]": search, "[data-scope-search-tools]": tools,
    "[data-scope-no-results]": noResults, "[data-scope-disclosure]": disclosure,
  };
  const context = { window: {} };
  vm.runInNewContext(script, context);
  const controller = context.window.ImpodoSchemaScopes.bind({
    querySelectorAll: () => controls, querySelector: (selector) => selectors[selector],
  }, input, () => { changes += 1; });
  return {
    controls, input, summary, selected, none, clear, search, tools, noResults, disclosure,
    controller, changes: () => changes,
    labels: () => selected.children.map((chip) => chip.children[0].textContent),
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
  assert.deepEqual(view.labels(), ["Parent Location", "Company"]);
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
  assert.equal(view.none.hidden, false);
  assert.equal(view.clear.hidden, true);
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
  assert.deepEqual(view.labels(), ["Parent Location", "Company"]);
  assert.equal(view.controls[1].checked, true);
  assert.equal(view.controls[2].checked, true);
});

test("selected fields can be removed directly from their labels", () => {
  const view = fixture(["location_id", "company_id"]);
  const removeParent = view.selected.children[0];
  assert.equal(removeParent.attributes["aria-label"], "Remove Parent Location from scope");
  removeParent.listeners.click();
  assert.equal(view.input.value, "company_id");
  assert.deepEqual(view.labels(), ["Company"]);
  assert.equal(view.changes(), 1);
  assert.equal(view.summary.focused, true);
});

test("search filters available fields without changing selected scope", () => {
  const view = fixture(["location_id", "company_id"]);
  view.search.value = "parent";
  view.search.listeners.input();
  assert.equal(view.controls[2].row.hidden, false);
  assert.equal(view.controls[1].row.hidden, true);
  assert.equal(view.input.value, "location_id, company_id");
  assert.deepEqual(view.labels(), ["Parent Location", "Company"]);
  assert.equal(view.changes(), 0);
  view.search.value = "absent field";
  view.search.listeners.input();
  assert.equal(view.noResults.hidden, false);
  view.search.value = "";
  view.search.listeners.input();
  assert.equal(view.noResults.hidden, true);
  assert.ok(view.controls.every((control) => !control.row.hidden));
});

test("Clear removes all fields in one action and Escape closes the picker", () => {
  const view = fixture(["location_id", "company_id"]);
  view.clear.listeners.click();
  assert.equal(view.input.value, "");
  assert.equal(view.selected.children.length, 0);
  assert.equal(view.none.hidden, false);
  assert.equal(view.changes(), 1);
  view.disclosure.open = true;
  view.disclosure.listeners.keydown({ key: "Escape" });
  assert.equal(view.disclosure.open, false);
  assert.equal(view.summary.focused, true);
});

test("Enter in field search does not submit the matching-rule form", () => {
  const view = fixture();
  let prevented = false;
  view.search.listeners.keydown({ key: "Enter", preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(view.changes(), 0);
});
