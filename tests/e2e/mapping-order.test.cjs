// Drive the shipped order-panel handlers without a running Odoo instance.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = readFileSync(join(__dirname, "../../src/impodo/web/static/mapping-order.js"), "utf8");
const box = () => ({
  dataset: {}, children: [], listeners: {},
  addEventListener(type, callback) { this.listeners[type] = callback; },
  querySelector(selector) { return this.children[selector]; },
});

function initialize(order, running, { dirty = false, busy = false, reordering = false } = {}) {
  const location = { href: "https://impodo.test/mapping?step=tables&task=links&mapping_dataset=1",
    reload() { this.reloaded = true; } };
  const history = { replaceState(_state, _unused, url) { location.href = String(url); } };
  const window = { location, history, impodoMappingEditor: { getState: () => ({ dirty, busy }) },
    setTimeout(cb) { this.scheduled = cb; } };
  const document = { addEventListener(_name, cb) { cb(); }, querySelector(selector) {
    if (selector === "[data-matching-order-form]") return order;
    if (selector === "[data-matching-order-check]") return running;
    return reordering;
  }};
  const fetch = async () => ({ ok: true, json: async () => ({
    terminal: true, check_id: "check-one", message: "Check finished", progress_percent: 100,
  }) });
  vm.runInNewContext(script, { document, window, fetch, URL });
  return window;
}

test("pointer drag changes positions, boundary buttons and saved permutation; cancel restores them", () => {
  const form = box(), toggle = box(), list = box(), announcement = box();
  Object.assign(form.children, { "[data-matching-order-toggle]": toggle,
    "[data-matching-order-toggle-label]": box(), "[data-matching-order-list]": list,
    "[data-matching-order-announcement]": announcement });
  const rows = ["categories", "products"].map(id => {
    const row = box();
    row.dataset.matchingOrderRow = id;
    row.closest = () => row;
    row.classList = { add() {}, remove() {} };
    row.getBoundingClientRect = () => ({ top: 0, height: 40 });
    Object.assign(row.children, { "[data-matching-order-position]": box(), strong: { textContent: id },
      'button[value^="move_up:"]': box(), 'button[value^="move_down:"]': box() });
    return row;
  });
  list.querySelectorAll = () => rows;
  list.insertBefore = (row, before) => {
    rows.splice(rows.indexOf(row), 1);
    rows.splice(before ? rows.indexOf(before) : rows.length, 0, row);
  };
  list.append = row => list.insertBefore(row, null);
  initialize(form);
  toggle.checked = true;
  toggle.listeners.change();
  const [categories, products] = rows;
  list.listeners.dragstart({ target: products });
  list.listeners.dragover({ target: categories, clientY: 0, preventDefault() {} });
  list.listeners.drop({ preventDefault() {} });
  assert.deepEqual(rows, [products, categories]);
  assert.equal(products.children['button[value^="move_up:"]'].disabled, true);
  assert.match(announcement.textContent, /products moved to position 1/);
  toggle.checked = false;
  toggle.listeners.change();
  assert.deepEqual(rows, [categories, products]);
  assert.equal(products.draggable, false);
});

test("a terminal check preserves the current URL and waits when editing or saving", async () => {
  for (const state of [{}, { dirty: true }, { busy: true }, { reordering: true }]) {
    const running = box();
    running.dataset.statusUrl = "/status";
    const message = box();
    running.children["[data-matching-order-check-message]"] = message;
    const window = initialize(null, running, state);
    await window.scheduled();
    const query = new URL(window.location.href).searchParams;
    assert.equal(query.get("order_check"), "check-one");
    assert.equal(query.get("mapping_dataset"), "1");
    assert.equal(query.get("task"), "links");
    if (Object.keys(state).length) {
      assert.equal(window.location.reloaded, undefined);
      assert.match(message.textContent, /Finish your edits/);
    } else assert.equal(window.location.reloaded, true);
  }
});
