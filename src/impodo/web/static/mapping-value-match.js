"use strict";

(() => {
  const create = (mappingForm) => {
    const valueMatchDialog = document.querySelector(
      "[data-value-match-dialog]"
    );
    if (!valueMatchDialog) return;
    const valueMatchTitle = valueMatchDialog?.querySelector(
      "[data-value-match-title]"
    );
    const valueMatchError = valueMatchDialog?.querySelector(
      "[data-value-match-error]"
    );
    const valueMatchStatus = valueMatchDialog?.querySelector(
      "[data-value-match-status]"
    );
    const valueMatchTableWrap = valueMatchDialog?.querySelector(
      "[data-value-match-table-wrap]"
    );
    const valueMatchFreshness = valueMatchDialog?.querySelector(
      "[data-value-match-freshness]"
    );
    const valueMatchFreshnessText = valueMatchDialog?.querySelector(
      "[data-value-match-freshness-text]"
    );
    const refreshValueMatch = valueMatchDialog?.querySelector(
      "[data-refresh-value-match]"
    );
    const valueMatchRows = valueMatchDialog?.querySelector(
      "[data-value-match-rows]"
    );
    const useValueMatches = valueMatchDialog?.querySelector(
      "[data-use-value-matches]"
    );
    const spinner = valueMatchDialog.querySelector("[data-value-match-spinner]");
    const results = valueMatchDialog.querySelector("[data-value-match-results]");
    const retryButton = valueMatchDialog.querySelector("[data-retry-value-match]");
    const supportDetails = valueMatchDialog.querySelector("[data-value-match-support]");
    const credentialDialog = document.querySelector("[data-read-credential-dialog]");
    const slowAfterMs = 5_000;
    // Allow for the local Odoo reader's default 120-second command timeout.
    const requestTimeoutMs = 150_000;
    const blankCreate = window.impodoBlankCreate?.bind(valueMatchDialog,
      () => activeValueMatch, (trigger, options) => openValueMatch(trigger, options));
    let activeValueMatch = null;
    let valueMatchRequest = null;
    let retryValueMatch = null;
    let readCredentialRetry = null;
    const stagedSelectionChoices = new Map();
    const selectionRefreshStorage = document.createElement("input");
    selectionRefreshStorage.type = "hidden";
    selectionRefreshStorage.name = "selection_refreshes";
    selectionRefreshStorage.value = "[]";
    mappingForm.append(selectionRefreshStorage);
    const isLanguageMatch = () => activeValueMatch?.kind === "scalar" &&
      activeValueMatch.trigger.dataset.targetField === "lang";
    const refreshLabel = () => isLanguageMatch() ? "Refresh languages from Odoo" :
      activeValueMatch?.kind === "scalar" ? "Refresh Odoo choices" : "Refresh Odoo values";
    const parseValueMatches = (storage) => {
      try {
        const values = JSON.parse(storage.value || "[]");
        return new Map(
          values
            .filter(
              (item) =>
                item &&
                typeof item.source_value === "string" &&
                (typeof item.target_value === "string" || item.action === "leave_blank")
            )
            .map((item) => [item.source_value, item.action === "leave_blank" ? null : item.target_value])
        );
      } catch {
        return new Map();
      }
    };
    const showValueMatchError = (message, detail, retry) => {
      retryValueMatch = retry;
      valueMatchStatus.textContent = activeValueMatch.lastPayload
        ? `${message} · Showing previous choices` : message;
      valueMatchError.textContent = detail;
      supportDetails.hidden = !detail;
      retryButton.hidden = false;
    };

    const setPending = (pending) => {
      spinner.hidden = !pending;
      results.inert = pending;
      results.setAttribute("aria-busy", String(pending));
      refreshValueMatch.disabled = pending;
      if (pending) useValueMatches.disabled = true;
    };
    const resetValueMatchDialog = () => {
      blankCreate?.reset();
      window.impodoValueMatchCompany?.reset(valueMatchDialog);
      const incomingNotice = valueMatchDialog.querySelector("[data-value-match-incoming-notice]");
      if (incomingNotice) {
        incomingNotice.hidden = true;
        incomingNotice.textContent = "";
      }
      if (valueMatchRows) {
        valueMatchRows.replaceChildren();
      }
      if (valueMatchTableWrap) {
        valueMatchTableWrap.hidden = true;
      }
      if (useValueMatches) {
        useValueMatches.disabled = true;
      }
      if (valueMatchFreshness) {
        valueMatchFreshness.hidden = true;
        valueMatchFreshness.open = false;
      }
      if (valueMatchFreshnessText) {
        valueMatchFreshnessText.textContent = "";
      }
      if (refreshValueMatch) {
        refreshValueMatch.hidden = true;
      }
    };
    const renderValueMatchRows = (payload, confirmation) => {
      if (!valueMatchRows || !activeValueMatch) {
        return;
      }
      const sourceChoices = Array.isArray(payload.source_choices)
        ? payload.source_choices
        : [];
      const targetChoices = Array.isArray(payload.target_choices)
        ? payload.target_choices
        : [];
      const targetValues = new Set(
        targetChoices.map((choice) => String(choice.value))
      );
      const companyFilter = window.impodoValueMatchCompany?.render(
        valueMatchDialog, activeValueMatch.trigger, targetChoices, () => valueMatchRows.querySelectorAll("select[data-source-value]").forEach(releaseTargetSelect));
      const existing =
        activeValueMatch.pendingMatches ||
        parseValueMatches(activeValueMatch.storage);
      activeValueMatch.lastPayload = payload;
      activeValueMatch.selectionRefresh = payload.selection_refresh || activeValueMatch.selectionRefresh;
      const required = activeValueMatch.trigger.closest("[data-relation-mapping-row]")
        ?.querySelector('[name^="relation_required_"]:checked, [name^="relation_required_create_"]:checked');
      const allowBlank = payload.allow_blank === true && !required;
      let blankChoice = `leave-blank:${window.crypto.randomUUID()}`;
      while (targetValues.has(blankChoice)) blankChoice = `leave-blank:${window.crypto.randomUUID()}`;
      activeValueMatch.blankChoice = blankChoice;
      const origins = window.impodoValueMatchOrigins?.create(valueMatchDialog, payload, targetValues, blankChoice);
      const ambiguousCount = payload.ambiguous_values?.length || 0;
      const targetChoiceLabel = (choice) => {
        const value = String(choice.value);
        const label = String(choice.label || choice.value);
        return companyFilter?.hasCompany(value) || label === value ? label : `${label} — ${value}`;
      };
      const updateValueMatchProgress = (prefix = "") => {
        const selects = Array.from(
          valueMatchRows.querySelectorAll("select[data-source-value]")
        );
        const combined = origins?.progress(selects);
        const matched = combined?.matched ?? selects.filter((select) => select.value).length;
        activeValueMatch.matchedCount = matched;
        const unavailableCount = selects.filter(
          (select) => select.selectedOptions[0]?.dataset.needsReview === "true"
        ).length;
        const remaining = Math.max(0, sourceChoices.length - matched);
        if (!retryValueMatch) {
          const progress = combined?.text || (!sourceChoices.length ? "No source values to match"
            : !targetChoices.length ? "No Odoo choices available"
            : `${matched - unavailableCount} of ${sourceChoices.length} matched`);
          valueMatchStatus.textContent = [
            prefix,
            progress,
            unavailableCount ? `${unavailableCount} saved ${unavailableCount === 1 ? "choice needs" : "choices need"} review` : "",
            ambiguousCount ? `${ambiguousCount} duplicate Odoo ${ambiguousCount === 1 ? "key omitted" : "keys omitted"}` : "",
          ].filter(Boolean).join(" · ");
        }
        if (useValueMatches) {
          useValueMatches.disabled = matched === 0 && existing.size === 0 && !blankCreate?.hasChoice(activeValueMatch);
          useValueMatches.textContent =
            matched === 0 && existing.size
              ? "Clear saved matches"
              : remaining
                ? `Use ${matched} partial match(es)`
                : "Use complete matches";
          if (blankCreate?.value() && blankCreate.hasChoice(activeValueMatch)) {
            useValueMatches.textContent = "Use matches and blank fallback";
          }
          if (activeValueMatch.selectionRefresh) {
            useValueMatches.textContent = isLanguageMatch()
              ? "Use refreshed languages and matches" : "Use refreshed choices and matches";
          }
        }
      };
      blankCreate?.render(payload, activeValueMatch, targetChoiceLabel, () => updateValueMatchProgress());
      const hydrateTargetSelect = (select) => {
        if (select.dataset.choicesLoaded === "true") {
          return;
        }
        const selectedValue = select.value;
        for (const choice of targetChoices) {
          const value = String(choice.value);
          if (value === selectedValue || companyFilter?.excludes(value)) {
            continue;
          }
          const option = document.createElement("option");
          option.value = value;
          option.textContent = targetChoiceLabel(choice);
          select.append(option);
        }
        select.dataset.choicesLoaded = "true";
      };
      const releaseTargetSelect = (select) => {
        for (const option of Array.from(select.options)) {
          if (option.value && !option.selected && !option.dataset.leaveBlank) {
            option.remove();
          }
        }
        select.dataset.choicesLoaded = "false";
      };
      for (const sourceChoice of sourceChoices) {
        const sourceValue = String(sourceChoice.value);
        const row = document.createElement("tr");
        const sourceCell = document.createElement("td");
        const countCell = document.createElement("td");
        const targetCell = document.createElement("td");
        const select = document.createElement("select");
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "Choose Odoo choice";
        select.append(placeholder);
        if (allowBlank) {
          const blank = document.createElement("option");
          blank.value = blankChoice;
          blank.textContent = "Leave blank (no linked record)";
          blank.dataset.leaveBlank = "true";
          select.append(blank);
        }
        select.dataset.sourceValue = sourceValue;
        select.setAttribute("aria-label", `Odoo choice for ${sourceValue}`);

        const savedTarget = existing.get(sourceValue);
        const scopedSuggestion = payload.scoped_suggestions?.[sourceValue];
        const suggestedTarget = origins ? origins.suggestion(sourceValue)
          : targetValues.has(sourceValue) ? sourceValue
          : targetValues.has(scopedSuggestion) ? scopedSuggestion : "";
        const selectedTarget =
          existing.has(sourceValue) && savedTarget === null && allowBlank
            ? blankChoice
            : savedTarget && (activeValueMatch.kind !== "scalar" || targetValues.has(savedTarget))
            ? savedTarget
            : existing.has(sourceValue) ? "" : suggestedTarget;
        const unavailable = savedTarget && !targetValues.has(savedTarget);
        if (unavailable) {
          window.impodoSelectionChoices?.annotateUnavailable(
            select, targetCell, savedTarget, payload, targetChoices, blankChoice
          );
        }
        if (selectedTarget === blankChoice) {
          select.value = blankChoice;
        } else if (selectedTarget) {
          const choice = targetChoices.find(
            (item) => String(item.value) === selectedTarget
          );
          const selectedOption = document.createElement("option");
          selectedOption.value = selectedTarget;
          selectedOption.textContent = choice
            ? targetChoiceLabel(choice)
            : `${selectedTarget} (saved choice; needs review)`;
          if (!choice) selectedOption.dataset.needsReview = "true";
          selectedOption.selected = true;
          select.append(selectedOption);
        }
        select.addEventListener("pointerdown", () =>
          hydrateTargetSelect(select)
        );
        select.addEventListener("focus", () => hydrateTargetSelect(select));
        select.addEventListener("keydown", () => hydrateTargetSelect(select));
        select.addEventListener("change", () => {
          updateValueMatchProgress();
          window.queueMicrotask(() => releaseTargetSelect(select));
        });
        select.addEventListener("blur", () => releaseTargetSelect(select));

        sourceCell.textContent = sourceValue;
        countCell.textContent = String(sourceChoice.count);
        targetCell.append(select);
        row.append(sourceCell, countCell, targetCell);
        origins?.attach(row, sourceValue, select);
        valueMatchRows.append(row);
      }

      if (valueMatchTableWrap) {
        valueMatchTableWrap.hidden = !sourceChoices.length;
      }
      if (useValueMatches) {
        useValueMatches.disabled = sourceChoices.length === 0;
      }
      activeValueMatch.sourceCount = sourceChoices.length;
      if (
        payload.target_checked_at &&
        valueMatchFreshness
      ) {
        const checkedAt = new Date(payload.target_checked_at);
        const checkedLabel = Number.isNaN(checkedAt.getTime())
          ? "recently"
          : new Intl.DateTimeFormat(undefined, {
              dateStyle: "medium",
              timeStyle: "short",
            }).format(checkedAt);
        valueMatchFreshness.hidden = false;
        if (valueMatchFreshnessText) {
          valueMatchFreshnessText.textContent = payload.target_choices_reused
            ? `Using saved Odoo values checked ${checkedLabel}.`
            : `Odoo values checked and saved ${checkedLabel}.`;
          if (activeValueMatch.kind === "scalar") {
            valueMatchFreshnessText.textContent = window.impodoSelectionChoices?.freshnessText(
              isLanguageMatch(), payload, checkedLabel, !targetChoices.length
            ) || `Odoo choices checked ${checkedLabel}.`;
          }
        }
        if (refreshValueMatch) {
          refreshValueMatch.textContent = refreshLabel();
          refreshValueMatch.hidden = false;
        }
      }
      updateValueMatchProgress(confirmation);
      if (confirmation) {
        const displayedMatch = activeValueMatch;
        window.setTimeout(() => {
          if (activeValueMatch === displayedMatch && !valueMatchRequest) updateValueMatchProgress();
        }, 2500);
      }
    };

    const requireChoice = (row, control, message) => {
      const notice = row.querySelector("[data-value-match-prerequisite]");
      notice.textContent = message;
      notice.hidden = false;
      notice.onclick = () => control.focus();
      notice.focus();
    };

    const openValueMatch = async (trigger, { refresh = false, checkBlankDefault = false } = {}) => {
      if (valueMatchRequest && valueMatchDialog.open) return;

      const kind = trigger.dataset.valueMatchKind || "";
      const row = trigger.closest(
        kind === "scalar" ? "[data-scalar-mapping-row]" : "[data-relation-mapping-row]"
      );
      const storage = row?.querySelector("[data-value-match-storage]");
      if (!(storage instanceof HTMLInputElement)) return;

      let sourceColumnKey;
      let businessKeyId = "";
      let resolverOrigin = "target_catalog";
      let incomingDatasetId = "";
      if (kind === "scalar") {
        const provider = row.querySelector("[data-value-source]");
        const source = row.querySelector('[name^="scalar_source_"]');
        if (!["source", "source_with_fallback"].includes(provider.value)) {
          requireChoice(row, provider, "Choose Source value first");
          return;
        }
        if (!source.value) {
          requireChoice(row, source, "Choose a source column first");
          return;
        }
        sourceColumnKey = source.value;
      } else {
        const origin = row.querySelector('[name^="relation_origin_"]');
        const source = row.querySelector('[name^="relation_source_"]');
        const key = row.querySelector('[name^="relation_key_"]');
        if (!["target_catalog", "target_then_dataset"].includes(origin.value)) {
          requireChoice(row, origin, "Choose an option that checks existing Odoo records");
          return;
        }
        const selectedSources = Array.from(source.selectedOptions, (option) => option.value);
        if (selectedSources.length !== 1 || !selectedSources[0]) {
          requireChoice(row, source, "Choose one source column first");
          return;
        }
        if (!key.value) {
          requireChoice(row, key, "Choose a matching rule first");
          return;
        }
        sourceColumnKey = selectedSources[0];
        businessKeyId = key.value;
        resolverOrigin = origin.value;
        if (resolverOrigin === "target_then_dataset") {
          const incoming = row.querySelector('[name^="relation_dataset_"]');
          if (!incoming?.value) {
            requireChoice(row, incoming || origin, "Choose an incoming table first");
            return;
          }
          incomingDatasetId = incoming.value;
        }
      }
      row.querySelector("[data-value-match-prerequisite]").hidden = true;

      const previous = valueMatchDialog.open && activeValueMatch?.trigger === trigger
        ? activeValueMatch : null;
      const hasPreviousChoices = Boolean(previous?.lastPayload);
      const pendingMatches = hasPreviousChoices ? new Map(
        Array.from(valueMatchRows.querySelectorAll("select[data-source-value]"), (select) => [
          select.dataset.sourceValue,
          select.value === previous.blankChoice ? null : select.value,
        ])
      ) : null;
      const pendingBlankCreate = hasPreviousChoices ? blankCreate?.value() : null;
      const applyWasDisabled = !hasPreviousChoices || useValueMatches.disabled;
      if (!hasPreviousChoices) resetValueMatchDialog();
      activeValueMatch = {
        ...previous,
        trigger,
        storage,
        kind,
        pendingMatches,
        blankCreate: blankCreate?.state(row, businessKeyId, pendingBlankCreate, checkBlankDefault),
        selectionRefresh: previous?.selectionRefresh ?? stagedSelectionChoices.get(
          `${trigger.dataset.targetModel}.${trigger.dataset.targetField}`
        ),
      };

      valueMatchTitle.textContent = `Match source choices to ${trigger.dataset.targetLabel || "Odoo"}`;
      const odooChoices = isLanguageMatch() ? "languages" : kind === "scalar" ? "Odoo choices" : "Odoo values";
      const loadingMessage = checkBlankDefault ? "Checking Odoo default…"
        : refresh ? `Refreshing ${odooChoices}…` : "Loading choices…";
      retryValueMatch = null;
      retryButton.hidden = true;
      supportDetails.hidden = true;
      supportDetails.open = false;
      refreshValueMatch.hidden = !hasPreviousChoices;
      setPending(true);
      if (!valueMatchDialog.open) valueMatchDialog.showModal();
      valueMatchStatus.textContent = loadingMessage;

      const body = new FormData();
      body.set("csrf_token", mappingForm.querySelector('input[name="csrf_token"]')?.value || "");
      body.set("kind", kind);
      body.set("dataset_id", trigger.dataset.datasetId || "");
      body.set("source_column_key", sourceColumnKey);
      body.set("target_model", trigger.dataset.targetModel || "");
      body.set("target_field", trigger.dataset.targetField || "");
      body.set("business_key_id", businessKeyId);
      body.set("resolver_origin", resolverOrigin);
      body.set("incoming_dataset_id", incomingDatasetId);
      body.set("refresh", refresh ? "1" : "0");
      if (checkBlankDefault) body.set("check_blank_default", "1");
      body.set("selection_refreshes", selectionRefreshStorage.value);

      valueMatchRequest?.abort();
      const activeRequest = new AbortController();
      valueMatchRequest = activeRequest;
      const isCurrent = () => valueMatchRequest === activeRequest && valueMatchDialog.open;
      const slowTimer = window.setTimeout(() => {
        if (isCurrent()) valueMatchStatus.textContent = "Still waiting for choices…";
      }, slowAfterMs);
      const deadline = window.setTimeout(() => activeRequest.abort("timeout"), requestTimeoutMs);
      const retry = { trigger, refresh, checkBlankDefault };
      try {
        const response = await fetch(valueMatchDialog.dataset.valueMatchEndpoint, {
          method: "POST",
          headers: { Accept: "application/json" },
          body,
          signal: activeRequest.signal,
        });
        window.impodoServerRecovery?.noteResponse?.(response);
        const payload = await response.json().catch(() => null);
        if (!isCurrent()) return;
        activeRequest.signal.throwIfAborted();
        if (!response.ok) {
          if (payload?.read_credential_required === true &&
              window.impodoOpenReadCredentialDialog?.({
                message: payload.detail || "Enter a read-only Odoo key to load these choices.",
                resume: "stay",
                trigger: retryButton,
              })) {
            readCredentialRetry = retry;
            showValueMatchError("Odoo read key needed", "", retry);
            return;
          }
          throw new Error(typeof payload?.detail === "string" ? payload.detail : "");
        }
        if (!Array.isArray(payload?.source_choices) || !Array.isArray(payload?.target_choices)) {
          throw new Error("Impodo returned an invalid response.");
        }
        resetValueMatchDialog();
        const confirmation = refresh
          ? `${odooChoices[0].toUpperCase()}${odooChoices.slice(1)} refreshed`
          : "";
        renderValueMatchRows(payload, confirmation);
      } catch (error) {
        if (!isCurrent()) return;
        const timedOut = activeRequest.signal.reason === "timeout";
        const detail = error instanceof Error ? error.message : "";
        showValueMatchError(timedOut ? "Loading timed out" : "Couldn’t load choices", detail, retry);
      } finally {
        window.clearTimeout(slowTimer);
        window.clearTimeout(deadline);
        if (isCurrent()) {
          valueMatchRequest = null;
          setPending(false);
          if (retryValueMatch) {
            refreshValueMatch.hidden = true;
            useValueMatches.disabled = applyWasDisabled;
          }
        }
      }
    };

    const retry = (action) => void openValueMatch(action.trigger, action);
    document.addEventListener("impodo:read-credential-saved", (event) => {
      if (!readCredentialRetry || !valueMatchDialog.open) return;
      event.preventDefault();
      const action = readCredentialRetry;
      readCredentialRetry = null;
      window.requestAnimationFrame(() => {
        if (valueMatchDialog.open && retryValueMatch === action) retry(action);
      });
    });
    credentialDialog?.addEventListener("close", () => { readCredentialRetry = null; });

    retryButton.addEventListener("click", () => {
      if (retryValueMatch) retry(retryValueMatch);
    });

    mappingForm.addEventListener("click", (event) => {
      const trigger = event.target.closest?.("[data-open-value-match]");
      if (!trigger || !mappingForm.contains(trigger)) {
        return;
      }
      void openValueMatch(trigger);
    });

    refreshValueMatch?.addEventListener("click", () => {
      const trigger = activeValueMatch?.trigger;
      if (!trigger) {
        return;
      }
      void openValueMatch(trigger, { refresh: true });
    });

    useValueMatches?.addEventListener("click", () => {
      if (!activeValueMatch || valueMatchRequest || !valueMatchRows) {
        return;
      }
      const mappings = Array.from(
        valueMatchRows.querySelectorAll("select[data-source-value]")
      )
        .filter((select) => select.value)
        .map((select) => select.value === activeValueMatch.blankChoice
          ? { source_value: select.dataset.sourceValue || "", action: "leave_blank" }
          : { source_value: select.dataset.sourceValue || "", target_value: select.value });
      activeValueMatch.storage.value = JSON.stringify(mappings);
      blankCreate?.save(activeValueMatch);
      if (activeValueMatch.selectionRefresh) {
        stagedSelectionChoices.set(`${activeValueMatch.trigger.dataset.targetModel}.${activeValueMatch.trigger.dataset.targetField}`,
          activeValueMatch.selectionRefresh);
        selectionRefreshStorage.value = JSON.stringify([...stagedSelectionChoices.values()]);
      }
      const policy = activeValueMatch.storage
        .closest("[data-scalar-mapping-row], [data-relation-mapping-row]")
        ?.querySelector("[data-categorical-policy]");
      if (policy instanceof HTMLSelectElement && mappings.length) {
        policy.value =
          activeValueMatch.trigger.dataset.valueMatchKind === "scalar"
            ? "EXPLICIT_VALUE_MATCH"
            : "EXPLICIT_KEY_MATCH";
        policy.dispatchEvent(new Event("change", { bubbles: true }));
      }
      activeValueMatch.storage.dispatchEvent(
        new Event("input", { bubbles: true })
      );
      const summary = activeValueMatch.storage
        .closest("[data-scalar-mapping-row], [data-relation-mapping-row]")
        ?.querySelector("[data-value-match-summary]");
      if (summary) {
        const matched = activeValueMatch.matchedCount ?? mappings.length;
        const remaining = activeValueMatch.sourceCount - matched;
        summary.textContent = remaining
          ? `${matched} matched · ${remaining} still need review`
          : `All ${matched} populated source choice(s) matched`;
      }
      const trigger = activeValueMatch.trigger;
      activeValueMatch = null;
      valueMatchDialog?.close();
      trigger.focus();
    });

    for (const close of valueMatchDialog?.querySelectorAll(
      "[data-close-value-match]"
    ) || []) {
      close.addEventListener("click", () => valueMatchDialog?.close());
    }

    valueMatchDialog?.addEventListener("close", () => {
      if (valueMatchDialog.open) return;
      valueMatchRequest?.abort();
      valueMatchRequest = null;
      activeValueMatch = null;
      readCredentialRetry = null;
      retryValueMatch = null;
    });

    mappingForm.addEventListener("change", (event) => {
      const row = event.target?.closest?.(
        "[data-scalar-mapping-row], [data-relation-mapping-row]"
      );
      const storage = row?.querySelector("[data-value-match-storage]");
      if (!(storage instanceof HTMLInputElement)) {
        return;
      }
      const name = event.target?.name || "";
      const changesValueIdentity =
        name.startsWith("scalar_source_") ||
        name.startsWith("scalar_value_source_") ||
        name.startsWith("relation_source_") ||
        name.startsWith("relation_origin_") ||
        name.startsWith("relation_dataset_") ||
        name.startsWith("relation_key_");
      if (!changesValueIdentity) return;
      const prerequisite = row.querySelector("[data-value-match-prerequisite]");
      if (prerequisite) prerequisite.hidden = true;
      if (storage.value === "[]") return;
      storage.value = "[]";
      const summary = row.querySelector("[data-value-match-summary]");
      if (summary) {
        summary.textContent = "Use when source and Odoo choices differ";
      }
    });

    return {
      acceptSelectionRefreshes(operation) {
        window.impodoSelectionChoices.consumeAcceptedRefreshes(
          stagedSelectionChoices, selectionRefreshStorage, activeValueMatch, operation
        );
      },
    };
  };

  window.impodoMappingValueMatch = { create };
})();
