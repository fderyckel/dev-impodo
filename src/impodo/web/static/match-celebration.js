(() => {
  "use strict";

  const celebration = document.querySelector("[data-match-celebration]");
  if (!celebration) return;
  if (document.visibilityState === "hidden") {
    celebration.remove();
    return;
  }

  const confetti = celebration.querySelector("[data-match-confetti]");
  const dismissButton = celebration.querySelector("[data-match-celebration-dismiss]");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let finished = false;
  let timer;

  const dismiss = () => {
    if (finished) return;
    finished = true;
    window.clearTimeout(timer);
    if (celebration.contains(document.activeElement)) {
      document.querySelector("[data-prepare-data]")?.focus({ preventScroll: true });
    }
    celebration.remove();
    document.removeEventListener("keydown", onKeydown);
    document.removeEventListener("visibilitychange", onVisibilityChange);
    window.removeEventListener("pagehide", dismiss);
  };
  const onKeydown = (event) => {
    if (event.key === "Escape") dismiss();
  };
  const onVisibilityChange = () => {
    if (document.visibilityState === "hidden") dismiss();
  };

  if (!reducedMotion.matches) {
    const pieces = document.createDocumentFragment();
    for (let index = 0; index < 150; index += 1) {
      const piece = document.createElement("span");
      piece.className = "match-celebration-piece";
      const random = (salt) => (
        ((index * 67 + salt * 31 + index * index * 7) % 997) / 997
      );
      piece.style.setProperty("--piece-x", `${random(1) * 100}%`);
      piece.style.setProperty("--piece-width", `${5 + random(2) * 6}px`);
      piece.style.setProperty("--piece-height", `${index % 4 === 0 ? 24 : 8 + random(3) * 7}px`);
      piece.style.setProperty("--piece-radius", index % 5 === 0 ? "50%" : "1px");
      piece.style.setProperty("--piece-colour", `var(--celebration-colour-${index % 5})`);
      piece.style.setProperty("--piece-drift", `${(random(6) - 0.5) * 180}px`);
      piece.style.setProperty("--piece-spin", `${(random(7) - 0.5) * 1800}deg`);
      piece.style.setProperty("--piece-duration", `${2200 + random(9) * 1100}ms`);
      piece.style.setProperty("--piece-delay", `${random(10) * 1300}ms`);
      pieces.appendChild(piece);
    }
    confetti.appendChild(pieces);
  }

  dismissButton.addEventListener("click", dismiss);
  document.addEventListener("keydown", onKeydown);
  document.addEventListener("visibilitychange", onVisibilityChange);
  window.addEventListener("pagehide", dismiss);
  celebration.hidden = false;
  timer = window.setTimeout(dismiss, 5000);
})();
