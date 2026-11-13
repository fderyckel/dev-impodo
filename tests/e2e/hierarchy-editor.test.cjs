// Run with node --test tests/e2e/hierarchy-editor.test.cjs.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const script = readFileSync(path.join(__dirname,
  "../../src/impodo/web/static/hierarchy-editor.js"), "utf8");
const settle = () => new Promise(setImmediate);

function fixture(responses = []) {
  const requests = [];
  const listeners = new Map();
  const summary = { textContent: "" };
  const fields = {};
  let panel;
  let focused = null;
  function control(name, value, text = value) {
    const item = {
      name, value, textContent: text, disabled: false,
      selectedOptions: [{ textContent: text }], options: [{}, {}],
      focus() { focused = name; }, scrollIntoView() {},
      replaceWith(other) { item.owner.fields[name] = other; other.owner = item.owner; },
    };
    return item;
  }
  for (const [name, value] of Object.entries({
    target_model: "x.place", target_name_field: "name", hierarchy_parent_field: "",
    hierarchy_start: "existing", existing_source_levels: "0",
    hierarchy_level_1: "aisle", hierarchy_level_2: "bin", output_dataset_name: "places",
  })) fields[name] = control(name, value);
  const form = {
    dataset: { sourceSourceId: "places-source" },
    elements: { namedItem: (name) => fields[name] || panel.fields[name] },
    addEventListener(type, callback) { listeners.set(type, callback); },
    querySelector(selector) {
      if (selector === "[data-hierarchy-picker]") return panel;
      if (selector === "[data-hierarchy-feedback]") return panel.feedback;
      if (selector === "[data-hierarchy-path]") return summary;
      if (selector.includes("data-hierarchy-load")) return panel.buttons[1];
      return null;
    },
    dispatchEvent(event) { event.target ||= form; listeners.get(event.type)?.(event); },
  };
  function picker(label = "Old parent", error = "", companyOptions = 2) {
    const result = {
      hidden: false, fields: {}, busy: false,
      feedback: {
        textContent: error,
        replaceChildren(child) { this.textContent = child.textContent; },
        focus() { focused = "feedback"; },
        replaceWith(other) { panel.feedback = other; },
      },
      setAttribute() { result.busy = true; },
      removeAttribute() { result.busy = false; },
      querySelector(selector) {
        if (selector === "[data-hierarchy-feedback]") return result.feedback;
        const name = selector.match(/name="([^"]+)"/)?.[1];
        return name ? result.fields[name] : null;
      },
      querySelectorAll: () => result.buttons,
      replaceWith(other) { panel = other; },
    };
    for (const [name, value] of Object.entries({
      hierarchy_company_id: "7", existing_parent_search: "Stock", existing_parent_choice: label,
    })) {
      result.fields[name] = control(name, value);
      result.fields[name].owner = result;
    }
    result.fields.hierarchy_company_id.options = Array.from({ length: companyOptions }, () => ({}));
    result.buttons = ["companies", "parents"].map((value) => ({
      name: "hierarchy_lookup_action", value, disabled: false,
      formAction: "https://impodo.test/workspaces/one/derived-entities/hierarchy/parents",
      hasAttribute: (name) => name === "data-hierarchy-load",
    }));
    return result;
  }
  panel = picker();
  class FormData {
    constructor() {
      this.values = new Map(Object.entries({ ...fields, ...panel.fields }).map(
        ([name, field]) => [name, field.value]
      ));
    }
    set(name, value) { this.values.set(name, value); }
    get(name) { return this.values.get(name); }
  }
  class Event {
    constructor(type) { this.type = type; }
  }
  class DOMParser {
    parseFromString(data) {
      return { querySelectorAll: () => [{ dataset: form.dataset,
        querySelector: () => data }] };
    }
  }
  const context = {
    document: {
      addEventListener(_type, callback) { callback(); },
      querySelectorAll: () => [form], dispatchEvent() {},
      createElement: () => ({ setAttribute() {}, textContent: "" }),
    },
    window: {}, FormData, Event, CustomEvent: Event, DOMParser,
    AbortController, setTimeout, clearTimeout,
    fetch: async (url, options) => {
      requests.push({ url, options });
      const response = responses.shift();
      if (response instanceof Error) throw response;
      return typeof response === "function" ? response() : {
        ok: response.ok ?? true, status: response.status ?? 200,
        redirected: response.redirected ?? false,
        text: async () => response.missing ? null : picker(
          response.parent || "Physical / Stock", response.error, response.companyOptions
        ),
      };
    },
  };
  vm.runInNewContext(script, context);
  return {
    form, fields, summary, requests, picker: () => panel, focused: () => focused,
    submit(action = "parents") {
      const event = { target: form, submitter: panel.buttons[action === "companies" ? 0 : 1],
        prevented: false, preventDefault() { this.prevented = true; } };
      listeners.get("submit")(event);
      return event;
    },
    change(name, value) {
      const input = form.elements.namedItem(name);
      input.value = value;
      listeners.get("change")({ target: input });
    },
    search() {
      const event = { key: "Enter", target: panel.fields.existing_parent_search,
        preventDefault() { this.prevented = true; } };
      listeners.get("keydown")(event);
      return event;
    },
    response(label) { return { ok: true, status: 200, text: async () => picker(label) }; },
  };
}

