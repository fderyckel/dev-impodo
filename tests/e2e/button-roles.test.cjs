// Run with node --test tests/e2e/button-roles.test.cjs.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../../src/impodo/web/static/button-roles.js"), "utf8");
const window = {};
vm.runInNewContext(source, { window, document: { addEventListener() {} } });
const { selectNext } = window.ImpodoButtonRoles;
const candidate = (priority, options = {}) => ({
  priority, visible: true, disabled: false, ...options,
  getAttribute(name) { return name === "data-next-step" ? this.priority : this[name] || null; },
});
const choose = (items, options = {}) => selectNext(items, { visible: (item) => item.visible, ...options });

test("the footer wins over an earlier duplicate of the same transition", () => {
  const card = candidate("100"), footer = candidate("100");
  assert.equal(choose([card, footer]), footer);
});

test("changing steps makes only the visible transition eligible", () => {
  const records = candidate("100"), matching = candidate("100", { visible: false });
  assert.equal(choose([records, matching]), records);
  records.visible = false;
  matching.visible = true;
  assert.equal(choose([records, matching]), matching);
});

test("saved work advances while an unsaved change gives confirmation priority", () => {
  const confirm = candidate("100"), onward = candidate("200");
  assert.equal(choose([confirm, onward]), onward);
  confirm.priority = "300";
  assert.equal(choose([confirm, onward]), confirm);
});

test("an edited advancing form takes priority over a later page transition", () => {
  let dirty = false;
  const local = candidate("100", { form: { hasAttribute: () => dirty } });
  const onward = candidate("200");
  assert.equal(choose([local, onward]), onward);
  dirty = true;
  assert.equal(choose([local, onward]), local);
  dirty = false;
  assert.equal(choose([local, onward]), onward);
});

test("a dialog owns action focus and can have no advancing action", () => {
  const page = candidate("500"), dialog = candidate("100");
  const scope = { contains: (item) => item === dialog };
  assert.equal(choose([page, dialog], { scope }), dialog);
  dialog.disabled = true;
  assert.equal(choose([page, dialog], { scope }), null);
});

test("disabled and unavailable actions never become the next step", () => {
  const first = candidate("300", { disabled: true });
  const second = candidate("200", { "aria-disabled": "true" });
  const available = candidate("100");
  assert.equal(choose([first, second, available]), available);
  available.visible = false;
  assert.equal(choose([first, second, available]), null);
});

test("plain actions, help and deletion remain outside the advancing selection", () => {
  assert.equal(choose([]), null);
  assert.equal(choose([candidate("invalid")]), null);
});

test("selecting among many transitions never executes their operations", () => {
  const items = Array.from({ length: 100 }, (_, index) => candidate(String(index), {
    click() { throw new Error("Colour selection must not navigate or submit"); },
    submit() { throw new Error("Colour selection must not execute an operation"); },
  }));
  assert.equal(choose(items), items[99]);
});
