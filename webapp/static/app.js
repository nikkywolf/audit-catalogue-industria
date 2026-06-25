const state = {
  bootstrap: null,
  me: null,
  openProductId: null,
  openIgnoredId: null,
  productRows: [],
  productTotal: 0,
  ignoredRows: [],
  ignoredTotal: 0,
  activeBrands: [],
  ignoredBrands: [],
  activeBrandsTotal: 0,
  ignoredBrandsTotal: 0,
  batchCandidates: [],
  batchPending: [],
  batchSubmitted: [],
  batchCompleted: [],
};

const $ = (selector) => document.querySelector(selector);

function debounce(callback, delay = 250) {
  let timeoutId;
  return (...args) => {
    clearTimeout(timeoutId);
    timeoutId = setTimeout(() => callback(...args), delay);
  };
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

function setPage(pageId) {
  document.querySelectorAll(".page").forEach((page) => page.classList.remove("active"));
  document.querySelectorAll(".nav").forEach((button) => button.classList.remove("active"));
  $(`#${pageId}`).classList.add("active");
  document.querySelector(`[data-page="${pageId}"]`).classList.add("active");
}

function metric(label, value) {
  return `<div class="metric"><div class="label">${label}</div><div class="value">${value}</div></div>`;
}

function fillSelect(element, values, allLabel) {
  const selectedValue = element.value;
  element.innerHTML = `<option value="">${allLabel}</option>` +
    values.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
  if ([...element.options].some((option) => option.value === selectedValue)) {
    element.value = selectedValue;
  }
}

async function loadBootstrap() {
  const data = await api("/api/bootstrap");
  state.bootstrap = data;

  const me = await api("/api/me");
  state.me = me;
  $("#userBox").textContent = `Connecté : ${me.name} | Rôle : ${me.role}`;
  applyRoleVisibility();
  if (data.latest_audit) {
    $("#syncBox").textContent = `Dernier export : ${data.latest_audit.Date} | ${data.latest_audit.Produits} produits`;
  } else if (data.latest_sync) {
    $("#syncBox").textContent = `Dernière sync : ${data.latest_sync.status} ${data.latest_sync.finished_at || data.latest_sync.started_at}`;
  }

  const metrics = data.metrics;
  $("#metrics").innerHTML = [
    metric("Produits", metrics.products),
    metric("Conformes", metrics.conformes),
    metric("Action requise", metrics.action_required),
    metric("Critiques", metrics.critical),
    metric("Erreurs approuvées", metrics.approved_errors),
    metric("Produits e-com", metrics.ecom_products),
    metric("Produits à ignorer", metrics.ignored_products),
  ].join("");

  fillSelect($("#brandFilter"), data.processed_brands, "Toutes les marques");
  fillSelect($("#priorityFilter"), data.priorities, "Toutes priorités");
  fillSelect($("#correctionFilter"), data.correction_types, "Tous types");
  renderBrandSummary(data.brand_summary);
}

function isAdmin() {
  return state.me && state.me.role === "admin";
}

function applyRoleVisibility() {
  document.querySelectorAll(".admin-only").forEach((element) => {
    element.style.display = isAdmin() ? "" : "none";
  });
  if (!isAdmin() && document.querySelector("#gptBatch").classList.contains("active")) {
    setPage("overview");
  }
}

function renderBrandSummary(rows) {
  const columns = ["Brand", "Produits", "Score_moyen", "Conformes", "A_surveillance", "Action_requise", "Critiques", "% conformes", "% critiques"];
  $("#brandSummary").innerHTML = renderSimpleTable(columns, rows);
}

function renderSimpleTable(columns, rows) {
  return `
    <table>
      <thead><tr>${columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("")}</tr></thead>
      <tbody>
        ${rows.map((row) => `
          <tr>${columns.map((column) => `<td>${escapeHtml(row[column] ?? "")}</td>`).join("")}</tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

function productQuery() {
  const params = new URLSearchParams();
  params.set("search", $("#productSearch").value);
  params.set("brand", $("#brandFilter").value);
  params.set("priority", $("#priorityFilter").value);
  params.set("correction", $("#correctionFilter").value);
  params.set("approval", $("#approvalFilter").value);
  params.set("limit", $("#limitFilter").value);
  return params.toString();
}

async function loadProducts() {
  const data = await api(`/api/products?${productQuery()}`);
  state.productRows = data.items;
  state.productTotal = data.total;
  $("#productCount").textContent = `Produits affichés : ${data.total}`;
  renderProducts();
}

function renderProducts() {
  const rows = state.productRows;
  $("#productsTable").innerHTML = `
    <table>
      <thead>
        <tr>
          <th></th><th>Marque</th><th>Produit</th><th>SKU</th><th>UPC</th>
          <th class="num">Score</th><th>Priorité</th><th class="num">Rest.</th><th class="num">Appr.</th><th class="admin-only">GPT</th>
        </tr>
      </thead>
      <tbody>
        ${rows.map((row) => productRowHtml(row)).join("")}
      </tbody>
    </table>
  `;
  document.querySelectorAll("[data-toggle-product]").forEach((button) => {
    button.addEventListener("click", () => toggleProduct(button.dataset.toggleProduct));
  });
}

function productRowHtml(row) {
  const id = row.Internal_Variant_ID;
  const isOpen = state.openProductId === id;
  const productName = escapeHtml(row.FC_Title_Short);
  const productCell = row.Lightspeed_Admin_URL
    ? `<a class="product-link" href="${escapeHtml(row.Lightspeed_Admin_URL)}" target="_blank" rel="noopener noreferrer">${productName}</a>`
    : productName;
  const gptButton = isAdmin() && buildAutofillLightspeedUrl(row)
    ? `<a class="button-link" href="${escapeHtml(buildAutofillLightspeedUrl(row))}" target="_blank" rel="noopener noreferrer">Remplir avec GPT</a>`
    : "";
  return `
    <tr>
      <td><button class="toggle" data-toggle-product="${escapeHtml(id)}">${isOpen ? "▾" : "▸"}</button></td>
      <td>${escapeHtml(row.Brand)}</td>
      <td>${productCell}</td>
      <td>${escapeHtml(row.SKU)}</td>
      <td>${escapeHtml(row.UPC)}</td>
      <td class="num">${escapeHtml(row.Score)}</td>
      <td>${escapeHtml(row.Priorité)}</td>
      <td class="num">${escapeHtml(row["Erreurs restantes"])}</td>
      <td class="num">${escapeHtml(row["Erreurs approuvées"])}</td>
      <td class="admin-only">${gptButton}</td>
    </tr>
    ${isOpen ? `<tr class="details"><td colspan="${isAdmin() ? 10 : 9}" id="product-detail-${escapeHtml(id)}">Chargement...</td></tr>` : ""}
  `;
}

async function toggleProduct(id) {
  state.openProductId = state.openProductId === id ? null : id;
  renderProducts();
  if (state.openProductId) {
    const data = await api(`/api/products/${encodeURIComponent(id)}`);
    renderProductDetail(id, data);
  }
}

function renderProductDetail(id, data) {
  const container = $(`#product-detail-${CSS.escape(id)}`);
  if (!container) return;
  const unresolved = data.unresolved.map((item) => errorActionHtml(id, item, "approve")).join("");
  const approved = data.approved.map((item) => errorActionHtml(id, item, "remove")).join("");
  container.innerHTML = `
    <div class="detail-grid">
      <div>
        <strong>${escapeHtml(data.product.Brand)}</strong> — ${productDetailLink(data.product)}
        <span class="pill">SKU ${escapeHtml(data.product.SKU)}</span>
        <span class="pill">UPC ${escapeHtml(data.product.UPC)}</span>
      </div>
      ${data.product["Type de correction"] ? `<div class="muted">${escapeHtml(data.product["Type de correction"])}</div>` : ""}
      <div>
        <h4>À traiter</h4>
        ${unresolved || '<div class="ok">Aucune erreur restante.</div>'}
      </div>
      <div>
        <h4>Déjà approuvées</h4>
        ${approved || '<div class="muted">Aucune approbation.</div>'}
      </div>
    </div>
  `;
  container.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", () => handleErrorAction(button));
  });
}

