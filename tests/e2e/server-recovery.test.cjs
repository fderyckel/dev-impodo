// Exercise the shipped recovery script with a controllable monotonic clock.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../../src/impodo/web/static/server-recovery.js"), "utf8");

function browser() {
  let clock = 1;
  let nextTimer = 0;
  let status = 200;
  const requests = [];
  const timers = new Map();
  const listeners = new Map();
  const events = [];
  const element = {textContent: "", addEventListener() {}};
  const banner = {
    querySelector: () => element, dataset: {}, classList: {add() {}, remove() {}}, setAttribute() {},
  };
  const document = {
    querySelector: () => banner,
    addEventListener: (name, handler) => listeners.set(name, handler),
    dispatchEvent: event => events.push(event.type),
  };
  const window = {
    addEventListener() {},
    setTimeout: (cb, delay) => { timers.set(++nextTimer, {cb, delay}); return nextTimer; },
    clearTimeout: id => timers.delete(id),
  };
  vm.runInNewContext(source, {
    window, document, performance: {now: () => clock}, AbortController,
    CustomEvent: class { constructor(type) { this.type = type; } },
    fetch: async (url, options) => {
      requests.push([url, options.method || "GET"]);
      if (!status) throw new Error("offline");
      return {status, ok: status === 200, json: async () => ({status: "ok"})};
    },
  });
  listeners.get("DOMContentLoaded")();
  return {recovery: window.impodoServerRecovery, timers, events, requests,
    advance(ms) { clock += ms; }, respondWith(code) { status = code; }};
}

test("a minute of progress, server loss and reconnection retain an uncertain mutation", async () => {
  const b = browser();
  b.recovery.beginOperation({label: "Compare", mutation: true});
  for (let i = 0; i < 15; i++) {
    b.advance(4000);
    await b.recovery.checkNow();
  }
  assert.equal(b.recovery.state().operation, "DELAYED");
  assert.equal(b.recovery.state().connectivity, "CONNECTED");
  b.respondWith(0);
  b.advance(45001);
  await b.recovery.checkNow();
  assert.equal(b.recovery.state().connectivity, "DISCONNECTED");
  b.respondWith(200);
  b.advance(100);
  await b.recovery.checkNow();
  assert.equal(b.recovery.state().connectivity, "CONNECTED");
  assert.equal(b.recovery.state().operation, "OUTCOME_UNKNOWN");
  assert.ok(b.requests.every(([url, method]) => url === "/health" && method === "GET"));
  b.recovery.endOperation({outcomeKnown: true});
  assert.equal(b.recovery.state().operation, "IDLE");
});

test("401 from another request cancels the heartbeat and stays terminal", async () => {
  const b = browser();
  b.recovery.beginOperation({mutation: true});
  b.recovery.noteResponse({ok: false, status: 401});
  assert.equal(b.recovery.state().connectivity, "SESSION_ENDED");
  assert.equal(b.recovery.state().operation, "OUTCOME_UNKNOWN");
  assert.deepEqual(b.events, ["impodo:session-ended"]);
  assert.equal(b.timers.size, 0);
  await b.recovery.checkNow();
  assert.equal(b.requests.length, 0);
});
