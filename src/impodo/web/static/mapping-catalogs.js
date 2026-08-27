"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const {
    initializeLazySourceSelect,
    initializeScalarRow,
    mappingForm,
    restoreRelationRow,
    restoreScalarRow,
  } = window.impodoMappingEditor;

  const fieldCatalogSearchDelayMs = 350;
  for (const catalog of document.querySelectorAll(
    "[data-scalar-field-catalog]"
  )) {
    const search = catalog.querySelector("[data-scalar-field-search]");
    const searchSubmit = catalog.querySelector("[data-field-search-submit]");
    const mappedOnly = catalog.querySelector("[data-show-mapped-scalars]");
    const count = catalog.querySelector("[data-scalar-field-count]");
    const topScroll = catalog.querySelector(
      "[data-scalar-table-scroll-top]"
    );
    const topScrollSpacer = catalog.querySelector(
      "[data-scalar-table-scroll-spacer]"
    );
    const tableScroll = catalog.querySelector("[data-scalar-table-scroll]");
    let scalarTable = tableScroll?.querySelector(".mapping-table");
    let rows = Array.from(
      catalog.querySelectorAll("[data-scalar-field-row]")
    );
    const updateScalarTableScroll = () => {
      if (!topScroll || !topScrollSpacer || !tableScroll) {
        return;
      }
      const scrollWidth = tableScroll.scrollWidth;
      topScrollSpacer.style.width = `${scrollWidth}px`;
      topScroll.hidden = scrollWidth <= tableScroll.clientWidth + 1;
      if (!topScroll.hidden) {
        topScroll.scrollLeft = tableScroll.scrollLeft;
      }
    };
    topScroll?.addEventListener("scroll", () => {
      if (tableScroll && tableScroll.scrollLeft !== topScroll.scrollLeft) {
        tableScroll.scrollLeft = topScroll.scrollLeft;
      }
    });
    tableScroll?.addEventListener("scroll", () => {
      if (topScroll && topScroll.scrollLeft !== tableScroll.scrollLeft) {
        topScroll.scrollLeft = tableScroll.scrollLeft;
      }
    });
    window.addEventListener("resize", updateScalarTableScroll);
    let scrollResizeObserver;
    if (tableScroll && "ResizeObserver" in window) {
      scrollResizeObserver = new ResizeObserver(updateScalarTableScroll);
      scrollResizeObserver.observe(tableScroll);
      if (scalarTable) {
        scrollResizeObserver.observe(scalarTable);
      }
    }
    const updateScalarFieldRows = () => {
      const query = search?.value.trim().toLowerCase() || "";
      let visible = 0;
      let mapped = 0;
      for (const row of rows) {
        const provider = row.querySelector("[data-value-source]");
        const isMapped = Boolean(provider?.value);
        const matches =
          (row.dataset.fieldSearchText || "").includes(query) &&
          (!mappedOnly?.checked || isMapped);
        row.hidden = !matches;
        row.dataset.mapped = String(isMapped);
        visible += matches ? 1 : 0;
        mapped += isMapped ? 1 : 0;
      }
      if (count) {
        const matchingTotal = catalog.dataset.scalarMatchingTotal || rows.length;
        const mappedTotal = catalog.dataset.scalarMappedTotal || mapped;
        count.textContent =
          `Showing ${visible} of ${matchingTotal} fields · ${mappedTotal} matched`;
      }
      window.requestAnimationFrame(updateScalarTableScroll);
    };
    const initializeCatalogRows = () => {
      rows = Array.from(
        catalog.querySelectorAll("[data-scalar-field-row]")
      );
      for (const row of rows) {
        restoreScalarRow(row);
        initializeScalarRow(row);
        const provider = row.querySelector("[data-value-source]");
        if (
          provider &&
          provider.dataset.catalogCountInitialized !== "true"
        ) {
          provider.dataset.catalogCountInitialized = "true";
          provider.addEventListener("change", updateScalarFieldRows);
        }
      }
    };
    let fieldSearchTimer;
    let fieldSearchController;
    const catalogSearchUrl = (requestedUrl = null) => {
      const url = new URL(requestedUrl || window.location.href);
      if (requestedUrl === null) {
        const searchValue = search?.value.trim() || "";
        if (searchValue) {
          url.searchParams.set("field_query", searchValue);
        } else {
          url.searchParams.delete("field_query");
        }
        if (mappedOnly?.checked) {
          url.searchParams.set("mapped_only", "1");
        } else {
          url.searchParams.delete("mapped_only");
        }
        url.searchParams.set("scalar_page", "1");
      }
      return url;
    };
    const catalogRequestUrl = (stateUrl) => {
      const endpoint = catalog.dataset.scalarSearchUrl;
      if (!endpoint) {
        return stateUrl;
      }
      const requestUrl = new URL(endpoint, window.location.href);
      requestUrl.search = stateUrl.search;
      return requestUrl;
    };
    const loadScalarCatalog = async (requestedUrl = null) => {
      window.clearTimeout(fieldSearchTimer);
      fieldSearchController?.abort();
      const activeController = new AbortController();
      fieldSearchController = activeController;
      const stateUrl = catalogSearchUrl(requestedUrl);
      const requestUrl = catalogRequestUrl(stateUrl);
      catalog.setAttribute("aria-busy", "true");
      if (count) {
        count.textContent = "Searching Odoo fields\u2026";
      }
      try {
        const response = await fetch(requestUrl, {
          headers: { Accept: "text/html" },
          signal: activeController.signal,
        });
        if (!response.ok) {
          throw new Error("The field list could not be updated. Please try again.");
        }
        const documentResult = new DOMParser().parseFromString(
          await response.text(),
          "text/html"
        );
        const incomingCatalog = documentResult.querySelector(
          "[data-scalar-field-catalog]"
        );
        const incomingTableScroll = incomingCatalog?.querySelector(
          "[data-scalar-table-scroll]"
        );
        if (!incomingCatalog || !incomingTableScroll || !tableScroll) {
          throw new Error("Field search returned an incomplete result.");
        }
        tableScroll.replaceChildren(
          ...Array.from(incomingTableScroll.childNodes, (node) =>
            document.importNode(node, true)
          )
        );
        scalarTable = tableScroll.querySelector(".mapping-table");
        if (scalarTable && scrollResizeObserver) {
          scrollResizeObserver.observe(scalarTable);
        }
        const pagination = catalog.querySelector("[data-scalar-pagination]");
        const incomingPagination = incomingCatalog.querySelector(
          "[data-scalar-pagination]"
        );
        if (pagination && incomingPagination) {
          pagination.replaceWith(document.importNode(incomingPagination, true));
        }
        for (const name of [
          "scalarCatalogTotal",
          "scalarMatchingTotal",
          "scalarMappedTotal",
        ]) {
          catalog.dataset[name] = incomingCatalog.dataset[name] || "0";
        }
        initializeCatalogRows();
        updateScalarFieldRows();
        window.history.replaceState(
          {},
          "",
          `${stateUrl.pathname}${stateUrl.search}${stateUrl.hash}`
        );
        if (mappingForm) {
          const saveUrl = new URL(
            mappingForm.getAttribute("action") || window.location.pathname,
            window.location.href
          );
          saveUrl.search = stateUrl.search;
          mappingForm.setAttribute(
            "action",
            `${saveUrl.pathname}${saveUrl.search}`
          );
        }
      } catch (error) {
        if (error?.name === "AbortError") {
          return;
        }
        if (count) {
          count.textContent =
            error instanceof Error
              ? error.message
              : "Field search failed.";
        }
      } finally {
        if (fieldSearchController === activeController) {
          catalog.removeAttribute("aria-busy");
        }
      }
    };
    const scheduleScalarCatalogSearch = () => {
      window.clearTimeout(fieldSearchTimer);
      updateScalarFieldRows();
      if (count) {
        count.textContent = "Searching Odoo fields\u2026";
      }
      fieldSearchTimer = window.setTimeout(
        () => loadScalarCatalog(),
        fieldCatalogSearchDelayMs
      );
    };
    search?.addEventListener("input", scheduleScalarCatalogSearch);
    search?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        loadScalarCatalog();
      }
    });
    searchSubmit?.addEventListener("click", () => loadScalarCatalog());
    mappedOnly?.addEventListener("change", () => loadScalarCatalog());
    catalog.addEventListener("click", (event) => {
      const link = event.target.closest("[data-scalar-pagination] a");
      if (!link) {
        return;
      }
      event.preventDefault();
      loadScalarCatalog(link.href);
    });
    initializeCatalogRows();
    updateScalarFieldRows();
  }

  for (const catalog of document.querySelectorAll(
    "[data-relation-field-catalog]"
  )) {
    const search = catalog.querySelector("[data-relation-field-search]");
    const searchSubmit = catalog.querySelector(
      "[data-relation-search-submit]"
    );
    const count = catalog.querySelector("[data-relation-field-count]");
    const results = catalog.querySelector("[data-relation-field-results]");
    let relationSearchTimer;
    let relationSearchController;

    const initializeRelationRows = () => {
      for (const row of catalog.querySelectorAll(
        "[data-relation-mapping-row]"
      )) {
        restoreRelationRow(row);
        for (const select of row.querySelectorAll(
          "select[data-lazy-source-column]"
        )) {
          initializeLazySourceSelect(select);
        }
      }
    };
    const relationCatalogUrl = (requestedUrl = null) => {
      const url = new URL(requestedUrl || window.location.href);
      if (requestedUrl === null) {
        const searchValue = search?.value.trim() || "";
        if (searchValue) {
          url.searchParams.set("relation_query", searchValue);
        } else {
          url.searchParams.delete("relation_query");
        }
        url.searchParams.set("relation_page", "1");
      }
      return url;
    };
    const relationRequestUrl = (stateUrl) => {
      const endpoint = catalog.dataset.relationSearchUrl;
      if (!endpoint) {
        return stateUrl;
      }
      const requestUrl = new URL(endpoint, window.location.href);
      requestUrl.search = stateUrl.search;
      requestUrl.searchParams.set("catalog", "relation");
      return requestUrl;
    };
    const loadRelationCatalog = async (requestedUrl = null) => {
      window.clearTimeout(relationSearchTimer);
      relationSearchController?.abort();
      const activeController = new AbortController();
      relationSearchController = activeController;
      const stateUrl = relationCatalogUrl(requestedUrl);
      const requestUrl = relationRequestUrl(stateUrl);
      catalog.setAttribute("aria-busy", "true");
      if (count) {
        count.textContent = "Searching linked Odoo fields\u2026";
      }
      try {
        const response = await fetch(requestUrl, {
          headers: { Accept: "text/html" },
          signal: activeController.signal,
        });
        if (!response.ok) {
          throw new Error(
            "The linked-field list could not be updated. Please try again."
          );
        }
        const documentResult = new DOMParser().parseFromString(
          await response.text(),
          "text/html"
        );
        const incomingCatalog = documentResult.querySelector(
          "[data-relation-field-catalog]"
        );
        const incomingResults = incomingCatalog?.querySelector(
          "[data-relation-field-results]"
        );
        if (!incomingCatalog || !incomingResults || !results) {
          throw new Error("Linked-field search returned an incomplete result.");
        }
        results.replaceChildren(
          ...Array.from(incomingResults.childNodes, (node) =>
            document.importNode(node, true)
          )
        );
        const pagination = catalog.querySelector("[data-relation-pagination]");
        const incomingPagination = incomingCatalog.querySelector(
          "[data-relation-pagination]"
        );
        if (pagination && incomingPagination) {
          pagination.replaceWith(document.importNode(incomingPagination, true));
        }
        for (const name of [
          "relationCatalogTotal",
          "relationMatchingTotal",
          "relationMappedTotal",
          "relationPageSize",
        ]) {
          catalog.dataset[name] = incomingCatalog.dataset[name] || "0";
        }
        const incomingCount = incomingCatalog.querySelector(
          "[data-relation-field-count]"
        );
        if (count && incomingCount) {
          count.textContent = incomingCount.textContent.trim();
        }
        initializeRelationRows();
        window.history.replaceState(
          {},
          "",
          `${stateUrl.pathname}${stateUrl.search}${stateUrl.hash}`
        );
        if (mappingForm) {
          const saveUrl = new URL(
            mappingForm.getAttribute("action") || window.location.pathname,
            window.location.href
          );
          saveUrl.search = stateUrl.search;
          mappingForm.setAttribute(
            "action",
            `${saveUrl.pathname}${saveUrl.search}`
          );
        }
      } catch (error) {
        if (error?.name === "AbortError") {
          return;
        }
        if (count) {
          count.textContent =
            error instanceof Error
              ? error.message
              : "Linked-field search failed.";
        }
      } finally {
        if (relationSearchController === activeController) {
          catalog.removeAttribute("aria-busy");
        }
      }
    };
    const scheduleRelationCatalogSearch = () => {
      window.clearTimeout(relationSearchTimer);
      if (count) {
        count.textContent = "Searching linked Odoo fields\u2026";
      }
      relationSearchTimer = window.setTimeout(
        () => loadRelationCatalog(),
        fieldCatalogSearchDelayMs
      );
    };
    search?.addEventListener("input", scheduleRelationCatalogSearch);
    search?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        loadRelationCatalog();
      }
    });
    searchSubmit?.addEventListener("click", () => loadRelationCatalog());
    catalog.addEventListener("click", (event) => {
      const link = event.target.closest("[data-relation-pagination] a");
      if (!link) {
        return;
      }
      event.preventDefault();
      loadRelationCatalog(link.href);
    });
    initializeRelationRows();
  }

});