function productDetailLink(product) {
  const title = escapeHtml(product.FC_Title_Short);
  if (!product.Lightspeed_Admin_URL) return title;
  return `<a class="product-link" href="${escapeHtml(product.Lightspeed_Admin_URL)}" target="_blank" rel="noopener noreferrer">${title}</a>`;
}

function buildAutofillLightspeedUrl(product) {
  if (!product.Lightspeed_Admin_URL || !product.Internal_Variant_ID) return "";
  const url = new URL(product.Lightspeed_Admin_URL);
  url.searchParams.set("industria_autofill", "1");
  url.searchParams.set("industria_variant", product.Internal_Variant_ID);
  url.searchParams.set("industria_api", window.location.origin);
  return url.toString();
}

function buildBatchAutofillLightspeedUrl(item) {
  if (!item.Lightspeed_Admin_URL || !item.Internal_Variant_ID) return "";
  const url = new URL(item.Lightspeed_Admin_URL);
  url.searchParams.set("industria_autofill", "batch");
  url.searchParams.set("industria_variant", item.Internal_Variant_ID);
  url.searchParams.set("industria_api", window.location.origin);
  return url.toString();
}

function errorActionHtml(id, item, action) {
  const label = action === "approve" ? "Approuver" : "Retirer";
  return `
    <div class="detail-row">
      <div><strong>${escapeHtml(item.type)}</strong> — ${escapeHtml(item.error)}</div>
      <button
        data-action="${action}"
        data-id="${escapeHtml(id)}"
        data-type="${escapeHtml(item.type)}"
        data-error="${escapeHtml(item.error)}"
      >${label}</button>
    </div>
  `;
}

