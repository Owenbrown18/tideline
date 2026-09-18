// Tideline's one script. Every page works without it; this adds three things.
(() => {
  // 1. The header tucks away while scrolling down and slides back on the way up.
  const header = document.querySelector(".top");
  if (header) {
    let last = window.scrollY;
    let queued = false;
    const update = () => {
      const y = window.scrollY;
      header.classList.toggle("is-scrolled", y > 8);
      if (y > header.offsetHeight + 40 && y > last + 4) header.classList.add("is-hidden");
      else if (y < last - 4 || y <= header.offsetHeight) header.classList.remove("is-hidden");
      last = y;
      queued = false;
    };
    window.addEventListener("scroll", () => {
      if (!queued) { queued = true; requestAnimationFrame(update); }
    }, { passive: true });
    // Tabbing into the header brings it back, so keyboard users never lose the nav.
    header.addEventListener("focusin", () => header.classList.remove("is-hidden"));
  }

  // 2. A whole row opens its page, not only the name in it.
  document.addEventListener("click", (event) => {
    const row = event.target.closest("[data-href]");
    if (!row || event.target.closest("a, button, input, form")) return;
    if (String(window.getSelection())) return; // selecting text, not clicking
    if (event.metaKey || event.ctrlKey) window.open(row.dataset.href, "_blank");
    else window.location.href = row.dataset.href;
  });

  // 3. Reports open in a dialog, ready to save as a PDF.
  const dialog = document.getElementById("report-dialog");
  if (!dialog || typeof dialog.showModal !== "function") return;
  const frame = dialog.querySelector("iframe");
  const title = dialog.querySelector("[data-title]");
  const download = dialog.querySelector("[data-download]");
  const newTab = dialog.querySelector("[data-newtab]");
  const print = dialog.querySelector("[data-print]");

  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-report]");
    if (!link || event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault();
    const url = link.getAttribute("href");
    title.textContent = link.dataset.report;
    download.href = url + "?download=1";
    newTab.href = url;
    print.disabled = true;
    frame.src = url;
    dialog.showModal();
  });
  frame.addEventListener("load", () => { print.disabled = frame.src === "about:blank"; });
  print.addEventListener("click", () => frame.contentWindow.print());
  dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
  // A click on the dimmed backdrop closes it, like Escape does.
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => { frame.src = "about:blank"; });
})();
