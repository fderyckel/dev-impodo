// Run with node --test tests/e2e/mapping-selection-choices.test.cjs.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const window = {};
vm.runInNewContext(readFileSync(path.join(__dirname,
  "../../src/impodo/web/static/mapping-selection-choices.js"), "utf8"), { window });
const { consumeAcceptedRefreshes } = window.impodoSelectionChoices;
const operation = {
  entries: [["selection_refreshes", JSON.stringify(["accepted-language", "accepted-status"])]],
};

test("accepted refreshes leave the next save and an open editor using current choices", () => {
  const staged = new Map([["res.partner.lang", "accepted-language"]]);
  const storage = { value: JSON.stringify([...staged.values()]) };
  const active = { selectionRefresh: "accepted-language" };
  consumeAcceptedRefreshes(staged, storage, active, operation);
  assert.equal(staged.size, 0);
  assert.equal(storage.value, "[]");
  assert.equal(active.selectionRefresh, null);
  // A repeated receipt is harmless.
  consumeAcceptedRefreshes(staged, storage, active, operation);
  assert.equal(storage.value, "[]");
});

test("a newer refresh staged during a save survives the older operation's receipt", () => {
  const staged = new Map([
    ["res.partner.lang", "new-language"],
    ["res.partner.status", "accepted-status"],
  ]);
  const storage = { value: JSON.stringify([...staged.values()]) };
  const active = { selectionRefresh: "new-language" };
  consumeAcceptedRefreshes(staged, storage, active, operation);
  assert.deepEqual(JSON.parse(storage.value), ["new-language"]);
  assert.equal(active.selectionRefresh, "new-language");
});