async function handleErrorAction(button) {
  const body = JSON.stringify({
    variant_id: button.dataset.id,
    error_type: button.dataset.type,
    error: button.dataset.error,
  });
  if (button.dataset.action === "approve") {
    await api("/api/approvals", { method: "POST", body });
  } else {
    await api("/api/approvals", { method: "DELETE", body });
  }
  const id = button.dataset.id;
  await loadProducts();
  const isStillVisible = state.productRows.some((row) => row.Internal_Variant_ID === id);
  if (!isStillVisible) {
    state.openProductId = null;
  } else if (state.openProductId === id) {
    const data = await api(`/api/products/${encodeURIComponent(id)}`);
    renderProductDetail(id, data);
  }
  await loadIgnored();
  await loadBootstrap();
}

async function loadIgnored() {
  const search = encodeURIComponent($("#ignoredSearch").value);
  const data = await api(`/api/ignored?search=${search}&limit=100`);
  state.ignoredRows = data.items;
  state.ignoredTotal = data.total;
  renderIgnored();
}

async function reloadMainData() {
  state.openProductId = null;
  state.openIgnoredId = null;
  await loadBootstrap();
  await loadProducts();
  await loadIgnored();
  await loadBrandsAdmin();
}

function renderIgnored() {
  $("#ignoredTable").innerHTML = `
    <div class="muted">Produits ignorés : ${state.ignoredTotal}</div>
    <table>
      <thead><tr><th></th><th>Marque</th><th>Produit</th><th>SKU</th><th>UPC</th><th>Priorité</th></tr></thead>
      <tbody>${state.ignoredRows.map((row) => ignoredRowHtml(row)).join("")}</tbody>
    </table>
  `;
  document.querySelectorAll("[data-toggle-ignored]").forEach((button) => {
    button.addEventListener("click", () => toggleIgnored(button.dataset.toggleIgnored));
  });
}

function ignoredRowHtml(row) {
  const id = row.Internal_Variant_ID;
  const isOpen = state.openIgnoredId === id;
  return `
    <tr>
      <td><button class="toggle" data-toggle-ignored="${escapeHtml(id)}">${isOpen ? "▾" : "▸"}</button></td>
      <td>${escapeHtml(row.Brand)}</td>
      <td>${escapeHtml(row.FC_Title_Short)}</td>
      <td>${escapeHtml(row.SKU)}</td>
      <td>${escapeHtml(row.UPC)}</td>
      <td>${escapeHtml(row.Priorité)}</td>
    </tr>
    ${isOpen ? `
      <tr class="details">
        <td colspan="6">
          <div>${escapeHtml(row["Alertes catalogue"])}</div>
          <button data-restore-ignored="${escapeHtml(id)}">Rétablir ce produit</button>
        </td>
      </tr>
    ` : ""}
  `;
}

function toggleIgnored(id) {
  state.openIgnoredId = state.openIgnoredId === id ? null : id;
  renderIgnored();
  const button = document.querySelector(`[data-restore-ignored="${CSS.escape(id)}"]`);
  if (button) {
    button.addEventListener("click", async () => {
      await api(`/api/ignored/${encodeURIComponent(id)}/restore`, { method: "POST" });
      state.openIgnoredId = null;
      await loadIgnored();
      await loadBootstrap();
    });
  }
}

