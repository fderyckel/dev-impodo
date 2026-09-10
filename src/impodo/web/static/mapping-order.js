document.addEventListener("DOMContentLoaded", () => {
  const liveCheck = document.querySelector("[data-matching-order-check]");
  if (liveCheck) {
    const statusUrl = liveCheck.dataset.statusUrl;
    const message = liveCheck.querySelector("[data-matching-order-check-message]");
    const progress = liveCheck.querySelector("[data-matching-order-check-progress]");
    const poll = async () => {
      try {
        const response = await fetch(statusUrl, {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (!response.ok) {
          throw new Error("status unavailable");
        }
        const state = await response.json();
        if (message) {
          message.textContent = state.failure_message || state.message;
        }
        if (progress) {
          progress.value = state.progress_percent;
          progress.textContent = `${state.progress_percent}%`;
        }
        if (state.terminal) {
          window.location.reload();
          return;
        }
      } catch (_error) {
        if (message) {
          message.textContent = "The status is temporarily unavailable. Impodo is keeping the previous suggestion.";
        }
      }
      window.setTimeout(poll, 750);
    };
    window.setTimeout(poll, 250);
  }

  const form = document.querySelector("[data-matching-order-form]");
  if (!form) {
    return;
  }

  const toggle = form.querySelector("[data-matching-order-toggle]");
  const toggleLabel = form.querySelector("[data-matching-order-toggle-label]");
  const list = form.querySelector("[data-matching-order-list]");
  const announcement = form.querySelector("[data-matching-order-announcement]");
  if (!toggle || !toggleLabel || !list) {
    return;
  }

  let originalOrder = [];
  let draggedRow = null;

  const rows = () => Array.from(
    list.querySelectorAll("[data-matching-order-row]")
  );

  const updatePositions = () => {
    const currentRows = rows();
    currentRows.forEach((row, index) => {
      const position = row.querySelector("[data-matching-order-position]");
      const moveUp = row.querySelector('button[value^="move_up:"]');
      const moveDown = row.querySelector('button[value^="move_down:"]');
      if (position) {
        position.textContent = String(index + 1);
      }
      if (moveUp) {
        moveUp.disabled = index === 0;
      }
      if (moveDown) {
        moveDown.disabled = index === currentRows.length - 1;
      }
    });
  };

  const restoreOriginalOrder = () => {
    const byId = new Map(
      rows().map((row) => [row.dataset.matchingOrderRow, row])
    );
    originalOrder.forEach((datasetId) => {
      const row = byId.get(datasetId);
      if (row) {
        list.append(row);
      }
    });
    updatePositions();
  };

  const setEditing = (editing) => {
    toggleLabel.textContent = editing ? "Cancel reordering" : "Reorder tables";
    rows().forEach((row) => {
      row.draggable = editing;
    });
    if (!editing) {
      restoreOriginalOrder();
    }
  };

  toggle.addEventListener("change", () => {
    if (toggle.checked) {
      originalOrder = rows().map((row) => row.dataset.matchingOrderRow || "");
    }
    setEditing(toggle.checked);
  });

  list.addEventListener("dragstart", (event) => {
    const row = event.target.closest("[data-matching-order-row]");
    if (!toggle.checked || !row) {
      event.preventDefault();
      return;
    }
    draggedRow = row;
    row.classList.add("dragging");
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", row.dataset.matchingOrderRow || "");
    }
  });

  list.addEventListener("dragover", (event) => {
    if (!draggedRow) {
      return;
    }
    event.preventDefault();
    const target = event.target.closest("[data-matching-order-row]");
    if (!target || target === draggedRow) {
      return;
    }
    const rectangle = target.getBoundingClientRect();
    const insertAfter = event.clientY > rectangle.top + rectangle.height / 2;
    list.insertBefore(draggedRow, insertAfter ? target.nextSibling : target);
    updatePositions();
  });

  list.addEventListener("drop", (event) => {
    if (!draggedRow) {
      return;
    }
    event.preventDefault();
    updatePositions();
    if (announcement) {
      const name = draggedRow.querySelector("strong")?.textContent?.trim() || "Table";
      const position = rows().indexOf(draggedRow) + 1;
      announcement.textContent = `${name} moved to position ${position}. Save the table order to keep this change.`;
    }
  });

  list.addEventListener("dragend", () => {
    draggedRow?.classList.remove("dragging");
    draggedRow = null;
  });

  setEditing(false);
});
