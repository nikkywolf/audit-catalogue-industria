 (function () {
   if (document.getElementById("industria-sync-button")) return;

   function wait(ms) {
     return new Promise(resolve => setTimeout(resolve, ms));
   }

   function findByText(selector, text) {
     return [...document.querySelectorAll(selector)].find(el =>
       el.innerText && el.innerText.trim().includes(text)
     );
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

   function createButton() {
     const button = document.createElement("button");
     button.id = "industria-sync-button";
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

     button.onclick = async () => {
       button.innerText = "⏳ Création export...";
       button.disabled = true;

       try {
         const exportButton = findByText("a,button", "Nouvelle exportation");

         if (!exportButton) {
           alert("Bouton Nouvelle exportation introuvable.");
           button.innerText = "🔄 Sync Produits";
           button.disabled = false;
           return;
         }

         exportButton.click();
         await wait(1500);

         const dropdowns = [...document.querySelectorAll("select")];

         if (dropdowns.length > 0) {
           const select = dropdowns[0];
           const option = [...select.options].find(opt =>
             opt.innerText.trim().includes("Produits")
           );

           if (!option) {
             alert("Option Produits introuvable.");
             button.innerText = "🔄 Sync Produits";
             button.disabled = false;
             return;
           }

           select.value = option.value;
           select.dispatchEvent(new Event("change", { bubbles: true }));
         } else {
           const productOption = findByText("button,div,a,span", "Produits");

           if (productOption) {
             productOption.click();
           } else {
             alert("Menu Produits introuvable.");
             button.innerText = "🔄 Sync Produits";
             button.disabled = false;
             return;
           }
         }

         await wait(1000);

         const createExportButton =
           findByText("a,button", "Créer") ||
           findByText("a,button", "Exporter") ||
           findByText("a,button", "Démarrer");

         if (!createExportButton) {
           alert("Bouton Créer/Exporter introuvable.");
           button.innerText = "🔄 Sync Produits";
           button.disabled = false;
           return;
         }

         createExportButton.click();

         button.innerText = "⏳ Attente export 0/180s...";

         let downloadStarted = false;

         for (let i = 1; i <= 36; i++) {
           await wait(5000);

           button.innerText = `⏳ Attente export ${i * 5}/180s...`;

           const rows = [...document.querySelectorAll("tbody tr")];

           if (rows.length > 0) {
             const firstRow = rows[0];
             const rowText = firstRow.innerText;

             const isProductExport = rowText.includes("Produits");
             const isFinished = rowText.includes("TERMINÉ");

             if (isProductExport && isFinished) {
               const downloadLink = [...firstRow.querySelectorAll("a,button")].find(el =>
                 el.innerText && el.innerText.includes("Télécharger")
               );

               if (downloadLink) {
                 const url = downloadLink.href;

                 if (!url) {
                   alert("Lien de téléchargement introuvable. Le bouton existe, mais pas d'URL.");
                   button.innerText = "⚠️ URL manquante";
                   button.disabled = false;
                   return;
                 }

                 const filename = safeFileNameFromUrl(url);

                 chrome.runtime.sendMessage(
                   {
                     action: "downloadExport",
                     url: url,
                     filename: filename
                   },
                   response => {
                     if (chrome.runtime.lastError) {
                       console.error(chrome.runtime.lastError);
                       alert("Erreur Chrome download : " + chrome.runtime.lastError.message);
                     }
                   }
                 );

                 downloadStarted = true;
                 break;
               }
             }
           }
         }

         if (downloadStarted) {
           button.innerText = "✅ Export téléchargé";
           alert("Export Produits téléchargé dans Téléchargements/audit-catalogue-industria ✅");
         } else {
           button.innerText = "⚠️ Export non terminé";
           alert("L'export n'a pas été détecté comme terminé après 3 minutes.");
         }

       } catch (error) {
         console.error(error);
         alert("Erreur pendant la synchronisation. Regarde la console.");
         button.innerText = "🔄 Sync Produits";
       }

       button.disabled = false;
     };

     document.body.appendChild(button);
   }

   setTimeout(createButton, 1500);
 })();
