// Run with node --test tests/e2e/comparison-progress.test.cjs.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(
  path.join(__dirname, "../../src/impodo/web/static/job-polling.js"), "utf8"
);

function progressPage(outcomes, initialStatus = "RUNNING") {
  const elements = new Map();
  const selectors = [
    "heading", "intro", "progress-card", "state", "message", "progress",
    "percent", "heartbeat", "spinner", "active", "failed", "failure",
    "failure-title", "failure-code", "failure-action", "retry", "retry-label",
    "previous-review", "complete", "complete-message", "continue",
  ];
  for (const name of selectors) {
    elements.set(`[data-preflight-${name}]`, {
      hidden: false, textContent: "", href: "/summary", classList: { add() {}, remove() {} },
    });
  }
  const operation = { value: "original-page-operation" };
  const csrf = { value: "retained-csrf-token" };
  const retry = elements.get("[data-preflight-retry]");
  retry.method = "post";
  retry.querySelector = (selector) => selector === '[name="operation_id"]' ? operation : csrf;
  const code = { textContent: "" };
  elements.get("[data-preflight-failure-code]").querySelector = () => code;
  const root = {
    dataset: { status: initialStatus, statusUrl: "/comparison/status" },
    querySelector(selector) { return elements.get(selector) || null; },
    setAttribute() {},
  };
  const timers = [];
  const intervals = [];
  const navigations = [];
  let ready;
  let calls = 0;
  vm.runInNewContext(source, {
    document: {
      addEventListener(name, callback) { if (name === "DOMContentLoaded") ready = callback; },
      querySelector(selector) { return selector.includes("[data-preflight-job]") ? root : null; },
    },
    window: {
      setTimeout(callback, delay) { timers.push({ callback, delay }); return timers.length; },
      setInterval(callback) { intervals.push(callback); return intervals.length; },
      clearTimeout() {}, clearInterval() {}, addEventListener() {},
      location: { assign(url) { navigations.push(url); } },
    },
    fetch: async () => {
      const outcome = outcomes[Math.min(calls++, outcomes.length - 1)];
      if (outcome instanceof Error) throw outcome;
      return {
        ok: true, status: 200,
        json: async () => outcome,
        clone() { return { json: async () => outcome }; },
      };
    },
  });
  ready();
  return {
    element(name) { return elements.get(`[data-preflight-${name}]`); },
    operation, csrf, code, root, timers, intervals, navigations,
    flush: () => new Promise((resolve) => setImmediate(resolve)),
    async poll() {
      const timer = timers.shift();
      assert.ok(timer, "a running attempt must schedule its next poll");
      timer.callback();
      await this.flush();
    },
    get calls() { return calls; },
  };
}

const running = {
  status: "RUNNING", message: "Reading Odoo", progress_percent: 20,
  outcome_message: "You can leave this page and return while the read-only comparison continues.",
  updated_at: "2026-11-10T08:50:51Z",
};
const stale = {
  ...running, status: "FAILED", message: "Comparison needs attention",
  outcome_message: "No new comparison was saved.",
  failure_title: "Refresh Odoo details",
  failure_message: "Impodo could not verify the saved Odoo fields or read access.",
  failure_code: "ODOO_SCHEMA_EVIDENCE_STALE",
  failure_action_kind: "link", failure_action_href: "/workspaces/example/schema",
  failure_action_label: "Review Odoo details", previous_review_available: false,
};

test("running to failed shows the actual recovery action without reloading", async () => {
  const page = progressPage([running, stale]);
  await page.flush();
  assert.equal(page.element("progress-card").hidden, false);
  assert.equal(page.element("failed").hidden, true);
  await page.poll();
  assert.equal(page.element("heading").textContent, "Comparison stopped");
  assert.equal(page.element("intro").textContent, "No new comparison was saved.");
  assert.equal(page.element("progress-card").hidden, true);
  assert.equal(page.element("failed").hidden, false);
  assert.equal(page.element("complete").hidden, true);
  assert.equal(page.element("failure-action").hidden, false);
  assert.equal(page.element("failure-action").href, stale.failure_action_href);
  assert.equal(page.element("failure-action").textContent, stale.failure_action_label);
  assert.equal(page.element("retry").hidden, true);
  assert.equal(page.element("previous-review").hidden, true);
  assert.equal(page.code.textContent, stale.failure_code);
  page.intervals[0]();
  assert.equal(page.element("heartbeat").textContent, "No new comparison was saved.");
  assert.equal(page.timers.length, 0, "a terminal attempt must stop polling");
  assert.deepEqual(page.navigations, []);
});