async function loadTodos() {
  const data = await api("/api/todos");
  renderTodos(data.items);
  document.querySelectorAll("[data-todo-status]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/api/todos/${button.dataset.todoId}`, {
        method: "PATCH",
        body: JSON.stringify({ statut: button.dataset.todoStatus }),
      });
      await loadTodos();
    });
  });
  document.querySelectorAll("[data-todo-delete]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/api/todos/${button.dataset.todoDelete}`, { method: "DELETE" });
      await loadTodos();
    });
  });
}

function renderTodos(items) {
  $("#todosList").innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Tâche</th><th>Assigné</th><th>Statut</th><th>Échéance</th><th>Description</th><th>Actions</th>
        </tr>
      </thead>
      <tbody>
        ${items.map(todoRowHtml).join("") || '<tr><td colspan="6" class="muted">Aucune tâche.</td></tr>'}
      </tbody>
    </table>
  `;
}

function todoRowHtml(todo) {
  const statusClass = cleanStatus(todo.Statut);
  return `
    <tr>
      <td><strong>${escapeHtml(todo.Tache)}</strong></td>
      <td>${escapeHtml(todo.Assigne)}</td>
      <td><span class="status ${statusClass}">${escapeHtml(todo.Statut)}</span></td>
      <td>${escapeHtml(todo.Date_echeance)}</td>
      <td class="todo-description">${escapeHtml(todo.Description)}</td>
      <td>
        <div class="row-actions">
          <button data-todo-id="${todo.ID}" data-todo-status="En cours">En cours</button>
          <button data-todo-id="${todo.ID}" data-todo-status="Terminé">Terminer</button>
          <button class="danger-button" data-todo-delete="${todo.ID}">Supprimer</button>
        </div>
      </td>
    </tr>
  `;
}

function cleanStatus(status) {
  return String(status || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "").replaceAll(" ", "-");
}

async function loadBrandsAdmin() {
  const search = encodeURIComponent($("#brandAdminSearch").value);
  const data = await api(`/api/brands?search=${search}`);
  state.activeBrands = data.active;
  state.ignoredBrands = data.ignored;
  state.activeBrandsTotal = data.active_total;
  state.ignoredBrandsTotal = data.ignored_total;
  renderBrandsAdmin();
}

function renderBrandsAdmin() {
  $("#activeBrandsTable").innerHTML = brandTableHtml(
    state.activeBrands,
    `Marques actives : ${state.activeBrandsTotal}`,
    "Ignorer",
    "ignore-brand"
  );
  $("#ignoredBrandsTable").innerHTML = brandTableHtml(
    state.ignoredBrands,
    `Marques ignorées : ${state.ignoredBrandsTotal}`,
    "Rétablir",
    "restore-brand"
  );
  document.querySelectorAll("[data-ignore-brand]").forEach((button) => {
    button.addEventListener("click", () => updateBrand("ignore", button.dataset.ignoreBrand));
  });
  document.querySelectorAll("[data-restore-brand]").forEach((button) => {
    button.addEventListener("click", () => updateBrand("restore", button.dataset.restoreBrand));
  });
}

function brandTableHtml(rows, countLabel, buttonLabel, dataName) {
  return `
    <div class="muted table-count">${escapeHtml(countLabel)}</div>
    <table>
      <thead><tr><th>Marque</th><th class="num">Produits</th><th>Action</th></tr></thead>
      <tbody>
        ${rows.map((row) => `
          <tr>
            <td><strong>${escapeHtml(row.Brand)}</strong></td>
            <td class="num">${escapeHtml(row.Produits)}</td>
            <td><button data-${dataName}="${escapeHtml(row.Brand)}">${escapeHtml(buttonLabel)}</button></td>
          </tr>
        `).join("") || '<tr><td colspan="3" class="muted">Aucune marque.</td></tr>'}
      </tbody>
    </table>
  `;
}

async function updateBrand(action, brand) {
  const actionText = action === "ignore" ? "ignorer" : "rétablir";
  const ok = window.confirm(`Confirmer: ${actionText} toute la marque ${brand}?`);
  if (!ok) return;
  await api(`/api/brands/${action}`, {
    method: "POST",
    body: JSON.stringify({ brand }),
  });
  await reloadMainData();
}

async function loadBatchCandidates() {
  const search = encodeURIComponent($("#batchCandidateSearch").value);
  const data = await api(`/api/gpt-batches/candidates?search=${search}&limit=100`);
  state.batchCandidates = data.items;
  $("#batchCandidatesTable").innerHTML = `
    <div class="muted table-count">Produits admissibles : ${data.total}</div>
    <table>
      <thead><tr><th>Marque</th><th>Produit</th><th>SKU</th><th>Correction</th></tr></thead>
      <tbody>
        ${data.items.map((item) => `
          <tr>
            <td>${escapeHtml(item.Brand)}</td>
            <td>${escapeHtml(item.Product_Title)}</td>
            <td>${escapeHtml(item.SKU)}</td>
            <td>${escapeHtml(item["Type de correction"])}</td>
          </tr>
        `).join("") || '<tr><td colspan="4" class="muted">Aucun candidat.</td></tr>'}
      </tbody>
    </table>
  `;
}

async function loadBatchPending() {
  const data = await api("/api/gpt-batches/items?status=pending&limit=200");
  state.batchPending = data.items;
  $("#batchPendingTable").innerHTML = batchItemsTableHtml(
    state.batchPending,
    `En attente : ${data.total}`,
    false
  );
}

async function loadBatchSubmitted() {
  const data = await api("/api/gpt-batches/items?status=submitted&limit=200");
  state.batchSubmitted = data.items;
  $("#batchSubmittedTable").innerHTML = batchItemsTableHtml(
    state.batchSubmitted,
    `Envoyés à OpenAI : ${data.total}`,
    false,
    true
  );
  const selectAll = document.querySelector("[data-reset-batch-select-all]");
  if (selectAll) {
    selectAll.addEventListener("change", () => {
      document.querySelectorAll("[data-reset-batch-item]").forEach((checkbox) => {
        checkbox.checked = selectAll.checked;
      });
    });
  }
}

async function loadBatchCompleted() {
  const search = encodeURIComponent($("#batchCompletedSearch").value);
  const data = await api(`/api/gpt-batches/items?status=completed&search=${search}&limit=200`);
  state.batchCompleted = data.items;
  $("#batchCompletedTable").innerHTML = batchItemsTableHtml(data.items, `Terminés : ${data.total}`, true);
  document.querySelectorAll("[data-approve-batch]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/api/gpt-batches/items/${encodeURIComponent(button.dataset.approveBatch)}/approve`, { method: "POST" });
      await loadBatchCompleted();
      await loadBatchApproved();
    });
  });
}

