// Browser-state evidence for saved-data bulk policies and profile navigation.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = readFileSync(path.join(__dirname,
  "../../src/impodo/web/static/saved-odoo-data.js"), "utf8");

function control(value = "") {
  const listeners = new Map();
  return {
    value,
    listeners,
    changes: 0,
    addEventListener(name, callback) { listeners.set(name, callback); },
    dispatchEvent(event) {
      if (event.type === "change") this.changes += 1;
      listeners.get(event.type)?.(event);
    },
  };
}

function page({ formPresent = true, fieldCount = 3,
  profileUrl = "/projects/one/saved-odoo-data?selected=saved-one",
  profilePresent = true, versionValue = null } = {}) {
  const fields = Array.from({ length: fieldCount }, () => control("use_saved"));
  const policy = control("use_saved");
  const profile = control("");
  const version = control(versionValue || "");
  const status = { textContent: "Field choices apply to all matching records." };
  const navigations = [];
  const buttons = ["use_saved", "keep_destination", "fill_empty"].map((value) => ({
    ...control(), dataset: { savedOdooPreset: value },
  }));
  const form = {
    dataset: { profileSelectionUrl: profileUrl },
    querySelector(selector) {
      if (selector === "[data-saved-odoo-default-policy]") return policy;
      if (selector === "[data-saved-odoo-profile]") return profilePresent ? profile : null;
      if (selector === "[data-saved-odoo-version]") return versionValue !== null ? version : null;
      if (selector === "[data-saved-odoo-policy-status]") return status;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === "[data-saved-odoo-field-policy]") return fields;
      if (selector === "[data-saved-odoo-preset]") return buttons;
      return [];
    },
  };
  class BrowserEvent {
    constructor(type, options) { this.type = type; this.bubbles = options?.bubbles; }
  }
  const context = {
    document: { querySelector: () => formPresent ? form : null },
    window: { location: { origin: "https://impodo.test", assign: (url) => navigations.push(url) } },
    Event: BrowserEvent,
    URL,
  };
  vm.runInNewContext(script, context);
  return {
    fields, policy, profile, status, buttons, navigations,
    clickPreset(value) {
      const button = buttons.find((item) => item.dataset.savedOdooPreset === value);
      button.listeners.get("click")();
    },
    changePolicy(value) {
      policy.value = value;
      policy.listeners.get("change")();
    },
    chooseProfile(value) {
      profile.value = value;
      profile.listeners.get("change")?.();
    },
    chooseVersion(value) {
      version.value = value;
      version.listeners.get("change")?.();
    },
  };
}

for (const value of ["use_saved", "keep_destination", "fill_empty"]) {
  test(`the ${value} preset applies to every matching-field control`, () => {
    const view = page();
    view.clickPreset(value);
    assert.equal(view.policy.value, value);
    assert.deepEqual(view.fields.map((item) => item.value), [value, value, value]);
    assert.ok(view.fields.every((item) => item.changes === 1));
    assert.match(view.status.textContent, /all 3 fields/);
    assert.deepEqual(view.navigations, []);
  });
}

test("changing the default applies all fields, then individual exceptions remain editable", () => {
  const view = page();
  view.changePolicy("keep_destination");
  view.fields[1].value = "use_saved";
  assert.deepEqual(view.fields.map((item) => item.value),
    ["keep_destination", "use_saved", "keep_destination"]);
  assert.equal(view.policy.value, "keep_destination");
});

test("an unsupported bulk policy leaves all selected field values untouched", () => {
  const view = page();
  view.changePolicy("unrecognized_policy");
  assert.ok(view.fields.every((item) => item.value === "use_saved" && item.changes === 0));
  assert.match(view.status.textContent, /^Field choices apply/);
});

test("choosing a profile loads its server-rendered settings without a write request", () => {
  const view = page();
  view.chooseProfile("profile-one");
  assert.deepEqual(view.navigations,
    ["/projects/one/saved-odoo-data?selected=saved-one&profile=profile-one#restore-saved-data"]);
  assert.ok(view.fields.every((item) => item.value === "use_saved"));
});

test("choosing another destination drops the saved-profile selection", () => {
  const view = page();
  view.chooseProfile("");
  assert.deepEqual(view.navigations,
    ["/projects/one/saved-odoo-data?selected=saved-one#restore-saved-data"]);
});

test("profile and version navigation keep the exact selected data version", () => {
  const view = page({ versionValue: "2" });
  view.chooseProfile("profile-one");
  assert.deepEqual(view.navigations,
    ["/projects/one/saved-odoo-data?selected=saved-one&profile=profile-one&version=2#restore-saved-data"]);
  view.chooseVersion("1");
  assert.equal(view.navigations[1],
    "/projects/one/saved-odoo-data?selected=saved-one&profile=profile-one&version=1#restore-saved-data");
});

test("profile navigation rejects another origin", () => {
  const view = page({ profileUrl: "https://untrusted.test/?selected=saved-one" });
  view.chooseProfile("profile-one");
  assert.deepEqual(view.navigations, []);
});

test("pages without the restore form or optional profiles stay usable", () => {
  assert.doesNotThrow(() => page({ formPresent: false }));
  const view = page({ profilePresent: false, fieldCount: 0 });
  assert.doesNotThrow(() => view.clickPreset("fill_empty"));
  assert.match(view.status.textContent, /all 0 fields/);
});
