(function () {
  const BUTTON_ID = "industria-sync-button";

  function normalizeText(value) {
    return String(value || "")
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/\s+/g, " ")
      .trim()
      .toLowerCase();
  }

  function isVisible(element) {
    if (!element) return false;
    const rect = element.getBoundingClientRect();
    const style = window.getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  }

  function safeFileNameFromUrl(url) {
    try {
      const cleanUrl = new URL(url);
      const pathParts = cleanUrl.pathname.split("/");
      const lastPart = pathParts[pathParts.length - 1];

      if (lastPart && lastPart.includes(".")) {
        return decodeURIComponent(lastPart);
      }
    } catch (e) {}

    const now = new Date();
    const stamp = now.toISOString().replace(/[:.]/g, "-");
    return `products_export_${stamp}.zip`;
  }

  function findLatestFinishedProductsExport() {
    const rows = [...document.querySelectorAll("tbody tr")];
    return rows.find(row => {
      const text = normalizeText(row.innerText);
      return text.includes("produits") && (
        text.includes("termine") ||
        text.includes("completed") ||
        text.includes("finished")
      );
    });
  }

  function findDownloadUrl(row) {
    const link = [...row.querySelectorAll("a")].find(element =>
      isVisible(element) && normalizeText(element.innerText || element.textContent).includes("telecharger")
    );
    return link ? link.href : "";
  }

  function downloadLatestExport(button) {
    const row = findLatestFinishedProductsExport();
    if (!row) {
      alert("Aucun export Produits terminé trouvé. Crée l'export manuellement, attends qu'il soit TERMINÉ, puis reclique sur Sync Produits.");
      return;
    }

    const url = findDownloadUrl(row);
    if (!url) {
      alert("Export Produits trouvé, mais lien Télécharger introuvable.");
      return;
    }

    button.disabled = true;
    button.innerText = "⬇️ Téléchargement...";

    chrome.runtime.sendMessage(
      {
        action: "downloadExport",
        url: url,
        filename: safeFileNameFromUrl(url)
      },
      () => {
        button.disabled = false;
        button.innerText = "✅ Export téléchargé";

        if (chrome.runtime.lastError) {
          console.error(chrome.runtime.lastError);
          alert("Erreur Chrome download : " + chrome.runtime.lastError.message);
          button.innerText = "🔄 Sync Produits";
          return;
        }

        alert("Export Produits téléchargé. Le watcher va lancer l'audit automatiquement.");
      }
    );
  }

  function createButton() {
    if (document.getElementById(BUTTON_ID) || !document.body) return;

    const button = document.createElement("button");
    button.id = BUTTON_ID;
    button.innerText = "🔄 Sync Produits";

    Object.assign(button.style, {
      position: "fixed",
      top: "90px",
      right: "30px",
      zIndex: "999999",
      padding: "12px 18px",
      background: "#111827",
      color: "white",
      border: "none",
      borderRadius: "8px",
      fontSize: "15px",
      fontWeight: "700",
      cursor: "pointer"
    });

    button.onclick = () => downloadLatestExport(button);
    document.body.appendChild(button);
  }

  if (location.pathname.startsWith("/admin/exports")) {
    setTimeout(createButton, 1500);
  }
})();