async function loadBatchApproved() {
  const search = encodeURIComponent($("#batchApprovedSearch").value);
  const data = await api(`/api/gpt-batches/items?status=approved&search=${search}&limit=200`);
  state.batchApproved = data.items;
  $("#batchApprovedTable").innerHTML = batchItemsTableHtml(data.items, `Approuvés : ${data.total}`, false, false, true);
  document.querySelectorAll("[data-restore-batch]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/api/gpt-batches/items/${encodeURIComponent(button.dataset.restoreBatch)}/restore`, { method: "POST" });
      await loadBatchCompleted();
      await loadBatchApproved();
    });
  });
}

function batchItemsTableHtml(items, countLabel, withApprove, withSelection = false, withRestore = false) {
  const selectionHeader = withSelection ? '<th><input type="checkbox" data-reset-batch-select-all /></th>' : "";
  return `
    <div class="muted table-count">${escapeHtml(countLabel)}</div>
    <table>
      <thead><tr>${selectionHeader}<th>Marque</th><th>Produit</th><th>SKU</th><th>Statut</th><th>Action</th></tr></thead>
      <tbody>
        ${items.map((item) => {
          const url = withApprove ? buildBatchAutofillLightspeedUrl(item) : "";
          const selectionCell = withSelection
            ? `<td><input type="checkbox" data-reset-batch-item="${escapeHtml(item.Internal_Variant_ID)}" /></td>`
            : "";
          return `
            <tr>
              ${selectionCell}
              <td>${escapeHtml(item.Brand)}</td>
              <td>${escapeHtml(item.Product_Title)}</td>
              <td>${escapeHtml(item.SKU)}</td>
              <td><span class="status">${escapeHtml(item.status)}</span></td>
              <td>
                ${withApprove && url
                  ? `<a class="button-link" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" data-approve-batch="${escapeHtml(item.Internal_Variant_ID)}">Approuver</a>`
                  : withRestore
                    ? `<button type="button" data-restore-batch="${escapeHtml(item.Internal_Variant_ID)}">Rétablir</button>`
                    : escapeHtml(item.batch_id || "")}
              </td>
            </tr>
          `;
        }).join("") || `<tr><td colspan="${withSelection ? 6 : 5}" class="muted">Aucun produit.</td></tr>`}
      </tbody>
    </table>
  `;
}

