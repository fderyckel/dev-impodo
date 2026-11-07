// Run with node --test tests/e2e/mapping-value-match-origins.test.cjs.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tag = "div", dataset = {}) {
    this.tagName = tag; this.dataset = dataset; this.children = [];
    this.listeners = new Map(); this.hidden = false; this.textContent = "";
    this.name = ""; this._value = "";
  }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  remove() { this.parent.children = this.parent.children.filter((child) => child !== this); }
  get options() { return this.children; }
  get value() { return this.tagName === "select" ? (this.selectedOptions[0]?.value || "") : this._value; }
  set value(value) {
    if (this.tagName === "select") this.children.forEach((child) => { child.selected = child.value === value; });
    else this._value = value;
  }
  get selectedOptions() {
    const selected = this.children.filter((child) => child.selected);
    return selected.length ? selected : this.children.slice(0, 1);
  }
  matches(selector) {
    return selector.split(",").some((part) => {
      const match = part.trim().match(/^(\w+)?\[([^\]^=]+)(\^?=\"([^\"]*)\")?\]$/);
      if (!match || (match[1] && match[1] !== this.tagName)) return false;
      const key = match[2].replace(/^data-/, "").replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
      const value = match[2].startsWith("data-") ? this.dataset[key] : this[match[2]];
      return match[3] ? (match[3].startsWith("^") ? String(value).startsWith(match[4]) : value === match[4]) : value !== undefined;
    });
  }
  querySelectorAll(selector) { return this.children.flatMap((child) => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { return this.matches(selector) ? this : this.parent?.closest(selector); }
  setAttribute() {}
  addEventListener(name, listener) { const list = this.listeners.get(name) || []; list.push(listener); this.listeners.set(name, list); }
  dispatchEvent(event) { for (const listener of this.listeners.get(event.type) || []) listener(event); }
  fire(name, target = this) { this.dispatchEvent({ type: name, target }); }
  focus() {}
  showModal() { this.open = true; }
  close() { this.open = false; this.fire("close"); }
}
class Input extends Element { constructor() { super("input"); } }
const flush = () => new Promise(setImmediate);

function fixture({ incoming = 7, targets = 11, saved = [], origin = "target_then_dataset" } = {}) {
  const document = new Element();
  document.createElement = (tag) => tag === "input" ? new Input() : new Element(tag);
  const form = new Element("form"); document.append(form);
  const row = new Element("div", { relationMappingRow: "" }); form.append(row);
  const select = (name, value, dataset = {}) => {
    const element = new Element("select", dataset); element.name = name;
    const option = new Element("option"); option.value = value; element.append(option); row.append(element);
    return element;
  };
  select("relation_origin_0_0", origin);
  select("relation_source_0_0", "vendor.term");
  select("relation_key_0_0", "term:name");
  select("relation_dataset_0_0", "terms");
  select("relation_categorical_policy_0_0", "EXACT_BUSINESS_KEY", { categoricalPolicy: "" });
  const storage = new Input(); storage.dataset.valueMatchStorage = ""; storage.value = JSON.stringify(saved); row.append(storage);
  const summary = new Element("small", { valueMatchSummary: "" }); row.append(summary);
  row.append(new Element("button", { valueMatchPrerequisite: "" }));
  const trigger = new Element("button", { openValueMatch: "", valueMatchKind: "relationship",
    datasetId: "vendors", targetModel: "res.partner", targetField: "property_supplier_payment_term_id", targetLabel: "Vendor Payment Terms" });
  row.append(trigger); form.contains = () => true;
  const dialog = new Element("dialog", { valueMatchDialog: "", valueMatchEndpoint: "/choices" }); document.append(dialog);
  const elements = {};
  for (const key of ["valueMatchTitle", "valueMatchError", "valueMatchStatus", "valueMatchTableWrap", "valueMatchFreshness",
    "valueMatchFreshnessText", "refreshValueMatch", "valueMatchRows", "useValueMatches", "valueMatchSpinner",
    "valueMatchResults", "retryValueMatch", "valueMatchSupport", "valueMatchChoiceHeading", "valueMatchOriginHeading", "valueMatchIncomingNotice"]) {
    elements[key] = new Element("div", { [key]: "" }); dialog.append(elements[key]);
  }
  let payload = {
    source_choices: Array.from({ length: targets + incoming }, (_, i) => ({ value: `TERM${i}`, count: 1 })),
    target_choices: Array.from({ length: targets }, (_, i) => ({ value: `TERM${i}`, label: `TERM${i}` })),
    incoming_table: { dataset_id: "terms", name: "Payment terms", checked: true },
    relationship_resolutions: Array.from({ length: targets + incoming }, (_, i) => ({ source_value: `TERM${i}`,
      status: i < targets ? "target" : "incoming", target_value: i < targets ? `TERM${i}` : "" })),
    target_checked_at: "2026-11-05T12:00:00Z",
  };
  const requests = [];
  let failure = false;
  const window = { crypto: require("node:crypto").webcrypto, queueMicrotask,
    setTimeout: (...args) => setTimeout(...args).unref(), clearTimeout };
  const context = { window, document, HTMLInputElement: Input, HTMLSelectElement: Element,
    FormData, AbortController, Event, Option: Element, fetch: async (_url, request) => {
      requests.push(Object.fromEntries(request.body));
      return { ok: !failure, json: async () => failure ? { detail: "Try again" } : payload };
    } };
  for (const name of ["mapping-value-match-origins", "mapping-value-match"]) {
    vm.runInNewContext(readFileSync(path.join(__dirname, `../../src/impodo/web/static/${name}.js`), "utf8"), context);
  }
  window.impodoMappingValueMatch.create(form);
  return { form, row, dialog, storage, summary, elements, requests,
    open() { form.fire("click", trigger); return flush(); },
    refresh() { elements.refreshValueMatch.fire("click"); return flush(); },
    retry() { elements.retryValueMatch.fire("click"); return flush(); },
    apply() { elements.useValueMatches.fire("click"); },
    selects() { return elements.valueMatchRows.querySelectorAll("select[data-source-value]"); },
    fail(value) { failure = value; }, payload,
  };
}

test("the actual dialog counts both origins, applies only Odoo aliases, and reopens", async () => {
  const page = fixture(); await page.open();
  assert.equal(page.elements.valueMatchStatus.textContent, "18 of 18 matched · 11 in Odoo · 7 from Payment terms");
  assert.equal(page.elements.useValueMatches.textContent, "Use complete matches");
  assert.equal(page.elements.useValueMatches.disabled, false);
  assert.equal(page.requests[0].resolver_origin, "target_then_dataset");
  assert.equal(page.requests[0].incoming_dataset_id, "terms");
  assert.equal(page.selects()[17].options[0].textContent, "TERM17 (from Payment terms)");
  page.apply();
  assert.equal(JSON.parse(page.storage.value).length, 11);
  assert.equal(JSON.parse(page.storage.value).some((item) => item.source_value === "TERM17"), false);
  assert.equal(page.summary.textContent, "All 18 populated source choice(s) matched");
  await page.open();
  assert.equal(page.elements.valueMatchStatus.textContent, "18 of 18 matched · 11 in Odoo · 7 from Payment terms");
});

test("an all-incoming match is usable without inventing Odoo translations", async () => {
  const page = fixture({ targets: 0, incoming: 2 }); await page.open();
  assert.equal(page.elements.useValueMatches.disabled, false);
  assert.equal(page.elements.useValueMatches.textContent, "Use complete matches");
  page.apply(); assert.deepEqual(JSON.parse(page.storage.value), []);
});

test("existing scoped choices prefill exact names and save the complete key", async () => {
  const key = (name, parent) => JSON.stringify([name, parent]);
  const saved = [{ source_value: "TERM1", target_value: key("TERM2", "Parent") }];
  const page = fixture({ targets: 80, incoming: 0, origin: "target_catalog", saved });
  delete page.payload.incoming_table;
  delete page.payload.relationship_resolutions;
  page.payload.scoped_suggestions = Object.fromEntries(
    page.payload.target_choices.slice(0, 79).map(({ value }) => [value, key(value, "Parent")])
  );
  page.payload.target_choices = page.payload.target_choices.map(({ value }) => ({
    value: key(value, "Parent"), label: `${value} (Parent)`,
  }));
  page.payload.target_choices.push(
    { value: key("TERM79", "Other parent"), label: "TERM79 (Other parent)" }
  );
  await page.open();
  assert.equal(page.elements.valueMatchStatus.textContent, "79 of 80 matched");
  assert.equal(page.selects()[0].value, key("TERM0", "Parent"));
  assert.equal(page.selects()[1].value, saved[0].target_value);
  assert.equal(page.selects()[79].value, "");
  page.apply();
  const matches = JSON.parse(page.storage.value);
  assert.equal(matches.length, 79);
  assert.deepEqual(matches[0], { source_value: "TERM0", target_value: key("TERM0", "Parent") });
  assert.deepEqual(matches[1], saved[0]);
});

test("the dialog respects a case-review outcome for a normalized generated key", async () => {
  const page = fixture({ targets: 1, incoming: 0 });
  page.payload.relationship_resolutions[0].status = "case_only";
  page.payload.relationship_resolutions[0].target_value = "";
  await page.open();
  assert.equal(page.elements.valueMatchStatus.textContent, "0 of 1 matched · 0 in Odoo · 0 from Payment terms");
  assert.equal(page.elements.useValueMatches.disabled, true);
});

test("missing and ambiguous values remain unresolved while incoming values count", async () => {
  const page = fixture({ targets: 1, incoming: 3 });
  page.payload.relationship_resolutions[2].status = "missing";
  page.payload.relationship_resolutions[3].status = "ambiguous";
  await page.open();
  assert.equal(page.elements.valueMatchStatus.textContent, "2 of 4 matched · 1 in Odoo · 1 from Payment terms");
  assert.equal(page.elements.useValueMatches.textContent, "Use 2 partial match(es)");
  const cells = page.elements.valueMatchRows.querySelectorAll("[data-value-match-origin]");
  assert.equal(cells[2].textContent, "Not found in Odoo or the incoming table");
  assert.equal(cells[3].textContent, "Matches more than one record");
});

test("refresh failure and retry preserve an unsaved Odoo override of an incoming match", async () => {
  const page = fixture({ targets: 1, incoming: 1 }); await page.open();
  const incoming = page.selects()[1]; incoming.fire("focus"); incoming.value = "TERM0"; incoming.fire("change");
  page.fail(true); await page.refresh();
  assert.equal(page.selects()[1].value, "TERM0");
  page.fail(false); await page.retry();
  assert.equal(page.selects()[1].value, "TERM0");
  assert.match(page.elements.valueMatchStatus.textContent, /2 of 2 matched · 2 in Odoo · 0 from Payment terms/);
});