test("an available previous review is labelled separately from this failed attempt", async () => {
  const page = progressPage([{ ...stale, previous_review_available: true }]);
  await page.flush();
  assert.equal(page.element("previous-review").hidden, false);
  assert.equal(page.element("intro").textContent, "No new comparison was saved.");
});

test("retry remains a POST with retained CSRF and a fresh operation identity", async () => {
  const retryFailure = {
    ...stale, failure_action_kind: "retry", failure_action_href: "/summary/compare",
    failure_action_label: "Try comparison again", comparison_operation_id: "new-attempt-operation",
  };
  const page = progressPage([running, retryFailure]);
  await page.flush();
  await page.poll();
  assert.equal(page.element("failure-action").hidden, true);
  assert.equal(page.element("retry").hidden, false);
  assert.equal(page.element("retry").method, "post");
  assert.equal(page.element("retry").action, "/summary/compare");
  assert.equal(page.operation.value, "new-attempt-operation");
  assert.equal(page.csrf.value, "retained-csrf-token");
  assert.equal(page.element("retry-label").textContent, "Try comparison again");
});

test("credential recovery uses the server's protected-form destination", async () => {
  const page = progressPage([{
    ...stale, failure_action_kind: "credential", failure_action_href: "/summary",
    failure_action_label: "Return to final review",
  }]);
  await page.flush();
  assert.equal(page.element("failure-action").hidden, false);
  assert.equal(page.element("failure-action").href, "/summary");
  assert.equal(page.element("failure-action").textContent, "Return to final review");
  assert.equal(page.element("retry").hidden, true);
});

test("successful completion reports saved and keeps its automatic redirect", async () => {
  const page = progressPage([running, {
    ...running, status: "SUCCEEDED", progress_percent: 100,
    outcome_message: "Your new comparison is ready.",
    completion_message: "Prepared data compared with Odoo.", redirect_url: "/load/review",
  }]);
  await page.flush();
  await page.poll();
  assert.equal(page.element("heading").textContent, "Comparison saved");
  assert.equal(page.element("heartbeat").textContent, "The comparison result is saved.");
  assert.equal(page.element("failed").hidden, true);
  assert.equal(page.element("complete").hidden, false);
  assert.equal(page.element("progress").value, 100);
  assert.equal(page.element("continue").href, "/load/review");
  const redirect = page.timers.shift();
  assert.equal(redirect.delay, 700);
  redirect.callback();
  assert.deepEqual(page.navigations, ["/load/review"]);
});

test("reloading a failed attempt cannot claim success during a polling outage", async () => {
  const page = progressPage([new Error("temporary polling failure")], "FAILED");
  await page.flush();
  page.intervals[0]();
  assert.doesNotMatch(page.element("heartbeat").textContent, /result is saved/);
  assert.equal(page.root.dataset.status, "FAILED");
});

test("uncertain publication retains the server's cautious outcome wording", async () => {
  const outcome_message = "Impodo could not confirm the comparison result.";
  const page = progressPage([{ ...stale, outcome_message }]);
  await page.flush();
  assert.equal(page.element("intro").textContent, outcome_message);
  page.intervals[0]();
  assert.equal(page.element("heartbeat").textContent, outcome_message);
});

test("a failure after saving retains the confirmed result instead of claiming no save", async () => {
  const outcome_message = "A new comparison was saved, but a later step needs attention.";
  const page = progressPage([{ ...stale, outcome_message }]);
  await page.flush();
  assert.equal(page.element("intro").textContent, outcome_message);
  assert.equal(page.element("previous-review").hidden, true);
  assert.equal(page.element("progress-card").hidden, true);
});
