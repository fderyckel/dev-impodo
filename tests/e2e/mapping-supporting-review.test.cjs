// Run with node --test tests/e2e/mapping-supporting-review.test.cjs.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname,
  "../../src/impodo/web/static/mapping-supporting-review.js"), "utf8");

function editor({ visible = true, sameTable = true, dirty = true } = {}) {
  const documentEvents = new Map();
  const hostEvents = new Map();
  let focused = false;
  let scrolled = false;
  let prevented = false;
  let requested;
  let revealed;
  const input = { value: "Keep my unsaved field", focus() { focused = true; } };
  class Details { constructor() { this.open = false; this.parentElement = null; } }
  const disclosure = new Details();
  const row = {
    parentElement: disclosure,
    setAttribute() {},
    scrollIntoView() { scrolled = true; },
    querySelector(selector) {
      assert.match(selector, /not\(\[type='hidden'\]\)/);
      return input;
    },
  };
  const error = { hidden: true, textContent: "" };
  const catalog = { dispatchEvent(event) { requested = event.detail; } };
  const dataset = { dataset: { mappingDataset: "boms" }, querySelector() { return catalog; } };
  const host = {
    addEventListener(name, callback) { hostEvents.set(name, callback); },
    querySelector(selector) { return selector === "[data-related-review-error]" ? error : null; },
  };
  vm.runInNewContext(source, {
    window: { impodoMappingJourney: { reveal(target) { revealed = target; } } }, HTMLDetailsElement: Details,
    CustomEvent: class { constructor(_name, options) { this.detail = options.detail; } },
    document: {
      addEventListener(name, callback) { documentEvents.set(name, callback); },
      querySelector(selector) {
        if (selector === "[data-related-review-host]") return host;
        if (selector === "[data-mapping-save-status]") return { classList: { contains() { return dirty; } } };
        return null;
      },
      querySelectorAll() { return sameTable ? [dataset] : []; },
      getElementById(id) {
        assert.equal(id, "relationship-2-product_tmpl_id");
        return visible ? row : null;
      },
    },
  });
  documentEvents.get("DOMContentLoaded")();
  const link = {
    href: "/mapping?mapping_dataset=2&relation_target=product_tmpl_id#relationship-2-product_tmpl_id",
    dataset: { reviewAnchor: "relationship-2-product_tmpl_id", datasetId: "boms" },
  };
  hostEvents.get("click")({
    target: { closest(selector) { return selector === "[data-review-related-field]" ? link : null; } },
    preventDefault() { prevented = true; },
  });
  return { input, disclosure, error, get focused() { return focused; },
    get scrolled() { return scrolled; }, get prevented() { return prevented; },
    get requested() { return requested; }, get revealed() { return revealed === row; } };
}

test("review opens the exact Product field and focuses a usable control", () => {
  const page = editor();
  assert.equal(page.prevented, true);
  assert.equal(page.disclosure.open, true);
  assert.equal(page.scrolled, true);
  assert.equal(page.focused, true);
  assert.equal(page.revealed, true);
  assert.equal(page.input.value, "Keep my unsaved field");
});

test("a field on another page uses the existing catalog loader", () => {
  const page = editor({ visible: false });
  assert.equal(page.prevented, true);
  assert.match(page.requested.url, /relation_target=product_tmpl_id/);
  assert.equal(page.requested.targetId, "relationship-2-product_tmpl_id");
  assert.equal(page.input.value, "Keep my unsaved field");
});

test("opening another table retains unsaved edits and requests saving first", () => {
  const page = editor({ visible: false, sameTable: false });
  assert.equal(page.prevented, true);
  assert.equal(page.error.hidden, false);
  assert.match(page.error.textContent, /Save progress/);
  assert.equal(page.input.value, "Keep my unsaved field");
});
