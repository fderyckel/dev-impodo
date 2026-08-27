"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const storageKey = `impodo.mapping.position:${window.location.pathname}`;
  let lastRow = null;
  let lastControl = null;

  const setupTableFieldsDisclosure = () => {
    for (const toggle of document.querySelectorAll("[data-table-fields-toggle]")) {
      const panelId = toggle.getAttribute("aria-controls");
      const panel = panelId ? document.getElementById(panelId) : null;
      if (!panel) {
        continue;
      }
      const dataset = toggle.closest("[data-mapping-dataset]");
      const label = toggle.querySelector("[data-table-fields-toggle-label]");
      const chevron = toggle.querySelector(".mapping-table-fields-chevron");
      const summary = dataset?.querySelector("[data-table-fields-summary]");
      const expandedCopy = dataset?.querySelector("[data-table-fields-expanded-copy]");

      const setExpanded = (expanded) => {
        toggle.setAttribute("aria-expanded", String(expanded));
        panel.hidden = !expanded;
        summary?.toggleAttribute("hidden", expanded);
        expandedCopy?.toggleAttribute("hidden", !expanded);
        chevron?.classList.toggle("collapsed", !expanded);
        if (label) {
          label.textContent = expanded
            ? "Close this table's fields"
            : "Open this table's fields";
        }
      };

      toggle.addEventListener("click", () => {
        setExpanded(toggle.getAttribute("aria-expanded") !== "true");
      });
      setExpanded(toggle.getAttribute("aria-expanded") === "true");
    }
  };

  const rememberInteraction = (target) => {
    const row = target?.closest?.("[data-target-field]");
    if (!row) {
      return;
    }
    lastRow = row;
    if (target.name) {
      lastControl = target;
    }
  };

  const visibleRow = () => {
    const candidates = Array.from(
      document.querySelectorAll("[data-target-field]")
    )
      .map((row) => ({ row, bounds: row.getBoundingClientRect() }))
      .filter(
        ({ bounds }) =>
          bounds.height > 0 &&
          bounds.bottom > 0 &&
          bounds.top < window.innerHeight
      );
    return candidates.reduce((nearest, candidate) => {
      if (!nearest) {
        return candidate;
      }
      const viewportReference = Math.min(160, window.innerHeight / 4);
      return Math.abs(candidate.bounds.top - viewportReference) <
        Math.abs(nearest.bounds.top - viewportReference)
        ? candidate
        : nearest;
    }, null)?.row;
  };

  const remember = () => {
    const active = document.activeElement;
    const activeRow = active?.closest?.("[data-target-field]");
    const row = activeRow || (lastRow?.isConnected ? lastRow : null) || visibleRow();
    const focusControl = activeRow
      ? active
      : lastControl?.isConnected
        ? lastControl
        : active;
    const dataset = row?.closest("[data-mapping-dataset]");
    const horizontal = Array.from(
      document.querySelectorAll("[data-scalar-table-scroll][id]")
    ).map((element) => [element.id, element.scrollLeft]);
    try {
      window.sessionStorage.setItem(
        storageKey,
        JSON.stringify({
          scrollY: window.scrollY,
          focusName: focusControl?.name || "",
          focusValue: focusControl?.value || "",
          datasetId: dataset?.dataset.mappingDataset || "",
          targetField: row?.dataset.targetField || "",
          targetOffset: row?.getBoundingClientRect().top ?? null,
          horizontal,
        })
      );
    } catch {
      // Navigation remains usable when browser storage is unavailable.
    }
  };

  const restore = () => {
    if (window.location.hash === "#next-step-blockers") {
      try {
        window.sessionStorage.removeItem(storageKey);
      } catch {
        // The blocker anchor remains usable without browser storage.
      }
      window.requestAnimationFrame(() => {
        document
          .querySelector("#next-step-blockers")
          ?.focus({ preventScroll: true });
      });
      return;
    }
    let stored = null;
    try {
      stored = JSON.parse(window.sessionStorage.getItem(storageKey) || "null");
      window.sessionStorage.removeItem(storageKey);
    } catch {
      stored = null;
    }
    if (!stored || !document.querySelector("[data-mapping-form]")) {
      return;
    }
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        const row = Array.from(document.querySelectorAll("[data-target-field]")).find(
          (candidate) =>
            candidate.dataset.targetField === stored.targetField &&
            (!stored.datasetId ||
              candidate.closest("[data-mapping-dataset]")?.dataset
                .mappingDataset === stored.datasetId)
        );
        const targetTop =
          row && Number.isFinite(stored.targetOffset)
            ? window.scrollY + row.getBoundingClientRect().top - stored.targetOffset
            : stored.scrollY;
        window.scrollTo({ top: Math.max(0, targetTop || 0), behavior: "auto" });
        for (const [id, scrollLeft] of stored.horizontal || []) {
          const element = document.getElementById(id);
          if (element) {
            element.scrollLeft = scrollLeft;
          }
        }
        const focusTarget = Array.from(
          (row || document).querySelectorAll("[name]")
        ).find(
          (control) =>
            control.name === stored.focusName &&
            (!stored.focusValue || control.value === stored.focusValue)
        );
        focusTarget?.focus({ preventScroll: true });
      });
    });
  };

  window.impodoMappingPosition = { rememberInteraction, remember };
  setupTableFieldsDisclosure();
  restore();
});
