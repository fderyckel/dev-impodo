// Run with node --test tests/e2e/schema-journey.test.cjs.
"use strict";
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tagName = "DIV", dataset = {}) {
    this.tagName = tagName;
    this.dataset = dataset;
    this.attributes = new Map();
    this.children = [];
    this.listeners = new Map();
    this.hidden = false;
    this.disabled = false;
    this.classes = new Set();
    this.classList = { toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name) };
  }
  setAttribute(name, value) { this.attributes.set(name, value); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  hasAttribute(name) {
    if (name.startsWith("data-")) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
      return Object.hasOwn(this.dataset, key);
    }
    return this.attributes.has(name) || (["id", "href"].includes(name) && Boolean(this[name]));
  }
  matches(selector) {
    return selector.split(",").some((part) => {
      const match = part.trim().match(/^([a-z]+)?(?:\.([\w-]+))?(?:\[([^=\]]+)(?:="([^"]*)")?\])?$/i);
      if (!match) return false;
      const [, tag, className, attribute, value] = match;
      return (!tag || this.tagName === tag.toUpperCase()) &&
        (!className || this.classes.has(className)) &&
        (!attribute || (this.hasAttribute(attribute) && (value === undefined || this.getAttribute(attribute) === value)));
    });
  }
  append(child) { this.children.push(child); child.parentElement = this; return child; }
  closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
  querySelectorAll(selector) {
    return this.children.flatMap((child) => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  focus() { this.focused = true; }
  scrollIntoView() { this.scrolled = true; }
}

function page({ step = "records", url = "https://impodo.test/workspaces/one/schema", prepare = () => {} } = {}) {
  const body = new Element();
  const root = body.append(new Element("DIV", { schemaJourney: "", step }));
  const heading = root.append(new Element("H1", { schemaHeading: "" }));
  const title = root.append(new Element("P", { schemaStepTitle: "" }));
  const panels = {}, links = {};
  for (const name of ["records", "related", "matching"]) {
    panels[name] = root.append(new Element("DIV", { schemaStepPanel: name }));
    links[name] = body.append(new Element("A"));
    links[name].href = `/workspaces/one/schema?step=${name}`;
    links[name].classes.add("sidebar-subnav-link");
  }
  const form = panels.matching.append(new Element("FORM"));
  form.action = "/workspaces/one/schema/govern";
  const cards = [true, false].map((confirmed, index) => {
    const card = form.append(new Element("DETAILS", { schemaKeyCard: "", keyDecision: "", keyConfirmed: String(confirmed) }));
    card.id = `matching-rule-${index}`;
    const input = card.append(new Element("INPUT"));
    input.name = `primary_key_field_${index}`;
    input.value = `unsaved-${index}`;
    return card;
  });
  const location = {};
  const setUrl = (value) => {
    const current = new URL(value, location.href || url);
    Object.assign(location, { href: current.href, origin: current.origin, pathname: current.pathname, hash: current.hash });
  };
  setUrl(url);
  const documentListeners = new Map(), windowListeners = new Map(), pushes = [];
  const document = { title: "Odoo data · Project · Impodo",
    querySelector: (selector) => body.querySelector(selector),
    querySelectorAll: (selector) => body.querySelectorAll(selector),
    getElementById: (id) => body.querySelectorAll("[id]").find((element) => element.id === id),
    addEventListener: (name, listener) => documentListeners.set(name, listener) };
  const window = { location, queueMicrotask,
    addEventListener: (name, listener) => windowListeners.set(name, listener),
    history: { state: { preserved: true }, pushState(state, _title, value) { pushes.push({ state, value }); setUrl(value); } } };
  prepare({ root, panels, cards, form });
  const source = readFileSync(path.join(__dirname, "../../src/impodo/web/static/schema-journey.js"), "utf8");
  vm.runInNewContext(source, { window, document, URL });
  const journey = window.ImpodoSchemaJourney.bind(root);
  const click = (target, modifiers = {}) => {
    let prevented = false;
    documentListeners.get("click")({ target, button: 0, defaultPrevented: false, ...modifiers,
      preventDefault() { prevented = true; } });
    return prevented;
  };
  return { root, panels, cards, form, links, heading, title, journey, document, pushes, window, click, setUrl,
    popstate: () => windowListeners.get("popstate")(), hashchange: () => windowListeners.get("hashchange")(),
    invalid: (target) => root.listeners.get("invalid")({ target }),
    rejectedSubmit: () => documentListeners.get("submit")({ target: form, defaultPrevented: true }) };
}

test("sidebar switches retain every unsaved matching value and the complete form", () => {
  const view = page();
  const before = view.form.querySelectorAll("input");
  assert.equal(view.click(view.links.matching), true);
  assert.equal(view.panels.records.hidden, true);
  assert.equal(view.panels.matching.hidden, false);
  assert.equal(view.title.textContent, "Step 3 of 3 · Matching rules");
  assert.equal(view.heading.focused, true);
  assert.equal(view.links.matching.getAttribute("aria-current"), "page");
  assert.equal(view.document.title, "Matching rules · Odoo data · Project · Impodo");
  view.click(view.links.related);
  assert.deepEqual(view.form.querySelectorAll("input"), before);
  assert.deepEqual(before.map((input) => [input.name, input.value, input.disabled]), [
    ["primary_key_field_0", "unsaved-0", false], ["primary_key_field_1", "unsaved-1", false],
  ]);
  assert.equal(view.form.action, "/workspaces/one/schema/govern");
  assert.deepEqual(view.pushes[0].state, { preserved: true });
});

test("Back and Forward restore the location without saving or replacing controls", () => {
  const view = page();
  view.click(view.links.related);
  view.setUrl("https://impodo.test/workspaces/one/schema?step=matching");
  view.popstate();
  assert.equal(view.root.dataset.step, "matching");
  assert.equal(view.cards[0].children[0].value, "unsaved-0");
  assert.equal(view.links.related.getAttribute("aria-current"), null);
  view.setUrl("https://impodo.test/workspaces/one/schema?step=unknown");
  view.popstate();
  assert.equal(view.root.dataset.step, "records");
  assert.equal(view.pushes.length, 1);
});

test("a direct anchor reveals its panel and closed ancestors before focus", () => {
  const view = page();
  const field = view.cards[0].children[0];
  field.id = "chosen-field";
  const link = new Element("A");
  link.href = "#chosen-field";
  view.click(link);
  assert.equal(view.root.dataset.step, "matching");
  assert.equal(view.cards[0].open, true);
  assert.equal(field.focused, true);
  assert.equal(field.scrolled, true);
  assert.match(view.window.location.href, /step=matching#chosen-field$/);
});

test("initial supporting-save anchors reveal content while existing restoration keeps focus", () => {
  let group;
  const view = page({ url: "https://impodo.test/workspaces/one/schema#supporting-data-review", prepare({ panels }) {
    group = panels.related.append(new Element("DETAILS", { schemaRelatedGroup: "" }));
    group.id = "supporting-data-review";
  } });
  assert.equal(view.root.dataset.step, "related");
  assert.equal(group.open, true);
  assert.equal(group.focused, undefined);
  assert.equal(group.scrolled, undefined);
  assert.equal(view.pushes.length, 0);
});

test("initial server errors take precedence and open their matching card", () => {
  const view = page({ url: "https://impodo.test/workspaces/one/schema?step=records", prepare({ cards }) {
    cards[0].append(new Element("P", { keyServerError: "" }));
  } });
  assert.equal(view.root.dataset.step, "matching");
  assert.equal(view.cards[0].open, true);
  assert.equal(view.cards[1].open, false);
  assert.equal(view.cards[0].children[1].focused, true);
});

test("only one matching editor is open without disabling hidden form values", () => {
  const view = page({ prepare({ cards }) { cards[0].open = true; } });
  assert.equal(view.cards[1].open, true, "the first unfinished rule opens initially");
  view.cards[0].open = true;
  view.cards[0].listeners.get("toggle")();
  assert.equal(view.cards[1].open, false);
  assert.equal(view.cards[1].children[0].disabled, false);
});

test("the initial server recovery panel takes precedence over a saved query location", () => {
  const view = page({ step: "records", url: "https://impodo.test/workspaces/one/schema?step=matching" });
  assert.equal(view.root.dataset.step, "records");
  assert.equal(view.panels.records.hidden, false);
  assert.equal(view.panels.matching.hidden, true);
});

test("initial explicit field anchors reveal and focus their owning editor", () => {
  const view = page({ url: "https://impodo.test/workspaces/one/schema#primary-field", prepare({ cards }) {
    cards[0].children[0].id = "primary-field";
  } });
  assert.equal(view.root.dataset.step, "matching");
  assert.equal(view.cards[0].open, true);
  assert.equal(view.cards[0].children[0].focused, true);
  assert.equal(view.cards[0].children[0].scrolled, true);
  assert.equal(view.pushes.length, 0);
});

test("compact rule summaries reflect edits after the existing field editor updates", async () => {
  const view = page();
  const card = view.cards[0];
  const selection = card.append(new Element("SPAN", { keySelectionSummary: "" }));
  const summary = card.append(new Element("SMALL", { schemaRuleSummary: "" }));
  selection.textContent = "Previous value";
  view.root.listeners.get("change")({ target: card.children[0] });
  selection.textContent = "Reference within Company";
  await new Promise(queueMicrotask);
  assert.equal(summary.textContent, "Reference within Company");
  assert.equal(card.children[0].value, "unsaved-0");
});

test("native validation opens the first invalid panel and group per validation turn", async () => {
  const view = page();
  const first = view.cards[0].children[0];
  const second = view.panels.related.append(new Element("SELECT"));
  view.invalid(first);
  view.invalid(second);
  assert.equal(view.root.dataset.step, "matching");
  assert.equal(view.cards[0].open, true);
  assert.equal(first.focused, true);
  assert.equal(second.focused, undefined);
  await new Promise(queueMicrotask);
  view.invalid(second);
  assert.equal(view.root.dataset.step, "related");
  assert.equal(second.focused, true);
});

test("a client-side matching conflict reveals its closed rule before correction", () => {
  const view = page();
  const error = view.cards[0].append(new Element("P", { keyFieldError: "" }));
  error.hidden = false;
  view.rejectedSubmit();
  assert.equal(view.root.dataset.step, "matching");
  assert.equal(view.cards[0].open, true);
  assert.equal(error.focused, true);
});

test("modified, external, download and unrelated navigation keeps browser behavior", () => {
  const view = page();
  assert.equal(view.click(view.links.related, { ctrlKey: true }), false);
  assert.equal(view.click(view.links.related, { button: 1 }), false);
  const external = new Element("A");
  external.href = "https://other.test/workspaces/one/schema?step=related";
  assert.equal(view.click(external), false);
  const other = new Element("A");
  other.href = "/workspaces/one/mapping?step=matching";
  assert.equal(view.click(other), false);
  const download = new Element("A");
  download.href = view.links.related.href;
  download.setAttribute("download", "");
  assert.equal(view.click(download), false);
  assert.equal(view.root.dataset.step, "records");
});

test("an in-flight save keeps its current panel", () => {
  const view = page();
  view.form.setAttribute("aria-busy", "true");
  assert.equal(view.click(view.links.related), true);
  assert.equal(view.root.dataset.step, "records");
  assert.equal(view.pushes.length, 0);
});

test("hash changes reveal the named choice and malformed hashes remain harmless", () => {
  const view = page();
  view.cards[0].children[0].id = "choice";
  view.setUrl("https://impodo.test/workspaces/one/schema#choice");
  view.hashchange();
  assert.equal(view.root.dataset.step, "matching");
  assert.equal(view.cards[0].children[0].focused, true);
  view.setUrl("https://impodo.test/workspaces/one/schema#%E0%A4%A");
  assert.doesNotThrow(() => view.hashchange());
  assert.equal(view.root.dataset.step, "records");
});
