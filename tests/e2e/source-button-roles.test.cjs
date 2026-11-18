"use strict";
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = (name) => readFileSync(path.join(__dirname, "../../src/impodo/web/static", name), "utf8");

function element(attributes = {}) {
  const attrs = new Map(Object.entries(attributes)), listeners = new Map(), classes = new Set();
  return {
    dataset: {}, disabled: false, hidden: false, innerHTML: "", textContent: "",
    classList: {
      contains: (name) => classes.has(name),
      toggle: (name, value) => value ? classes.add(name) : classes.delete(name),
    },
    setAttribute: (name, value) => attrs.set(name, String(value)),
    removeAttribute: (name) => attrs.delete(name),
    getAttribute: (name) => attrs.get(name) ?? null,
    hasAttribute: (name) => attrs.has(name),
    toggleAttribute(name, value) { if (value) attrs.set(name, ""); else attrs.delete(name); },
    addEventListener(name, listener) {
      if (!listeners.has(name)) listeners.set(name, []);
      listeners.get(name).push(listener);
    },
    fire(name, event = {}) { for (const listener of listeners.get(name) || []) listener(event); },
    focus() { this.focused = true; }, scrollIntoView() {},
  };
}

function sourcePage(confirmed, { locked = false, disabled = [] } = {}) {
  const buttons = [], forms = [], cards = [];
  const save = confirmed.every(Boolean) && !locked ? element({ "data-next-step": "100" }) : null;
  if (save) { save.form = element(); buttons.push(save); }
  for (const [index, saved] of confirmed.entries()) {
    const form = element(), confirm = element(), jump = element(), discard = element(), notice = element();
    const status = element(), card = element();
    const rank = String(200 + confirmed.length - index);
    confirm.dataset.sourceNextPriority = rank;
    confirm.disabled = disabled.includes(index);
    if (!saved && !locked) confirm.setAttribute("data-next-step", rank);
    confirm.textContent = saved ? "Update confirmation" : "Confirm this file";
    confirm.form = form;
    form.controls = [["header_row_0", "1"], ["selected_0", "1"]];
    form.reset = () => { form.controls[0][1] = "1"; };
    const targets = { "[data-source-confirmation-action]": confirm,
      "[data-source-discard-changes]": discard, "[data-source-confirmation-notice]": notice,
      "[data-source-confirmation-next]": saved ? jump : null };
    form.querySelector = (selector) => selector === "fieldset[disabled]" ? (locked ? {} : null) : targets[selector] || null;
    card.dataset = { sourceReviewCard: String(index), sourceReviewConfirmed: String(saved) };
    card.querySelector = (selector) => selector === "form[data-source-review-form]" ? form : targets[selector] || null;
    card.querySelectorAll = () => [status];
    forms.push(form); buttons.push(confirm, jump); cards.push(card);
    Object.assign(card, { form, confirm, discard, notice });
  }
  const document = element(), status = element();
  const review = { dataset: { sourcePositionKey: "test" } };
  status.classList.toggle("complete", confirmed.every(Boolean));
  document.querySelector = (selector) => ({
    "[data-source-review-page]": review,
    "[data-source-page-status]": status,
    "[data-source-save-tables]": save,
  })[selector] || null;
  document.querySelectorAll = (selector) => ({
    "[data-source-review-card]": cards, "[data-source-review-form]": forms,
  })[selector] || [];
  const window = { addEventListener() {}, sessionStorage: { getItem: () => null, removeItem() {} } };
  vm.runInNewContext(script("button-roles.js"), { window, document: { addEventListener() {} } });
  class FormData {
    constructor(form) { this.form = form; }
    entries() { return this.form.controls[Symbol.iterator](); }
  }
  vm.runInNewContext(script("source-workflow.js"), { window, document, FormData });
  document.fire("DOMContentLoaded");
  const next = () => window.ImpodoButtonRoles.selectNext(
    buttons.filter((button) => button.hasAttribute("data-next-step")),
    { visible: (button) => !button.hidden },
  );
  const edit = (index, value) => {
    forms[index].controls[0][1] = value;
    forms[index].fire("input");
  };
  return { cards, forms, save, next, edit, document };
}

test("first entry highlights the first unfinished file and keeps navigation supporting", () => {
  const page = sourcePage([false, false]);
  assert.equal(page.next(), page.cards[0].confirm);
  assert.equal(page.forms[0].controls[0][1], "1");
});

test("confirmation advances to the remaining file, then to saving the tables", () => {
  const remaining = sourcePage([true, false]);
  assert.equal(remaining.next(), remaining.cards[1].confirm);
  const complete = sourcePage([true, true]);
  assert.equal(complete.next(), complete.save);
  assert.equal(complete.cards[0].confirm.hasAttribute("data-next-step"), false);
});

test("editing a saved file restores confirmation and removes the premature table-save highlight", () => {
  const page = sourcePage([true, true]);
  page.edit(1, "2");
  assert.equal(page.next(), page.cards[1].confirm);
  assert.equal(page.cards[1].confirm.textContent, "Confirm changes");
  assert.equal(page.save.hasAttribute("data-next-step"), false);
  let prevented = false;
  page.document.fire("submit", { target: page.save.form,
    preventDefault() { prevented = true; }, stopImmediatePropagation() {} });
  assert.equal(prevented, true, "the existing guard still protects unconfirmed edits");
  assert.equal(page.forms[1].controls[0][1], "2");
});

test("reverting or discarding edits restores the saved next step and original values", () => {
  for (const discard of [false, true]) {
    const page = sourcePage([true, true]);
    page.edit(0, "3");
    if (discard) page.cards[0].discard.fire("click");
    else page.edit(0, "1");
    assert.equal(page.next(), page.save);
    assert.equal(page.forms[0].hasAttribute("data-next-dirty"), false);
    assert.equal(page.forms[0].controls[0][1], "1");
  }
});

test("a reverted unfinished file does not retain dirty priority over the first file", () => {
  const page = sourcePage([false, false]);
  page.edit(1, "2");
  assert.equal(page.next(), page.cards[1].confirm);
  page.edit(1, "1");
  assert.equal(page.next(), page.cards[0].confirm);
});

test("several changed files keep one deterministic next confirmation", () => {
  const page = sourcePage([true, true]);
  page.edit(1, "2"); page.edit(0, "3");
  assert.equal(page.next(), page.cards[0].confirm);
  page.cards[0].discard.fire("click");
  assert.equal(page.next(), page.cards[1].confirm);
  assert.equal(page.save.hasAttribute("data-next-step"), false);
});

test("unavailable and accepted read-only confirmations are never selected", () => {
  const disabled = sourcePage([false, false], { disabled: [0] });
  assert.equal(disabled.next(), disabled.cards[1].confirm);
  const saved = sourcePage([true, true], { locked: true });
  assert.equal(saved.next(), null);
});
