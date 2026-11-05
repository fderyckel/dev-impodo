"use strict";

(() => {
  const steps = ["company", "tables", "check", "confirm"];
  const tasks = ["rows", "identity", "fields", "links"];
  const titles = {
    company: "Choose the company",
    tables: "Match your tables",
    check: "Check your matches",
    confirm: "Confirm your matches",
  };
  const destinationForHash = (hash) => {
    if (/company/.test(hash)) return { step: "company" };
    if (/next-step-blockers|semantic-validation|health|matching-order|required-values|migration-issues|related-data-review/.test(hash)) {
      return { step: "check" };
    }
    if (/business-expectations|rows-to-use-review/.test(hash)) return { step: "confirm" };
    if (/rows-to-use-/.test(hash)) return { step: "tables", task: "rows" };
    if (/source-identity|target-identity/.test(hash)) return { step: "tables", task: "identity" };
    if (/relationships-|relationship-|relation-/.test(hash)) return { step: "tables", task: "links" };
    if (/scalar-|scalar-fields-|field-/.test(hash)) return { step: "tables", task: "fields" };
    if (/mapping-dataset-/.test(hash)) return { step: "tables", task: "rows" };
    return null;
  };

  const create = (root) => {
    const form = root.querySelector("[data-mapping-form]");
    const stepPanels = Array.from(root.querySelectorAll("[data-mapping-step-panel]"));
    const activeTable = root.querySelector("[data-mapping-active-table]");
    const taskPanels = Array.from((activeTable || root).querySelectorAll("[data-mapping-task-panel]"));
    const availableTasks = tasks.filter((item) => taskPanels.some((panel) => panel.dataset.mappingTaskPanel === item));
    let step = steps.includes(root.dataset.step) ? root.dataset.step : "tables";
    let task = tasks.includes(root.dataset.task) ? root.dataset.task : "rows";
    let continuationUsed = false;

    const updateFormUrl = () => {
      if (!form) return;
      const url = new URL(form.getAttribute("action"), window.location.href);
      url.searchParams.set("step", step);
      url.searchParams.set("task", task);
      form.setAttribute("action", `${url.pathname}${url.search}`);
    };
    const render = () => {
      if (!availableTasks.includes(task)) task = availableTasks[0] || "rows";
      root.dataset.step = step;
      root.dataset.task = task;
      for (const panel of stepPanels) panel.hidden = panel.dataset.mappingStepPanel !== step;
      for (const panel of taskPanels) panel.hidden = panel.dataset.mappingTaskPanel !== task;
      for (const control of root.querySelectorAll("[data-mapping-go-step]")) {
        if (control.dataset.mappingGoStep === step) control.setAttribute("aria-current", "step");
        else control.removeAttribute("aria-current");
      }
      for (const control of root.querySelectorAll("[data-mapping-go-task]")) {
        const current = control.dataset.mappingGoTask === task;
        control.setAttribute("aria-pressed", String(current));
        if (current) control.setAttribute("aria-current", "step");
        else control.removeAttribute("aria-current");
      }
      for (const button of root.querySelectorAll("[data-mapping-continue]")) {
        const lastTask = task === availableTasks[availableTasks.length - 1];
        button.textContent = !lastTask ? "Save and continue" :
          button.dataset.mappingContinuationUrl ? "Save and match next table" : "Save and review matches";
      }
      const title = root.querySelector("[data-mapping-step-title]");
      if (title) title.textContent = titles[step];
      for (const link of document.querySelectorAll(".sidebar-subnav-link[href]")) {
        const url = new URL(link.href, window.location.href);
        if (url.pathname !== window.location.pathname || !steps.includes(url.searchParams.get("step"))) continue;
        const current = url.searchParams.get("step") === step;
        link.classList.toggle("active", current);
        if (current) {
          link.setAttribute("aria-current", "page");
          const breadcrumb = document.querySelector('.project-breadcrumb [aria-current="page"]');
          if (breadcrumb) breadcrumb.textContent = link.querySelector("span")?.textContent || titles[step];
        } else link.removeAttribute("aria-current");
      }
      updateFormUrl();
    };
    const focusHeading = () => {
      const panel = step === "tables"
        ? taskPanels.find((item) => item.dataset.mappingTaskPanel === task)
        : null;
      const heading = panel?.querySelector("[data-mapping-task-heading], h2, h3") ||
        root.querySelector("[data-mapping-journey-heading]");
      if (!heading) return;
      heading.setAttribute("tabindex", "-1");
      window.requestAnimationFrame(() => {
        heading.scrollIntoView({ block: "start", behavior: "auto" });
        heading.focus({ preventScroll: true });
        window.impodoMappingPosition?.remember?.();
      });
    };
    const show = (next, { history = "push", focus = false, hash = "" } = {}) => {
      if (steps.includes(next.step)) step = next.step;
      if (tasks.includes(next.task)) task = next.task;
      render();
      if (history) {
        const url = new URL(window.location.href);
        url.searchParams.set("step", step);
        url.searchParams.set("task", task);
        url.hash = hash;
        window.history[history === "replace" ? "replaceState" : "pushState"](
          window.history.state, "", url
        );
      }
      if (focus) focusHeading();
    };
    const reveal = (target, { history = "replace" } = {}) => {
      if (!target) return;
      const stepPanel = target.closest("[data-mapping-step-panel]");
      const taskPanel = target.closest("[data-mapping-task-panel]");
      const inferred = destinationForHash(target.id || "");
      show({
        step: stepPanel?.dataset.mappingStepPanel || inferred?.step || step,
        task: taskPanel?.dataset.mappingTaskPanel || inferred?.task || task,
      }, { history, hash: window.location.hash });
      for (let ancestor = target; ancestor && ancestor !== root; ancestor = ancestor.parentElement) {
        if (ancestor.tagName === "DETAILS") ancestor.open = true;
        if (ancestor.hasAttribute?.("data-table-fields-panel")) {
          ancestor.hidden = false;
          const toggle = root.querySelector(`[aria-controls="${ancestor.id}"]`);
          if (toggle?.getAttribute("aria-expanded") !== "true") toggle?.click?.();
          toggle?.setAttribute("aria-expanded", "true");
        }
      }
    };
    const continuationFor = (submitter) => {
      if (!submitter?.hasAttribute("data-mapping-continue")) return null;
      const desired = submitter.dataset.mappingContinue;
      const url = submitter.dataset.mappingContinuationUrl;
      if (desired === "table") return { url, preserveFilters: submitter.dataset.mappingPreserveFilters === "true" };
      if (tasks.includes(desired)) return { step: "tables", task: desired };
      if (steps.includes(desired)) return { step: desired, task: "rows" };
      const index = availableTasks.indexOf(task);
      if (step === "tables" && index < availableTasks.length - 1) {
        return { step: "tables", task: availableTasks[index + 1] };
      }
      return url ? { url } : { step: "check" };
    };
    const tableUrl = (value) => {
      const url = new URL(value, window.location.href);
      url.searchParams.set("step", "tables");
      url.searchParams.set("task", "rows");
      for (const key of ["field_query", "field_target", "field_page", "scalar_query", "scalar_target", "relation_query", "relation_target"]) {
        url.searchParams.delete(key);
      }
      url.searchParams.set("scalar_page", "1");
      url.searchParams.set("relation_page", "1");
      url.hash = "";
      return url;
    };
    const committed = (operation, payload) => {
      const continuation = operation.journeyContinuation;
      if (!continuation || operation.action !== "save_progress" || operation.journeyContinuationUsed) return false;
      operation.journeyContinuationUsed = true;
      continuationUsed = true;
      const url = continuation.url;
      if (url) {
        window.location.assign(continuation.preserveFilters ? url : tableUrl(url));
      } else {
        show(continuation, { focus: true });
      }
      return true;
    };
    const resultUrl = (value, operation) => {
      const url = new URL(value || window.location.href, window.location.href);
      if (url.origin !== window.location.origin || url.pathname !== window.location.pathname) return url.href;
      const resultStep = operation.action === "confirm_rows" ? "confirm" :
        steps.includes(url.searchParams.get("step")) ? url.searchParams.get("step") : "check";
      url.searchParams.set("step", resultStep);
      url.searchParams.set("task", task);
      if (resultStep === "check" && !url.hash) url.hash = "semantic-validation";
      return url.href;
    };
    const cancelContinuation = (operation) => {
      if (operation) operation.journeyContinuation = null;
    };

    const saveBeforeNavigation = (url, preserveFilters = false) => {
      if (!form || form.getAttribute("aria-busy") === "true") return;
      const button = document.createElement("button");
      button.type = "submit";
      button.name = "action";
      button.value = "save_progress";
      button.hidden = true;
      button.dataset.mappingContinue = "table";
      button.dataset.mappingContinuationUrl = url.href;
      button.dataset.mappingPreserveFilters = String(preserveFilters);
      form.append(button);
      form.requestSubmit(button);
      button.remove();
    };
    document.addEventListener("click", (event) => {
      const target = event.target?.closest?.("[data-mapping-go-step], [data-mapping-go-task], [data-mapping-table-link], a[href]");
      if (!target || event.button > 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      if (form?.getAttribute("aria-busy") === "true") {
        event.preventDefault();
        return;
      }
      if (target.hasAttribute("data-mapping-table-link")) {
        const url = tableUrl(target.href);
        if (url.origin !== window.location.origin || url.pathname !== window.location.pathname) return;
        event.preventDefault();
        const currentIndex = root.dataset.activeDatasetIndex || new URL(window.location.href).searchParams.get("mapping_dataset") || "0";
        if (url.searchParams.get("mapping_dataset") === currentIndex) {
          show({ step: "tables", task: "rows" }, { focus: true });
        } else saveBeforeNavigation(url);
        return;
      }
      if (!target.hasAttribute("data-mapping-go-step") && !target.hasAttribute("data-mapping-go-task")) {
        const url = new URL(target.href, window.location.href);
        if (url.origin !== window.location.origin || url.pathname !== window.location.pathname) return;
        const targetStep = url.searchParams.get("step");
        if (target.matches(".sidebar-subnav-link") && steps.includes(targetStep)) {
          event.preventDefault();
          show({ step: targetStep }, { focus: true });
          return;
        }
        if (!url.hash) return;
        let destination;
        try { destination = document.getElementById(decodeURIComponent(url.hash.slice(1))); } catch { return; }
        event.preventDefault();
        if (destination && !destination.closest("[data-mapping-dataset][hidden]")) {
          window.history.pushState(window.history.state, "", url);
          reveal(destination);
          destination.setAttribute("tabindex", "-1");
          destination.scrollIntoView({ block: "start", behavior: "auto" });
          destination.focus({ preventScroll: true });
        } else saveBeforeNavigation(url, true);
        return;
      }
      event.preventDefault();
      show(target.hasAttribute("data-mapping-go-step")
        ? { step: target.dataset.mappingGoStep }
        : { step: "tables", task: target.dataset.mappingGoTask }, { focus: true });
    });
    let invalidTarget = null;
    form?.addEventListener("invalid", (event) => {
      if (invalidTarget) return;
      invalidTarget = event.target;
      window.queueMicrotask(() => { invalidTarget = null; });
      reveal(event.target);
      event.target.focus({ preventScroll: true });
      event.target.scrollIntoView({ block: "center", behavior: "auto" });
    }, true);
    document.addEventListener("submit", (event) => {
      if (event.target === form) return;
      const action = event.target?.getAttribute?.("action");
      if (!action) return;
      const url = new URL(action, window.location.href);
      const suffix = url.pathname.slice(window.location.pathname.length);
      if (!url.pathname.startsWith(`${window.location.pathname}/`) ||
          !/^\/(company(?:\/choices|\/refresh)?|order(?:\/check)?|quality|review-workbook|expectations)$/.test(suffix)) return;
      const state = window.impodoMappingEditor?.getState?.();
      if (!state?.dirty && !state?.busy) return;
      event.preventDefault();
      const notice = form?.querySelector("[data-mapping-save-outcome]");
      const message = notice?.querySelector("[data-mapping-save-outcome-message]");
      if (message) message.textContent = state.busy
        ? "Resolve the current save outcome before continuing."
        : "Save your matching edits before continuing. Your edits are still here.";
      if (notice) {
        notice.hidden = false;
        notice.focus();
        notice.scrollIntoView({ block: "center", behavior: "auto" });
      }
    }, true);
    window.addEventListener("popstate", () => {
      if (form?.getAttribute("aria-busy") === "true") {
        window.impodoMappingEditor?.cancelPendingContinuation?.();
      }
      const url = new URL(window.location.href);
      const inferred = destinationForHash(url.hash);
      let target;
      try { target = document.getElementById(decodeURIComponent(url.hash.slice(1))); } catch { target = null; }
      show({ step: url.searchParams.get("step") || inferred?.step || "tables", task: url.searchParams.get("task") || inferred?.task || "rows" }, { history: false, focus: !target });
      if (target) {
        reveal(target, { history: false });
        target.scrollIntoView({ block: "start", behavior: "auto" });
      }
    });
    window.addEventListener("hashchange", () => {
      let target;
      try { target = document.getElementById(decodeURIComponent(window.location.hash.slice(1))); } catch { return; }
      if (target) {
        reveal(target, { history: false });
        target.scrollIntoView({ block: "start", behavior: "auto" });
      }
    });
    const explicitHash = destinationForHash(window.location.hash);
    const initialUrl = new URL(window.location.href);
    if (explicitHash) {
      step = explicitHash.step;
      task = explicitHash.task || task;
    } else if (root.dataset.companyRequired === "true" && !steps.includes(initialUrl.searchParams.get("step"))) {
      step = "company";
    } else {
      step = steps.includes(initialUrl.searchParams.get("step")) ? initialUrl.searchParams.get("step") : step;
      task = tasks.includes(initialUrl.searchParams.get("task")) ? initialUrl.searchParams.get("task") : task;
    }
    render();
    return { show, reveal, continuationFor, committed, resultUrl, cancelContinuation,
      get continuationUsed() { return continuationUsed; } };
  };

  window.impodoMappingJourney = { create, destinationForHash };
  document.addEventListener("DOMContentLoaded", () => {
    const root = document.querySelector("[data-mapping-journey]");
    if (root) Object.assign(window.impodoMappingJourney, create(root));
  });
})();
