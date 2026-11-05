// Run with node --test tests/e2e/mapping-journey.test.cjs.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = (name) => readFileSync(path.join(__dirname,
  `../../src/impodo/web/static/${name}.js`), "utf8");

class Element {
  constructor(dataset = {}, tagName = "DIV") {
    this.dataset = dataset;
    this.tagName = tagName;
    this.attributes = new Map();
    this.hidden = false;
    this.disabled = false;
    this.children = [];
    this.listeners = new Map();
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
  setAttribute(name, value) { this.attributes.set(name, value); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  hasAttribute(name) {
    const key = name.replace(/^data-/, "").replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
    return this.attributes.has(name) || Object.hasOwn(this.dataset, key) ||
      (["href", "id"].includes(name) && Boolean(this[name]));
  }
  matches(selector) {
    return selector.split(",").some((part) => {
      const name = part.trim();
      if (name.includes(" ")) return false;
      if (name.startsWith(".")) return this.className === name.split("[")[0].slice(1) &&
        (!name.includes("[") || this.hasAttribute(name.slice(name.indexOf("[") + 1, -1)));
      if (/^[a-z]+\[/.test(name)) return this.tagName === name.split("[")[0].toUpperCase() &&
        this.hasAttribute(name.slice(name.indexOf("[") + 1, -1));
      return name.startsWith("[") ? this.hasAttribute(name.slice(1, -1)) : this.tagName === name.toUpperCase();
    });
  }
  closest(selector) {
    if (this.matches(selector)) return this;
    return this.parentElement?.closest(selector) || null;
  }
  querySelectorAll(selector) {
    return this.children.flatMap((child) => [
      ...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector),
    ]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  append(child) { this.children.push(child); child.parentElement = this; }
  remove() { this.parentElement.children = this.parentElement.children.filter((child) => child !== this); }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  focus() { this.focused = true; }
  scrollIntoView() { this.scrolled = true; }
}

function page({ url = "https://impodo.test/workspaces/one/mapping", company = false,
  availableTasks = ["rows", "identity", "fields", "links"], inactiveTable = false,
  tableIds = ["active"], activeId = tableIds[0], storage = new Map(), continuationUrl = "" } = {}) {
  const root = new Element({ mappingJourney: "", step: "tables", task: "rows",
    companyRequired: String(company), activeDatasetIndex: String(tableIds.indexOf(activeId)) });
  const heading = new Element({ mappingJourneyHeading: "", mappingStepTitle: "" }, "H2");
  root.append(heading);
  const panels = {};
  for (const step of ["company", "tables", "check", "confirm"]) {
    panels[step] = new Element({ mappingStepPanel: step });
    root.append(panels[step]);
    root.append(new Element({ mappingGoStep: step }, "A"));
  }
  const form = new Element({ mappingForm: "", mutationReceiptUrl: "/receipts/",
    mutationReceiptWaitMs: "0" });
  form.setAttribute("action", "/workspaces/one/mapping/save?mapping_dataset=0");
  root.append(form);
  const continueButton = new Element({ mappingContinue: "", mappingContinuationUrl: continuationUrl }, "BUTTON");
  form.append(continueButton);
  let inactiveHeading;
  if (inactiveTable) {
    const inactive = new Element({ mappingDataset: "inactive" });
    inactive.hidden = true;
    const panel = new Element({ mappingTaskPanel: "fields" });
    inactiveHeading = new Element({ mappingTaskHeading: "" }, "H3");
    panel.append(inactiveHeading);
    inactive.append(panel);
    panels.tables.append(inactive);
  }
  let active;
  for (const [index, id] of tableIds.entries()) {
    const table = new Element({ mappingDataset: id, ...(id === activeId ? { mappingActiveTable: "" } : {}) });
    table.id = `mapping-dataset-${index}`;
    if (id === activeId) active = table;
    else {
      table.hidden = true;
      const link = new Element({ mappingTableLink: "" }, "A");
      link.href = `https://impodo.test/workspaces/one/mapping?mapping_dataset=${index}`;
      table.append(link);
    }
    panels.tables.append(table);
  }
  const tasks = {};
  for (const task of availableTasks) {
    tasks[task] = new Element({ mappingTaskPanel: task });
    tasks[task].append(new Element({ mappingTaskHeading: "" }, "H3"));
    active.append(tasks[task]);
    active.append(new Element({ mappingGoTask: task }, "BUTTON"));
  }
  const location = { assign(value) { navigations.push(String(value)); } };
  const setUrl = (value) => {
    const current = new URL(value, location.href || url);
    Object.assign(location, { href: current.href, origin: current.origin,
      pathname: current.pathname, search: current.search, hash: current.hash });
  };
  setUrl(url);
  const navigations = [];
  const listeners = new Map();
  const window = { location, requestAnimationFrame: (callback) => callback(),
    queueMicrotask,
    sessionStorage: { getItem: (key) => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, value) },
    addEventListener: (name, listener) => listeners.set(name, listener), setTimeout, clearTimeout,
    history: { state: null, pushState(_state, _title, value) { setUrl(value); },
      replaceState(_state, _title, value) { setUrl(value); } } };
  const documentListeners = new Map();
  const document = { querySelector: () => null, querySelectorAll: (selector) => root.querySelectorAll(selector),
    addEventListener: (name, listener) => documentListeners.set(name, listener),
    getElementById: (id) => root.querySelectorAll("[id]").find((element) => element.id === id),
    createElement: (tag) => new Element({}, tag.toUpperCase()), dispatchEvent: () => true };
  const context = { window, document, URL, URLSearchParams, AbortController,
    fetch: async () => { throw new Error("offline"); } };
  vm.runInNewContext(source("mapping-journey"), context);
  const journey = window.impodoMappingJourney.create(root);
  Object.assign(window.impodoMappingJourney, journey);
  const click = (target, handled = false) => {
    let prevented = false;
    documentListeners.get("click")({ target, button: 0, defaultPrevented: handled,
      preventDefault() { prevented = true; } });
    return prevented;
  };
  const recovery = ({ newerEdits = false } = {}) => {
    vm.runInNewContext(source("mapping-save-recovery"), context);
    let dirty = true;
    const recover = window.impodoMappingSaveRecovery.create({ mappingForm: form,
      setDirty(value) { dirty = value; }, updateMappingVersionFields() {},
      navigateToMappingResult(value) { navigations.push(value); },
      hasNewerEdits: () => newerEdits,
      onCommitted(operation, payload) {
        assert.equal(dirty, newerEdits, "advance only after the editor accepts the committed save");
        return journey.committed(operation, payload);
      } });
    return { recover, dirty: () => dirty };
  };
  return { root, panels, tasks, heading, form, window, journey, click, navigations, recovery, inactiveHeading, continueButton,
    submit(target) {
      let prevented = false;
      documentListeners.get("submit")({ target, preventDefault() { prevented = true; } });
      return prevented;
    },
    setUrl, popstate: () => listeners.get("popstate")() };
}

test("changing tasks keeps all field controls enabled and retains their edits", () => {
  const view = page();
  const input = new Element({}, "INPUT");
  input.value = "Unsaved category";
  view.tasks.fields.append(input);
  const fields = view.root.querySelectorAll("[data-mapping-go-task]").find((item) => item.dataset.mappingGoTask === "fields");
  assert.equal(view.click(fields), true);
  assert.equal(view.tasks.rows.hidden, true);
  assert.equal(view.tasks.fields.hidden, false);
  assert.equal(input.disabled, false);
  assert.equal(input.value, "Unsaved category");
  assert.equal(fields.getAttribute("aria-current"), "step");
  assert.match(view.form.getAttribute("action"), /step=tables&task=fields/);
  assert.equal(view.tasks.fields.children[0].focused, true);
  assert.equal(view.root.querySelector("[data-mapping-active-table]").scrolled, true);
  assert.equal(view.tasks.fields.children[0].scrolled, undefined,
    "keep the table and task navigation in view while focusing the task");
});

test("an inline recovery handler keeps control of its correction link and focus", () => {
  const view = page();
  const fields = view.root.querySelectorAll("[data-mapping-go-task]")
    .find((item) => item.dataset.mappingGoTask === "fields");
  view.click(fields, true);
  assert.equal(view.root.dataset.task, "rows");
  assert.equal(view.tasks.fields.children[0].focused, undefined);
});

test("missing company wins over a remembered task but an explicit field link wins", () => {
  const first = page({ company: true, url: "https://impodo.test/workspaces/one/mapping?task=fields" });
  assert.equal(first.root.dataset.step, "company");
  const linked = page({ company: true, url: "https://impodo.test/workspaces/one/mapping#scalar-0-name" });
  assert.equal(linked.root.dataset.step, "tables");
  assert.equal(linked.root.dataset.task, "fields");
  const explicit = page({ company: true, url: "https://impodo.test/workspaces/one/mapping?step=check" });
  assert.equal(explicit.root.dataset.step, "check", "an explicit valid step remains stable across reloads");
});

test("revealing a deep field opens its task and nested disclosures", () => {
  const view = page();
  const details = new Element({}, "DETAILS");
  const field = new Element();
  field.id = "scalar-0-name";
  details.append(field);
  view.tasks.fields.append(details);
  view.journey.reveal(field);
  assert.equal(view.tasks.fields.hidden, false);
  assert.equal(details.open, true);
  assert.equal(view.root.dataset.task, "fields");
});

test("Save and continue advances exactly once after a committed save", () => {
  const view = page();
  const button = new Element({ mappingContinue: "" }, "BUTTON");
  const { recover, dirty } = view.recovery();
  const operation = recover.createOperation({ action: "save_progress", entries: [],
    journeyContinuation: view.journey.continuationFor(button) });
  assert.equal(view.root.dataset.task, "rows");
  recover.applyMutationOutcome({ status: "committed" }, operation);
  assert.equal(dirty(), false);
  assert.equal(view.root.dataset.task, "identity");
  view.journey.show({ task: "fields" });
  recover.applyMutationOutcome({ status: "committed" }, operation);
  assert.equal(view.root.dataset.task, "fields", "a repeated receipt must not repeat navigation");
});

test("ordinary save progress never advances the journey", () => {
  const view = page();
  const { recover } = view.recovery();
  recover.applyMutationOutcome({ status: "committed" }, { action: "save_progress" });
  assert.equal(view.root.dataset.task, "rows");
});

for (const outcome of [
  { status: "not_found" },
  { status: "rejected", failure_code: "MAPPING_VERSION_CONFLICT" },
  { status: "rejected", partial_save: true },
  { status: "rejected", failure_code: "VALIDATION_FAILED" },
]) {
  test(`${outcome.failure_code || outcome.status} keeps edits and cancels continuation`, () => {
    const view = page();
    const { recover, dirty } = view.recovery();
    const operation = { action: "save_progress", journeyContinuation: { step: "tables", task: "identity" } };
    recover.applyMutationOutcome(outcome, operation);
    assert.equal(view.root.dataset.task, "rows");
    assert.equal(dirty(), true);
    assert.equal(operation.journeyContinuation, null);
    assert.deepEqual(view.navigations, []);
  });
}

test("unknown save outcome cancels continuation even if a later receipt commits", async () => {
  const view = page();
  const { recover } = view.recovery();
  const operation = { action: "save_progress", journeyContinuation: { step: "tables", task: "identity" } };
  assert.equal(await recover.resolveMutationOutcome(operation), "pending");
  recover.applyMutationOutcome({ status: "committed" }, operation);
  assert.equal(view.root.dataset.task, "rows");
});

test("switching tables waits for save and retains only the destination table filters", () => {
  const view = page();
  const { recover } = view.recovery();
  const link = new Element({ mappingTableLink: "" }, "A");
  link.href = "https://impodo.test/workspaces/one/mapping?mapping_dataset=2&relation_target=old#mapping-dataset-2";
  view.root.append(link);
  let operation;
  view.form.requestSubmit = (button) => {
    operation = { action: button.value, journeyContinuation: view.journey.continuationFor(button) };
  };
  view.click(link);
  assert.deepEqual(view.navigations, []);
  assert.equal(operation.action, "save_progress");
  recover.applyMutationOutcome({ status: "committed" }, operation);
  const destination = new URL(view.navigations[0]);
  assert.equal(destination.searchParams.get("mapping_dataset"), "2");
  assert.equal(destination.searchParams.get("task"), "rows");
  assert.equal(destination.searchParams.has("relation_target"), false);
});

test("check and row confirmation route separately while final preparation navigation survives", () => {
  const view = page();
  assert.match(view.journey.resultUrl("/workspaces/one/mapping", { action: "draft" }), /step=check.*#semantic-validation$/);
  assert.match(view.journey.resultUrl("/workspaces/one/mapping?step=check#next-step-blockers", { action: "draft" }), /step=check.*#next-step-blockers$/);
  assert.match(view.journey.resultUrl("/workspaces/one/mapping?step=confirm", { action: "draft" }), /step=confirm/);
  assert.match(view.journey.resultUrl("/workspaces/one/mapping", { action: "confirm_rows" }), /step=confirm/);
  assert.equal(view.journey.resultUrl("/workspaces/one/prepare", { action: "submit" }), "https://impodo.test/workspaces/one/prepare");
});

test("browser Back restores the panel and task from the URL", () => {
  const view = page();
  view.journey.show({ step: "confirm" });
  view.setUrl("https://impodo.test/workspaces/one/mapping?step=tables&task=links");
  view.popstate();
  assert.equal(view.panels.tables.hidden, false);
  assert.equal(view.panels.confirm.hidden, true);
  assert.equal(view.tasks.links.hidden, false);
  assert.equal(view.tasks.links.children[0].focused, true);
});

test("pinned tables skip absent identity and linked-record tasks", () => {
  const view = page({ availableTasks: ["rows", "fields"], tableIds: ["active", "other"],
    url: "https://impodo.test/workspaces/one/mapping?step=tables&task=identity" });
  assert.equal(view.root.dataset.task, "rows");
  const button = new Element({ mappingContinue: "", mappingContinuationUrl: "/workspaces/one/mapping?mapping_dataset=1" });
  assert.equal(view.journey.continuationFor(button).task, "fields", "next-table URL must not skip remaining tasks");
  view.journey.show({ task: "fields" });
  assert.match(view.journey.continuationFor(button).url, /mapping_dataset=1/);
});

test("the final task uses the actual next table and never a recommendation that skips work", () => {
  const view = page();
  view.journey.show({ task: "links" });
  const button = new Element({ mappingContinue: "", mappingContinuationUrl: "" });
  const continuation = view.journey.continuationFor(button);
  view.journey.committed({ action: "save_progress", journeyContinuation: continuation },
    { next_recommended_url: "/workspaces/one/mapping?mapping_dataset=0" });
  assert.equal(view.root.dataset.step, "check");
  assert.deepEqual(view.navigations, []);
});

test("native validation reveals a hidden task before focusing its invalid control", () => {
  const view = page();
  const invalid = new Element({}, "INPUT");
  view.tasks.links.append(invalid);
  view.form.listeners.get("invalid")({ target: invalid });
  assert.equal(view.root.dataset.task, "links");
  assert.equal(view.tasks.links.hidden, false);
  assert.equal(invalid.focused, true);
  assert.equal(invalid.disabled, false);
});

test("one validation turn keeps the first invalid task visible", async () => {
  const view = page();
  const first = new Element({}, "INPUT");
  const second = new Element({}, "INPUT");
  view.tasks.identity.append(first);
  view.tasks.links.append(second);
  const invalid = view.form.listeners.get("invalid");
  invalid({ target: first });
  invalid({ target: second });
  assert.equal(view.root.dataset.task, "identity");
  assert.equal(view.tasks.identity.hidden, false);
  assert.equal(first.focused, true);
  assert.equal(second.focused, undefined);
  await new Promise(queueMicrotask);
  invalid({ target: second });
  assert.equal(view.root.dataset.task, "links", "a later validation turn can reveal its first issue");
});

test("new edits made during a save survive its committed receipt", () => {
  const view = page();
  const input = new Element({}, "INPUT");
  input.value = "A later unsaved edit";
  view.tasks.rows.append(input);
  const { recover, dirty } = view.recovery({ newerEdits: true });
  const operation = { action: "save_progress", journeyContinuation: { step: "tables", task: "identity" } };
  recover.applyMutationOutcome({ status: "committed" }, operation);
  assert.equal(dirty(), true);
  assert.equal(input.value, "A later unsaved edit");
  assert.equal(view.root.dataset.task, "rows");
  assert.equal(operation.journeyContinuation, null);
  assert.deepEqual(view.navigations, []);
});

test("sidebar navigation changes the visible step without submitting or leaving the editor", () => {
  const view = page();
  const link = new Element({}, "A");
  link.href = "https://impodo.test/workspaces/one/mapping?step=confirm";
  link.className = "sidebar-subnav-link";
  view.root.append(link);
  view.click(link);
  assert.equal(view.root.dataset.step, "confirm");
  assert.equal(link.getAttribute("aria-current"), "page");
  assert.deepEqual(view.navigations, []);
});

test("step navigation waits while a save is in flight", () => {
  const view = page();
  const check = view.root.querySelectorAll("[data-mapping-go-step]").find((item) => item.dataset.mappingGoStep === "check");
  view.form.setAttribute("aria-busy", "true");
  assert.equal(view.click(check), true);
  assert.equal(view.root.dataset.step, "tables");
});

test("browser Back during a pending save cancels its automatic continuation", () => {
  const view = page();
  const { recover } = view.recovery();
  const operation = { action: "save_progress", journeyContinuation: { step: "tables", task: "identity" } };
  view.window.impodoMappingEditor = {
    cancelPendingContinuation: () => view.journey.cancelContinuation(operation),
  };
  view.form.setAttribute("aria-busy", "true");
  view.setUrl("https://impodo.test/workspaces/one/mapping?step=tables&task=fields");
  view.popstate();
  recover.applyMutationOutcome({ status: "committed" }, operation);
  assert.equal(view.root.dataset.task, "fields");
  assert.equal(operation.journeyContinuation, null);
});

test("task focus belongs to the active table when another table also has field panels", () => {
  const view = page({ inactiveTable: true });
  view.journey.show({ task: "fields" }, { focus: true });
  assert.equal(view.tasks.fields.children[0].focused, true);
  assert.equal(view.inactiveHeading.focused, undefined);
});

test("ordinary sibling posts keep unsaved matching edits on the current page", () => {
  const view = page();
  view.window.impodoMappingEditor = { getState: () => ({ dirty: true, busy: false }) };
  for (const suffix of ["company", "company/choices", "company/refresh", "order/check", "order", "quality", "review-workbook", "expectations"]) {
    const sibling = new Element({}, "FORM");
    sibling.setAttribute("action", `/workspaces/one/mapping/${suffix}`);
    assert.equal(view.submit(sibling), true, suffix);
  }
  const recovery = new Element({}, "FORM");
  recovery.setAttribute("action", "/workspaces/one/schema/supporting/save");
  assert.equal(view.submit(recovery), false, "inline recovery keeps its asynchronous behavior");
  view.window.impodoMappingEditor = { getState: () => ({ dirty: false, busy: false }) };
  const clean = new Element({}, "FORM");
  clean.setAttribute("action", "/workspaces/one/mapping/order/check");
  assert.equal(view.submit(clean), false, "a saved early check remains available");
});

const reviewedKey = "impodo.mapping.reviewedTables:/workspaces/one/mapping";

test("final-task continuation wraps to an unreviewed table when the active table moved last", () => {
  const storage = new Map();
  const view = page({ tableIds: ["earlier", "active"], activeId: "active", storage });
  view.journey.show({ task: "links" });
  assert.equal(view.continueButton.textContent, "Save and match next table");
  const operation = { action: "save_progress", journeyContinuation: view.journey.continuationFor(view.continueButton) };
  assert.equal(storage.has(reviewedKey), false, "viewing the last task does not count as reviewing it");
  view.recovery().recover.applyMutationOutcome({ status: "committed" }, operation);
  assert.equal(new URL(view.navigations[0]).searchParams.get("mapping_dataset"), "0");
  assert.deepEqual(JSON.parse(storage.get(reviewedKey)).reviewed, ["active"]);
});

test("successful final-task saves survive reload and reordered tables without revisiting reviewed work", () => {
  const storage = new Map();
  const first = page({ tableIds: ["products", "suppliers"], activeId: "products", storage,
    continuationUrl: "/workspaces/one/mapping?mapping_dataset=1" });
  first.journey.show({ task: "links" });
  first.recovery().recover.applyMutationOutcome({ status: "committed" }, {
    action: "save_progress", journeyContinuation: first.journey.continuationFor(first.continueButton),
  });
  assert.equal(first.navigations.length, 1);
  assert.equal(new URL(first.navigations[0]).searchParams.has("mapping_reviewed"), false);
  const second = page({ tableIds: ["suppliers", "products"], activeId: "suppliers", storage,
    continuationUrl: "/workspaces/one/mapping?mapping_dataset=1" });
  second.journey.show({ task: "links" });
  assert.equal(second.continueButton.textContent, "Save and review matches");
  second.recovery().recover.applyMutationOutcome({ status: "committed" }, {
    action: "save_progress", journeyContinuation: second.journey.continuationFor(second.continueButton),
  });
  assert.equal(second.root.dataset.step, "check");
  assert.deepEqual(second.navigations, []);
  assert.deepEqual(JSON.parse(storage.get(reviewedKey)).reviewed.sort(), ["products", "suppliers"]);
  const reloaded = page({ tableIds: ["products", "suppliers"], activeId: "products", storage });
  reloaded.journey.show({ task: "links" });
  assert.equal(reloaded.journey.continuationFor(reloaded.continueButton).step, "check");
});

test("a changed dataset-ID set resets only the optional traversal hints", () => {
  const storage = new Map([[reviewedKey, JSON.stringify({
    tableIds: JSON.stringify(["products", "suppliers"]), reviewed: ["products", "suppliers"],
  })]]);
  const view = page({ tableIds: ["products", "suppliers", "new"], activeId: "new", storage });
  view.journey.show({ task: "links" });
  assert.match(view.journey.continuationFor(view.continueButton).url, /mapping_dataset=0/);
  assert.equal(view.root.dataset.step, "tables");
});

for (const outcome of [
  { status: "rejected", failure_code: "VALIDATION_FAILED" },
  { status: "rejected", failure_code: "MAPPING_VERSION_CONFLICT" },
  { status: "pending" },
]) {
  test(`${outcome.failure_code || outcome.status} never marks a final task reviewed`, () => {
    const storage = new Map();
    const view = page({ tableIds: ["products", "suppliers"], storage });
    view.journey.show({ task: "links" });
    const operation = { action: "save_progress", journeyContinuation: view.journey.continuationFor(view.continueButton) };
    view.recovery().recover.applyMutationOutcome(outcome, operation);
    assert.equal(storage.has(reviewedKey), false);
    assert.equal(view.root.dataset.task, "links");
    assert.deepEqual(view.navigations, []);
  });
}

test("ordinary saves, manual table jumps and newer edits never mark the final task reviewed", () => {
  for (const kind of ["ordinary", "table-jump", "newer-edits", "canceled"]) {
    const storage = new Map();
    const view = page({ tableIds: ["products", "suppliers"], storage });
    view.journey.show({ task: "links" });
    const operation = { action: "save_progress" };
    if (kind === "table-jump") operation.journeyContinuation = { url: "/workspaces/one/mapping?mapping_dataset=1" };
    else if (kind !== "ordinary") operation.journeyContinuation = view.journey.continuationFor(view.continueButton);
    if (kind === "canceled") view.journey.cancelContinuation(operation);
    view.recovery({ newerEdits: kind === "newer-edits" }).recover.applyMutationOutcome({ status: "committed" }, operation);
    assert.equal(storage.has(reviewedKey), false, kind);
  }
});

test("denied browser storage leaves final-task traversal usable without claiming saved readiness", () => {
  const storage = { get() { throw new Error("storage denied"); }, set() { throw new Error("storage denied"); } };
  const view = page({ tableIds: ["earlier", "active"], activeId: "active", storage });
  view.journey.show({ task: "links" });
  view.recovery().recover.applyMutationOutcome({ status: "committed" }, {
    action: "save_progress", journeyContinuation: view.journey.continuationFor(view.continueButton),
  });
  assert.equal(new URL(view.navigations[0]).searchParams.get("mapping_dataset"), "0");
  assert.equal(view.root.dataset.step, "tables");
});

test("a full two-table journey terminates after reload when browser storage is denied", () => {
  const storage = { get() { throw new Error("denied"); }, set() { throw new Error("denied"); } };
  const first = page({ tableIds: ["products", "suppliers"], activeId: "products", storage });
  first.journey.show({ task: "links" });
  first.recovery().recover.applyMutationOutcome({ status: "committed" }, {
    action: "save_progress", journeyContinuation: first.journey.continuationFor(first.continueButton),
  });
  const second = page({ url: first.navigations[0], tableIds: ["suppliers", "products"], activeId: "suppliers", storage });
  second.journey.show({ task: "links" });
  assert.equal(second.continueButton.textContent, "Save and review matches");
  second.recovery().recover.applyMutationOutcome({ status: "committed" }, {
    action: "save_progress", journeyContinuation: second.journey.continuationFor(second.continueButton),
  });
  assert.equal(second.root.dataset.step, "check");
  assert.deepEqual(second.navigations, []);
  const reloaded = page({ url: second.window.location.href, tableIds: ["products", "suppliers"], activeId: "suppliers", storage });
  reloaded.journey.show({ step: "tables", task: "links" });
  assert.equal(reloaded.journey.continuationFor(reloaded.continueButton).step, "check");
});

test("a handled recovery link during save cancels late automatic task navigation", () => {
  const view = page();
  const operation = { action: "save_progress", journeyContinuation: { step: "tables", task: "identity" } };
  view.window.impodoMappingEditor = {
    cancelPendingContinuation: () => view.journey.cancelContinuation(operation),
  };
  view.form.setAttribute("aria-busy", "true");
  const link = new Element({ reviewRelatedField: "" }, "A");
  link.href = "https://impodo.test/workspaces/one/mapping#relationship-0-category_id";
  view.journey.show({ task: "links" }); // The recovery owner already revealed this field.
  view.click(link, true);
  view.recovery().recover.applyMutationOutcome({ status: "committed" }, operation);
  assert.equal(view.root.dataset.task, "links");
  assert.equal(operation.journeyContinuation, null);
  assert.deepEqual(view.navigations, []);
});

test("a full storage quota does not let older stored hints erase progress carried by the URL", () => {
  const prior = JSON.stringify({ tableIds: JSON.stringify(["products", "suppliers"]), reviewed: [] });
  const storage = { get() { return prior; }, set() { throw new Error("quota exceeded"); } };
  const first = page({ tableIds: ["products", "suppliers"], activeId: "products", storage });
  first.journey.show({ task: "links" });
  first.recovery().recover.applyMutationOutcome({ status: "committed" }, {
    action: "save_progress", journeyContinuation: first.journey.continuationFor(first.continueButton),
  });
  const next = page({ url: first.navigations[0], tableIds: ["products", "suppliers"], activeId: "suppliers", storage });
  next.journey.show({ task: "links" });
  assert.equal(next.journey.continuationFor(next.continueButton).step, "check");
});
