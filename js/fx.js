// VIRALX FX — glitch support only. The sparkle field and tap bursts were
// removed 2026-11-14 on Keyz's order (the gleam was not the right look).
(function () {
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  // Glitch: feed the CSS its data-text, then fire random micro-glitches.
  document.querySelectorAll(".vx-logo, h1.chrome").forEach(function (el) {
    el.setAttribute("data-text", el.textContent.trim());
  });
  (function glitchLoop() {
    setTimeout(function () {
      document.documentElement.classList.add("glitching");
      setTimeout(function () {
        document.documentElement.classList.remove("glitching");
        glitchLoop();
      }, 150);
    }, 4200 + Math.random() * 5200);
  })();
})();