test("parent loading updates the picker, preserving every other entered field", async () => {
  const current = fixture([{}]);
  const old = current.picker();
  const event = current.submit();
  assert.equal(event.prevented, true);
  assert.equal(old.busy, true);
  await settle();
  assert.notEqual(current.picker(), old);
  assert.equal(current.fields.hierarchy_level_1.value, "aisle");
  assert.equal(current.fields.output_dataset_name.value, "places");
  assert.match(current.summary.textContent, /Physical \/ Stock → aisle → bin/);
  assert.equal(current.focused(), "existing_parent_choice");
  assert.match(current.requests[0].url, /hierarchy\/parents$/);
  assert.equal(current.requests[0].options.body.get("hierarchy_lookup_action"), "parents");
});

test("a rejected lookup keeps the previous parent list and shows the actual error", async () => {
  const current = fixture([{ ok: false, status: 422, error: "Choose the parent relationship" }]);
  const old = current.picker();
  current.submit();
  await settle();
  assert.equal(current.picker(), old);
  assert.equal(old.fields.existing_parent_choice.value, "Old parent");
  assert.equal(old.feedback.textContent, "Choose the parent relationship");
  assert.equal(current.focused(), "feedback");
  assert.equal(old.busy, false);
});

test("network failure, login redirects, and invalid responses keep entries and allow retry", async () => {
  for (const response of [new Error("offline"), { redirected: true }, { status: 503 }, { missing: true }]) {
    const current = fixture([response, {}]);
    const old = current.picker();
    current.submit();
    await settle();
    assert.equal(current.picker(), old);
    assert.match(old.feedback.textContent, /Your entries are still here/);
    assert.equal(old.buttons[1].disabled, false);
    current.submit();
    await settle();
    assert.notEqual(current.picker(), old);
  }
});

test("company refresh preserves the loaded parent list", async () => {
  const current = fixture([{}]);
  const selected = current.picker().fields.existing_parent_choice;
  current.submit("companies");
  await settle();
  assert.equal(current.picker().fields.existing_parent_choice, selected);
  assert.equal(current.focused(), "hierarchy_company_id");
});

test("changing company clears the old parent and loads the new company scope", async () => {
  const current = fixture([{}]);
  const selected = current.picker().fields.existing_parent_choice;
  current.change("hierarchy_company_id", "8");
  assert.equal(selected.value, "");
  await settle();
  assert.equal(current.requests[0].options.body.get("hierarchy_company_id"), "8");
});

test("a late result cannot overwrite a changed target or submit another action", async () => {
  let resolve;
  const current = fixture([() => new Promise((done) => { resolve = done; })]);
  const old = current.picker();
  current.submit();
  assert.equal(current.submit().prevented, true);
  assert.equal(current.requests.length, 1);
  current.fields.target_model.value = "x.other";
  resolve(current.response("Wrong target"));
  await settle();
  assert.equal(current.picker(), old);
  assert.match(old.feedback.textContent, /record type changed/);
});

test("top-level mode hides parent selection and shows only the file hierarchy", () => {
  const current = fixture();
  current.change("hierarchy_start", "source");
  assert.equal(current.picker().hidden, true);
  assert.equal(current.picker().fields.existing_parent_choice.required, false);
  assert.equal(current.fields.hierarchy_level_2.required, true);
  assert.equal(current.summary.textContent, "Your hierarchy: aisle → bin");
});

test("Enter in parent search loads parents instead of submitting the hierarchy", async () => {
  const current = fixture([{}]);
  assert.equal(current.search().prevented, true);
  await settle();
  assert.equal(current.requests[0].options.body.get("hierarchy_lookup_action"), "parents");
});