async function loadGptBatchPage() {
  await loadBatchCandidates();
  await loadBatchPending();
  await loadBatchSubmitted();
  await loadBatchCompleted();
  await loadBatchApproved();
}

async function setup() {
  document.querySelectorAll(".nav").forEach((button) => {
    button.addEventListener("click", () => setPage(button.dataset.page));
  });

  await loadBootstrap();
  await loadProducts();
  await loadIgnored();
  await loadTodos();
  await loadBrandsAdmin();
  if (isAdmin()) {
    await loadGptBatchPage();
  }

  const reloadProducts = debounce(() => {
    state.openProductId = null;
    loadProducts();
  });
  ["productSearch", "brandFilter", "priorityFilter", "correctionFilter", "approvalFilter", "limitFilter"].forEach((id) => {
    $(`#${id}`).addEventListener("input", () => {
      reloadProducts();
    });
  });

  const reloadIgnored = debounce(() => {
    state.openIgnoredId = null;
    loadIgnored();
  });
  $("#ignoredSearch").addEventListener("input", () => {
    reloadIgnored();
  });

  const reloadBrands = debounce(() => loadBrandsAdmin());
  $("#brandAdminSearch").addEventListener("input", () => {
    reloadBrands();
  });

  if (isAdmin()) {
    const reloadBatchCandidates = debounce(() => loadBatchCandidates());
    $("#batchCandidateSearch").addEventListener("input", () => reloadBatchCandidates());
    const reloadBatchCompleted = debounce(() => loadBatchCompleted());
    $("#batchCompletedSearch").addEventListener("input", () => reloadBatchCompleted());
    const reloadBatchApproved = debounce(() => loadBatchApproved());
    $("#batchApprovedSearch").addEventListener("input", () => reloadBatchApproved());
    $("#queueBatchCandidates").addEventListener("click", async () => {
      await api("/api/gpt-batches/queue", {
        method: "POST",
        body: JSON.stringify({ variant_ids: state.batchCandidates.slice(0, 50).map((item) => item.Internal_Variant_ID), limit: 50 }),
      });
      await loadGptBatchPage();
    });
    $("#submitGptBatch").addEventListener("click", async () => {
      const ok = window.confirm("Envoyer les produits en attente à l'API OpenAI Batch?");
      if (!ok) return;
      const result = await api("/api/gpt-batches/submit", {
        method: "POST",
        body: JSON.stringify({ limit: 50 }),
      });
      if (result.batch_id) {
        window.alert(`${result.count} produits envoyés à OpenAI.`);
      } else if (result.message) {
        window.alert(result.message);
      }
      await loadGptBatchPage();
    });
    $("#clearPendingBatch").addEventListener("click", async () => {
      const ok = window.confirm("Vider tous les produits en attente? Les batchs déjà envoyés à OpenAI ne seront pas touchés.");
      if (!ok) return;
      await api("/api/gpt-batches/pending", { method: "DELETE" });
      await loadGptBatchPage();
    });
    $("#syncGptBatch").addEventListener("click", async () => {
      const result = await api("/api/gpt-batches/sync", { method: "POST" });
      window.alert(`${result.synced || 0} résultat(s) récupéré(s).`);
      await loadGptBatchPage();
    });
    $("#resetSubmittedBatch").addEventListener("click", async () => {
      const selectedIds = [...document.querySelectorAll("[data-reset-batch-item]:checked")].map((input) => input.dataset.resetBatchItem);
      if (selectedIds.length === 0) {
        window.alert("Sélectionne au moins un produit envoyé à réinitialiser.");
        return;
      }
      const ok = window.confirm(`Réinitialiser ${selectedIds.length} produit(s) envoyé(s) vers la table en attente?`);
      if (!ok) return;
      const result = await api("/api/gpt-batches/reset", {
        method: "POST",
        body: JSON.stringify({ variant_ids: selectedIds }),
      });
      window.alert(`${result.updated} produit(s) réinitialisé(s).`);
      await loadGptBatchPage();
    });
  }

  $("#todoForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const formData = new FormData(event.target);
    await api("/api/todos", {
      method: "POST",
      body: JSON.stringify(Object.fromEntries(formData.entries())),
    });
    event.target.reset();
    await loadTodos();
  });
}

setup().catch((error) => {
  document.body.innerHTML = `<pre>${escapeHtml(error.stack || error.message)}</pre>`;
});
