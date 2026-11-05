"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const dialog = document.querySelector("[data-duplicate-rows-dialog]");
  if (!dialog) return;
  const mainForm = document.querySelector("[data-mapping-form]");
  const signature = () => JSON.stringify(Array.from(new FormData(mainForm)).filter(
    ([name]) => !["csrf_token", "operation_id"].includes(name)
  ));
  let initialForm;
  setTimeout(() => { initialForm = signature(); }, 0);
  const find = (name) => dialog.querySelector(`[data-duplicate-${name}]`);
  const status = find("status"), error = find("error"), groups = find("groups");
  const preview = find("preview"), reviewButton = find("review"), applyButton = find("apply");
  const retry = find("retry"), previous = find("previous"), next = find("next");
  let sourceUrl, data, selected = new Set(), previewHash = "", operationId = "", busy = false, opener;
  let retryAction = null, saved = false;
  const node = (tag, text, parent) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (parent) parent.append(element);
    return element;
  };
  const message = (text) => { error.textContent = text; error.hidden = !text; };
  const lock = (value) => {
    busy = value;
    for (const control of dialog.querySelectorAll("button, input")) control.disabled = value;
    if (!value) {
      reviewButton.disabled = !selected.size || Boolean(data?.unavailable) || saved;
      previous.disabled = data?.previous == null || Boolean(selected.size) || saved;
      next.disabled = data?.next == null || Boolean(selected.size) || saved;
    }
  };
  const request = async (url, options = {}) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(url, { credentials: "same-origin", cache: "no-store", ...options, signal: controller.signal });
      window.impodoServerRecovery?.noteResponse?.(response);
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || "The review could not be completed. Your selections are still here.");
      return result;
    } catch (failure) {
      if (failure.name === "AbortError") throw new Error("The request timed out. Your selections are still here.");
      throw failure;
    } finally { clearTimeout(timer); }
  };
  const resetPreview = () => {
    previewHash = ""; operationId = ""; preview.hidden = true; applyButton.hidden = true;
    reviewButton.hidden = false; retry.hidden = true; message("");
    status.textContent = `${selected.size} selected to exclude · ${data.included_count - selected.size} rows would remain in this table`;
    lock(false);
  };
  const render = () => {
    groups.replaceChildren();
    for (const group of data.groups) {
      const section = node("section", undefined, groups);
      section.className = "duplicate-row-group";
      node("h3", group.key, section);
      node("p", `${group.rows.length} shown of ${group.total} rows with this matching identity`, section);
      const scroll = node("div", undefined, section);
      scroll.className = "table-scroll"; scroll.tabIndex = 0;
      scroll.setAttribute("role", "region"); scroll.setAttribute("aria-label", `Compare ${group.key}`);
      const table = node("table", undefined, scroll); table.className = "data-table";
      const heading = node("tr", undefined, node("thead", undefined, table));
      for (const title of ["Decision", "Included source position", ...data.columns]) {
        node("th", title, heading).scope = "col";
      }
      const body = node("tbody", undefined, table);
      const differs = data.columns.map((_, index) => new Set(group.rows.map(row => row.values[index])).size > 1);
      for (const row of group.rows) {
        const tr = node("tr", undefined, body);
        const label = node("label", undefined, node("td", undefined, tr));
        const checkbox = node("input", undefined, label); checkbox.type = "checkbox";
        checkbox.setAttribute("aria-label", `Exclude source position ${row.position}`);
        checkbox.addEventListener("change", () => {
          if (checkbox.checked) selected.add(row.position); else selected.delete(row.position);
          tr.dataset.excluded = String(checkbox.checked); resetPreview();
        });
        label.append(document.createTextNode("Exclude"));
        node("td", String(row.position), tr);
        row.values.forEach((value, index) => {
          const cell = node("td", value === "" ? "(blank)" : value, tr);
          if (differs[index]) { cell.className = "duplicate-value-differs"; cell.title = "Values differ in this group"; }
        });
      }
    }
    find("pages").hidden = data.previous == null && data.next == null;
    resetPreview();
    status.textContent = `Showing ${data.start}–${data.end} of ${data.matching_count} affected rows. No rows selected.`;
    if (data.unavailable) message(data.unavailable);
  };
  const load = async (url) => {
    lock(true); message(""); status.textContent = "Reading the checked source rows…";
    try {
      data = await request(url); sourceUrl = url; selected = new Set(); saved = false; render();
      retryAction = () => load(url);
    } catch (failure) {
      message(failure.message); retryAction = () => load(url); retry.hidden = false;
    } finally { lock(false); }
  };
  const form = (action) => {
    const body = new URLSearchParams();
    body.set("csrf_token", mainForm.elements.namedItem("csrf_token").value);
    body.set("action", action); body.set("dataset_id", data.dataset_id); body.set("check_id", data.check_id);
    body.set("expected_version", String(data.expected_version));
    body.set("expected_parent_version", data.expected_parent_version == null ? "" : String(data.expected_parent_version));
    body.set("after", new URL(sourceUrl, location.href).searchParams.get("after") || "");
    body.set("before", new URL(sourceUrl, location.href).searchParams.get("before") || "");
    for (const position of [...selected].sort((a, b) => a - b)) body.append("position", String(position));
    if (action === "apply") { body.set("preview_hash", previewHash); body.set("operation_id", operationId); }
    return body;
  };
  const reviewSelection = async () => {
    lock(true); message(""); retry.hidden = true;
    try {
      const result = await request(dialog.dataset.endpoint, { method: "POST", body: form("preview") });
      preview.replaceChildren();
      node("strong", `Exclude ${result.excluded_count} selected rows; keep ${result.included_count} rows in this table.`, preview);
      node("p", `${result.remaining_groups_on_page} duplicate groups shown on this page would still need attention. Other identity or Odoo issues may remain.`, preview);
      for (const impact of result.impacts) {
        node("p", impact.count == null
          ? `${impact.table} has a relationship through ${impact.field} whose affected rows cannot be counted here. Review this relationship after applying exclusions.`
          : `${impact.count} included rows in ${impact.table} refer to these records through ${impact.field}. ${impact.follows_parent ? "Their Rows to use rule follows this parent and will exclude them when checked again." : "Review these relationships after the exclusion; these rows will not be automatically excluded."}`, preview);
      }
      node("p", "Review this effect, then apply. Impodo will check the saved identities again. Rows to use still requires its normal checked confirmation before you continue.", preview);
      previewHash = result.preview_hash; operationId = crypto.randomUUID();
      preview.hidden = false; applyButton.hidden = false; reviewButton.hidden = true;
      preview.scrollIntoView({ block: "nearest" }); applyButton.focus();
    } catch (failure) { message(failure.message); retryAction = reviewSelection; retry.hidden = false; }
    finally { lock(false); if (previewHash) applyButton.focus(); }
  };
  const checkSaved = async () => {
    lock(true); retry.hidden = true;
    try {
      status.textContent = "Exclusions saved. Starting the identity check…";
      const body = JSON.stringify({ entries: [["csrf_token", mainForm.elements.namedItem("csrf_token").value]] });
      const result = await request(dialog.dataset.checkUrl, { method: "POST", body, headers: { "Content-Type": "application/json", Accept: "application/json" } });
      if (!result.check_id) throw new Error("The identity check could not be confirmed. Try again to check the saved selection.");
      const destination = new URL(location.href); destination.searchParams.set("mapping_dataset", String(data.dataset_index));
      destination.searchParams.set("order_check", result.check_id); destination.hash = "identity-health-title";
      location.assign(destination.href);
    } catch (failure) {
      status.textContent = "Exclusions are saved in Rows to use. The identity check has not started.";
      message(failure.message); retryAction = checkSaved; retry.hidden = false;
      const link = node("a", "Review saved Rows to use", error);
      link.href = `${location.pathname}?mapping_dataset=${data.dataset_index}#rows-to-use-${data.dataset_index}`;
      applyButton.hidden = true; reviewButton.hidden = true; lock(false);
    }
  };
  const apply = async () => {
    lock(true); message(""); retry.hidden = true;
    try {
      const result = await request(dialog.dataset.endpoint, { method: "POST", body: form("apply") });
      if (result.saved !== true) throw new Error("The save outcome could not be confirmed.");
      saved = true; await checkSaved();
    } catch (failure) {
      message(`${failure.message} If the response was interrupted, Try again checks the same save outcome.`);
      retryAction = apply; retry.hidden = false; lock(false);
      for (const checkbox of dialog.querySelectorAll('input[type="checkbox"]')) checkbox.disabled = true;
      applyButton.hidden = true; reviewButton.hidden = true;
    }
  };
  for (const link of document.querySelectorAll("[data-duplicate-rows-url]")) {
    link.addEventListener("click", (event) => {
      event.preventDefault(); opener = link; dialog.showModal();
      if (initialForm !== signature()) {
        groups.replaceChildren(); preview.hidden = true; reviewButton.hidden = true; applyButton.hidden = true;
        retry.hidden = true; retryAction = null; find("pages").hidden = true; status.textContent = "";
        message("You have unsaved matching choices. Close this dialog, save progress, and check current data before reviewing exclusions. Your edits are still on the page.");
        return;
      }
      groups.replaceChildren(); data = null; selected = new Set(); preview.hidden = true;
      applyButton.hidden = true; reviewButton.hidden = false; retry.hidden = true;
      load(link.dataset.duplicateRowsUrl);
    });
  }
  for (const button of dialog.querySelectorAll("[data-close-duplicate-rows]")) {
    button.addEventListener("click", () => { if (!busy) dialog.close(); });
  }
  dialog.addEventListener("cancel", event => { if (busy) event.preventDefault(); });
  dialog.addEventListener("close", () => opener?.focus());
  for (const [button, parameter] of [[previous, "before"], [next, "after"]]) {
    button.addEventListener("click", () => {
      if (selected.size) return;
      const url = new URL(sourceUrl, location.href); url.searchParams.delete("after"); url.searchParams.delete("before");
      url.searchParams.set(parameter, String(parameter === "after" ? data.next : data.previous)); load(url.href);
    });
  }
  reviewButton.addEventListener("click", reviewSelection); applyButton.addEventListener("click", apply);
  retry.addEventListener("click", () => retryAction?.());
});
