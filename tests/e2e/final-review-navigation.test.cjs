// Browser-state evidence for Final review return and legacy navigation.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = readFileSync(path.join(__dirname,
  "../../src/impodo/web/static/final-review-navigation.js"), "utf8");
const key = "impodo:final-review-return:one";

function page({ url = "https://impodo.test/workspaces/one/summary?step=results&view=attention",
  summary = true, evidence = "prepared:compared", saved = null, denied = false,
  target = null } = {}) {
  const store = new Map(saved ? [[key, JSON.stringify(saved)]] : []);
  const events = new Map();
  const navigations = [];
  const scrolls = [];
  const current = new URL(url);
  const location = { href: current.href, pathname: current.pathname,
    origin: current.origin, hash: current.hash, replace: value => navigations.push(value) };
  const root = { dataset: { step: "results", view: "attention", reviewEvidence: evidence } };
  const back = { href: "/workspaces/one/summary", addEventListener: (name, cb) => events.set(`back:${name}`, cb) };
  const notice = { hidden: true };
  const document = {
    body: { dataset: { finalReviewWorkspace: "one" } }, activeElement: { id: "issue-1" },
    addEventListener: (name, cb) => events.set(name, cb),
    querySelector: selector => selector === "[data-final-review-journey]" ? (summary ? root : null) : notice,
    querySelectorAll: () => [back],
    getElementById: () => target,
  };
  const sessionStorage = {
    getItem(name) { if (denied) throw new Error("denied"); return store.get(name) || null; },
    setItem(name, value) { if (denied) throw new Error("denied"); store.set(name, value); },
  };
  vm.runInNewContext(script, { document, location, URL, sessionStorage, Date,
    window: { scrollY: 420, scrollTo: (...value) => scrolls.push(value) },
    requestAnimationFrame: cb => cb() });
  events.get("DOMContentLoaded")();
  return { store, events, back, notice, navigations, scrolls,
    click(href, options = {}) {
      const link = { href, target: "", hasAttribute: () => false };
      events.get("click")?.({ target: { closest: () => link }, ...options });
    } };
}

function saved(overrides = {}) {
  return { url: "/workspaces/one/summary?step=results&view=attention&dataset=products&row_page=2",
    evidence: "prepared:compared", scroll: 420, time: Date.now(), restore: false, ...overrides };
}

test("a correction remembers the exact review scope and never starts a request", () => {
  const view = page({ url: "https://impodo.test/workspaces/one/summary?step=results&view=attention&dataset=products&row_page=2" });
  view.click("https://impodo.test/workspaces/one/mapping?step=tables#relationship-0-category_id");
  const stored = JSON.parse(view.store.get(key));
  assert.equal(stored.url, "/workspaces/one/summary?step=results&view=attention&dataset=products&row_page=2");
  assert.equal(stored.scroll, 420);
  assert.equal(stored.restore, false);
  assert.deepEqual(view.navigations, []);
});

test("the upstream return control restores only the originating same-workspace review", () => {
  const view = page({ summary: false, url: "https://impodo.test/workspaces/one/mapping", saved: saved() });
  assert.equal(view.notice.hidden, false);
  assert.match(view.back.href, /dataset=products&row_page=2/);
  view.events.get("back:click")();
  assert.equal(JSON.parse(view.store.get(key)).restore, true);
});

test("changed comparison evidence prevents restoring a scroll that hides recovery", () => {
  const view = page({ evidence: "new:comparison", saved: saved({ restore: true }) });
  assert.deepEqual(view.scrolls, []);
  assert.equal(JSON.parse(view.store.get(key)).restore, false);
});

test("returning to unchanged evidence restores the saved position once", () => {
  const view = page({ url: "https://impodo.test/workspaces/one/summary?step=results&view=attention&dataset=products&row_page=2",
    saved: saved({ restore: true }) });
  assert.deepEqual(view.scrolls, [[0, 420]]);
  assert.equal(JSON.parse(view.store.get(key)).restore, false);
});

test("an explicit section takes priority over remembered scroll", () => {
  const view = page({ url: "https://impodo.test/workspaces/one/summary?step=results&view=attention#readiness-rows",
    saved: saved({ restore: true }) });
  assert.deepEqual(view.scrolls, []);
  assert.deepEqual(view.navigations, []);
});

test("legacy hashes reveal their owning step without adding history", () => {
  const view = page({ url: "https://impodo.test/workspaces/one/summary#expected-versus-found" });
  assert.deepEqual(view.navigations, ["/workspaces/one/summary?step=results&view=changes#expected-versus-found"]);
});

test("explicit outside-view links are not reclassified as prepared-data links", () => {
  const view = page({ url: "https://impodo.test/workspaces/one/summary?step=results&view=outside#quality-rows" });
  assert.deepEqual(view.navigations, []);
});

for (const unsafe of ["https://elsewhere.test/workspaces/one/summary", "/workspaces/other/summary", "/workspaces/one/load/confirm"]) {
  test(`reject unsafe return location ${unsafe}`, () => {
    const view = page({ summary: false, saved: saved({ url: unsafe }) });
    assert.equal(view.notice.hidden, true);
    assert.equal(view.back.href, "/workspaces/one/summary");
  });
}

test("downloads and modified clicks leave the current return context alone", () => {
  const view = page();
  view.click("https://impodo.test/workspaces/one/summary/attention.xlsx");
  view.click("https://impodo.test/workspaces/one/mapping", { metaKey: true });
  assert.equal(view.store.size, 0);
});

test("browser storage failure leaves ordinary navigation usable", () => {
  const view = page({ denied: true });
  assert.doesNotThrow(() => view.click("https://impodo.test/workspaces/one/mapping"));
  assert.deepEqual(view.navigations, []);
});
