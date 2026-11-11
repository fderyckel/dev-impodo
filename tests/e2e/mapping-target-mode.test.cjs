// Run with node --test tests/e2e/mapping-target-mode.test.cjs.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname,
  "../../src/impodo/web/static/mapping-target-mode.js"), "utf8");

function table(value, policy = "block") {
  const listeners = new Map();
  const mode = { value, addEventListener: (name, listener) => listeners.set(name, listener) };
  const existing = { value: policy, disabled: false };
  const create = { hidden: false, open: true };
  const update = { hidden: false };
  const updateOnly = { hidden: false };
  return {
    mode, existing, create, update, updateOnly,
    querySelector: (selector) => ({
      "[data-mapping-target-mode]": mode,
      "[data-mapping-create-policy]": create,
      "[data-mapping-update-summary]": update,
      "[data-mapping-update-only-summary]": updateOnly,
    })[selector],
    choose(value) { mode.value = value; listeners.get("change")(); },
  };
}

function initialize(...tables) {
  vm.runInNewContext(source, { document: {
    querySelectorAll: () => tables,
    addEventListener: (name, listener) => {
      assert.equal(name, "DOMContentLoaded");
      listener();
    },
  } });
}

test("updates and reference-only tables hide the create-only decision on entry", () => {
  for (const mode of ["upsert", "update", "reference"]) {
    const current = table(mode);
    initialize(current);
    assert.equal(current.create.hidden, true);
    assert.equal(current.update.hidden, !["upsert", "update"].includes(mode));
    assert.equal(current.updateOnly.hidden, mode !== "update");
  }
});

test("switching modes retains the operator's unsaved create-only choice", () => {
  const current = table("create", "unchanged");
  initialize(current);
  assert.equal(current.create.hidden, false);
  assert.equal(current.update.hidden, true);
  current.choose("upsert");
  assert.equal(current.create.hidden, true);
  assert.equal(current.update.hidden, false);
  current.choose("reference");
  assert.equal(current.create.hidden, true);
  assert.equal(current.update.hidden, true);
  current.choose("update");
  assert.equal(current.create.hidden, true);
  assert.equal(current.update.hidden, false);
  assert.equal(current.updateOnly.hidden, false);
  current.choose("create");
  assert.equal(current.create.hidden, false);
  assert.equal(current.create.open, true);
  assert.equal(current.existing.value, "unchanged");
  assert.equal(current.existing.disabled, false);
});

test("each table follows its own mode and captured Odoo records need no selector", () => {
  const first = table("upsert");
  const second = table("create");
  initialize(first, second, { querySelector: () => null });
  first.choose("reference");
  assert.equal(first.create.hidden, true);
  assert.equal(first.update.hidden, true);
  assert.equal(second.create.hidden, false);
  assert.equal(second.update.hidden, true);
});
