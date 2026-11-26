document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll(".chip[data-prompt]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const q = document.getElementById("q");
      if (q) {
        q.value = btn.getAttribute("data-prompt") || "";
        q.focus();
      }
    });
  });

  const copyBtn = document.getElementById("copyBtn");
  if (copyBtn) {
    copyBtn.addEventListener("click", function () {
      const el = document.getElementById("sqlOut");
      if (!el || !navigator.clipboard) return;
      navigator.clipboard.writeText(el.textContent).then(function () {
        copyBtn.textContent = "Copied!";
        setTimeout(function () {
          copyBtn.textContent = "Copy SQL";
        }, 1800);
      });
    });
  }
});
