"use strict";

// Remember review location only. Saved checks and decisions stay server-owned.
document.addEventListener("DOMContentLoaded", () => {
  const workspace = document.body.dataset.finalReviewWorkspace;
  if (!workspace) return;
  const prefix = `/workspaces/${encodeURIComponent(workspace)}/`;
  const summaryPath = `${prefix}summary`;
  const key = `impodo:final-review-return:${workspace}`;
  const root = document.querySelector("[data-final-review-journey]");
  const parameters = new Set([
    "step", "view", "status", "dataset", "issue", "page", "page_size", "row_page",
    "row_page_size", "quality_status", "quality_dataset", "quality_page",
    "quality_page_size",
  ]);

  function reviewUrl(value) {
    try {
      const url = new URL(value, location.href);
      if (url.origin !== location.origin || url.pathname !== summaryPath) return null;
      for (const name of Array.from(url.searchParams.keys())) {
        if (!parameters.has(name)) url.searchParams.delete(name);
      }
      return url;
    } catch (_error) {
      return null;
    }
  }

  function read() {
    try {
      const value = JSON.parse(sessionStorage.getItem(key) || "null");
      if (!value || typeof value.url !== "string" || !reviewUrl(value.url)) return null;
      if (!Number.isFinite(value.time) || Date.now() - value.time > 8 * 60 * 60 * 1000) return null;
      return value;
    } catch (_error) {
      return null;
    }
  }

  function write(value) {
    try { sessionStorage.setItem(key, JSON.stringify(value)); } catch (_error) { /* Navigation works without storage. */ }
  }

  // Old links can name a section on the former single-page summary.
  // Resolve only those legacy links; explicit step/view choices keep priority.
  if (root && location.hash && !new URL(location.href).searchParams.has("step")) {
    const target = document.getElementById(location.hash.slice(1));
    if (!target || target.closest("[hidden]")) {
      const sections = {
        "#quality-rows": ["prepared", ""],
        "#control-totals": ["prepared", ""],
        "#readiness-rows": ["results", "changes"],
        "#migration-issues": ["results", "attention"],
        "#missing-parent-values": ["results", "attention"],
        "#expected-versus-found": ["results", "changes"],
        "#compare-with-odoo": ["compare", ""],
      };
      const destination = sections[location.hash];
      if (destination) {
        const url = reviewUrl(location.href);
        url.searchParams.set("step", destination[0]);
        if (destination[1]) url.searchParams.set("view", destination[1]);
        location.replace(url.pathname + url.search + url.hash);
        return;
      }
    }
  }

  const saved = read();
  const returnLinks = document.querySelectorAll("[data-final-review-back]");
  if (saved) {
    const url = reviewUrl(saved.url);
    for (const link of returnLinks) link.href = url.pathname + url.search + url.hash;
    const notice = document.querySelector("[data-final-review-return]");
    if (notice) notice.hidden = false;
  }

  for (const link of returnLinks) {
    link.addEventListener("click", () => {
      const value = read();
      if (value) write({ ...value, restore: true });
    });
  }

  if (!root) return;

  if (saved?.restore) {
    write({ ...saved, restore: false });
    const url = reviewUrl(saved.url);
    const current = reviewUrl(location.href);
    if (
      !location.hash && url.search === current.search &&
      saved.evidence === root.dataset.reviewEvidence && Number.isFinite(saved.scroll)
    ) {
      requestAnimationFrame(() => {
        window.scrollTo(0, Math.max(0, saved.scroll));
        const focused = typeof saved.focus === "string" ? document.getElementById(saved.focus) : null;
        if (focused && !focused.closest("[hidden]")) focused.focus({ preventScroll: true });
      });
    }
  }

  document.addEventListener("click", (event) => {
    const link = event.target.closest?.("a[href]");
    if (!link || event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    if (link.target === "_blank" || link.hasAttribute("download")) return;
    const destination = new URL(link.href, location.href);
    if (destination.origin !== location.origin || !destination.pathname.startsWith(prefix)) return;
    if (destination.pathname === summaryPath || /\.(xlsx|zip)$/.test(destination.pathname)) return;
    const current = reviewUrl(location.href);
    if (!current) return;
    current.searchParams.set("step", root.dataset.step);
    if (root.dataset.step === "results") current.searchParams.set("view", root.dataset.view);
    const focus = document.activeElement?.id || "";
    write({
      url: current.pathname + current.search + current.hash,
      evidence: root.dataset.reviewEvidence,
      scroll: window.scrollY,
      focus,
      time: Date.now(),
      restore: false,
    });
  });
});
