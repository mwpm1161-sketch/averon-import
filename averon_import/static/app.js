const state = {
  config: null,
  document: null,
  selectedPages: new Set(),
  previewPage: null,
  crop: null,
  cropSelecting: false,
  rows: [],
  result: null,
  activeRowId: null,
  zoom: 1,
  dirty: false,
  exportOrder: [],
  exportSelected: new Set(),
  ocrMode: "standard",
  ocrHealth: null,
  aiProvider: "off",
  reviewFilter: "",
  settings: null,
  sourcing: {row: null, result: null, projectFilter: "all"},
  sourcingHealth: null,
  currentUser: null,
  recentDocuments: [],
  manual: {active: false, rows: []},
};

const CRITICAL_FIELDS = ["quantity", "unit", "mass"];
const CRITICAL_LABELS = {quantity:"Количество", unit:"Единица", mass:"Масса"};
const MANUAL_DRAFT_KEY = "averonManualTenderDraft";
const MANUAL_FIELDS = ["name", "type_mark", "manufacturer", "code", "quantity", "unit"];

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function toast(message, type = "") {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.textContent = message;
  $("#toast-root").appendChild(node);
  setTimeout(() => node.remove(), 4200);
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      message = data.detail || message;
    } catch (_) {}
    throw new Error(message);
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response;
}

function setView(name) {
  $$(".view").forEach((view) => view.classList.remove("visible"));
  $(`#${name}-view`).classList.add("visible");
  document.body.classList.toggle("manual-mode", name === "manual");
  const stepMap = {upload:"upload", pages:"pages", processing:"recognition", review:"review"};
  const step = stepMap[name] || name;
  $$(".step").forEach((button) => button.classList.toggle("active", button.dataset.step === step));
  const titles = {
    upload: ["Импорт спецификации", "Перенос таблиц из PDF в редактируемый Excel"],
    pages: ["Выбор страниц", "Укажите, какие таблицы необходимо распознать"],
    processing: ["Распознавание документа", "Определение строк, столбцов и текста"],
    review: ["Проверка данных", "Сравните результат с PDF и подготовьте экспорт"],
    manual: ["Тендер без спецификации", "Введите позиции и подберите предложения в общем sourcing-процессе"],
  };
  const [title, subtitle] = titles[name] || titles.upload;
  $("#page-title").textContent = title;
  $("#page-subtitle").textContent = subtitle;
}

function bytes(value) {
  if (!Number.isFinite(value)) return "";
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} КБ`;
  return `${(value / 1024 / 1024).toFixed(1)} МБ`;
}

async function boot() {
  try {
    const currentUser = await api("/api/me");
    state.currentUser = currentUser;
    const isAdmin = String(currentUser?.role || "").toLowerCase() === "admin";
    $("#settings-button").hidden = !isAdmin;
    const [config, health] = await Promise.all([
      api("/api/config"),
      api("/api/health"),
    ]);
    state.config = config;
    state.sourcingHealth = health.sourcing || null;
    const ocr = health.cloud_ocr;
    state.ocrHealth = ocr;
    updateCloudStatus();
    if (isAdmin) {
      state.settings = await api("/api/settings");
      initializeSettings(state.settings);
    } else {
      state.settings = null;
    }
    populateFilters();
    initializeExportColumns();
    loadManualDraft();
    try { await loadRecentDocuments(); } catch (_) { state.recentDocuments = []; renderRecentDocuments(); }
    await resumeLastDocument();
    if (state.manual.active) openManualWorkspace();
  } catch (error) {
    toast(error.message, "error");
  }
}

function updateCloudStatus() {
  const ocr = state.ocrHealth || {};
  const ready = Boolean(ocr.available);
  $("#ocr-dot").classList.toggle("ok", ready);
  $("#ocr-dot").classList.toggle("bad", !ready);
  $("#ocr-title").textContent = ready ? "Yandex Vision готов" : "Yandex Vision не настроен";
  $("#ocr-text").textContent = ready ? "Облачный OCR · подключено" : "Для распознавания настройте Yandex Cloud";
  const status = $("#yandex-ocr-status");
  status.textContent = ready
    ? "Подключено. Можно запускать распознавание выбранных страниц."
    : "Не настроено. Укажите Folder ID и API key в настройках.";
  status.className = ready ? "mode-status ok" : "mode-status warning";
  $("#yandex-language-codes").textContent = (state.settings?.yandex?.language_codes || ["ru", "en"]).join(", ");
}

function initializeSettings(settings) {
  $("#settings-folder-id").value = settings?.yandex?.folder_id || "";
  $("#settings-vision-model").value = settings?.yandex?.vision_model || "table";
  $("#settings-language-codes").value = (settings?.yandex?.language_codes || ["ru", "en"]).join(",");
  const sourcingProvider = settings?.sourcing?.provider;
  $("#settings-sourcing-provider").value = ["local_catalog", "demo_store_http", "etm_ipro"].includes(sourcingProvider)
    ? sourcingProvider : "local_catalog";
  $("#settings-demo-store-url").value = settings?.sourcing?.demo_store_base_url || "http://127.0.0.1:8877";
  const etm = settings?.sourcing?.etm_ipro || {};
  $("#settings-etm-environment").value = etm.environment || "test";
  $("#settings-etm-warehouses").value = (etm.warehouse_codes || []).join(",");
  $("#settings-etm-base-url").value = etm.base_url_override || "";
  $("#settings-etm-login").value = "";
  $("#settings-etm-password").value = "";
  updateSourcingProviderFields();
  const ready = Boolean(state.ocrHealth?.available);
  const visionKeyReady = Boolean(settings?.yandex?.vision_api_key_configured ?? settings?.yandex?.api_key_configured);
  $("#settings-connection").textContent = ready
    ? `Yandex Vision подключён · ключ ${visionKeyReady ? "настроен" : "не настроен"}.`
    : `Yandex Vision не настроен · ключ ${visionKeyReady ? "настроен" : "не настроен"}.`;
  $("#settings-connection").className = ready ? "mode-status ok" : "mode-status warning";
  const folder = settings?.yandex?.folder_id || "";
  const proposal = folder ? `gpt://${folder}/qwen3.6-35b-a3b/latest` : "";
  $("#settings-llm-model").value = settings?.yandex?.llm_model || proposal;
  const aiKeyReady = Boolean(settings?.yandex?.ai_api_key_configured);
  $("#settings-ai-connection").textContent = `Qwen / AI Studio: ключ ${aiKeyReady ? "настроен" : "не настроен"}.`;
  $("#settings-ai-connection").className = aiKeyReady ? "mode-status ok" : "mode-status warning";
  const etmLoginReady = Boolean(etm.login_configured);
  const etmPasswordReady = Boolean(etm.password_configured);
  const etmStatus = $("#settings-etm-connection");
  if (etmStatus) {
    etmStatus.textContent = "ЭТМ iPRO: " + (etmLoginReady && etmPasswordReady ? "учётные данные настроены." : "учётные данные не настроены.");
    etmStatus.className = etmLoginReady && etmPasswordReady ? "mode-status ok" : "mode-status warning";
  }
  updateSourcingStatus();
}

function updateSourcingProviderFields() {
  const provider = $("#settings-sourcing-provider");
  const url = $("#settings-demo-store-url");
  if (!provider || !url) return;
  url.disabled = provider.value !== "demo_store_http";
  ["#settings-etm-environment", "#settings-etm-warehouses", "#settings-etm-base-url", "#settings-etm-login", "#settings-etm-password"]
    .forEach((selector) => { const element = $(selector); if (element) element.disabled = provider.value !== "etm_ipro"; });
}

function updateSourcingStatus() {
  const ai = state.sourcingHealth?.ai || state.config?.sourcing?.ai || {};
  const status = ai.status || "not_configured";
  const keyReady = Boolean(state.settings?.yandex?.ai_api_key_configured);
  const element = $("#settings-ai-connection");
  if (!element) return;
  if (status === "ready") {
    element.textContent = "Qwen подключён · AI Studio";
    element.className = "mode-status ok";
  } else if (status === "access_denied") {
    element.textContent = "Ключ не имеет доступа к AI Studio. Используется резервный режим.";
    element.className = "mode-status warning";
  } else if (!keyReady) {
    element.textContent = "Qwen / AI Studio: ключ не настроен.";
    element.className = "mode-status warning";
  } else if (ai.available) {
    element.textContent = "Qwen настроен; доступ проверится при подборе.";
    element.className = "mode-status warning";
  } else {
    element.textContent = "Qwen / AI Studio: ключ настроен; модель или доступ не проверены.";
    element.className = "mode-status warning";
  }
}

async function saveSettings() {
  const codes = $("#settings-language-codes").value.split(",").map((value) => value.trim()).filter(Boolean);
  if (!codes.length) { toast("Укажите хотя бы один язык OCR", "error"); return; }
  const apiKey = $("#settings-api-key").value.trim();
  const aiApiKey = $("#settings-ai-api-key").value.trim();
  const etmLogin = $("#settings-etm-login").value.trim();
  const etmPassword = $("#settings-etm-password").value.trim();
  const etmWarehouses = $("#settings-etm-warehouses").value.split(",").map((value) => value.trim()).filter(Boolean);
  const selectedProvider = $("#settings-sourcing-provider").value;
  const payload = {
    processing_mode: "cloud",
    yandex: {
      folder_id: $("#settings-folder-id").value.trim(),
      vision_model: $("#settings-vision-model").value,
      language_codes: codes,
      llm_model: $("#settings-llm-model").value.trim(),
    },
    sourcing: {
      provider: selectedProvider,
      demo_store_base_url: $("#settings-demo-store-url").value.trim(),
      etm_ipro: {
        enabled: selectedProvider === "etm_ipro" || Boolean(state.settings?.sourcing?.etm_ipro?.enabled),
        environment: $("#settings-etm-environment").value,
        warehouse_codes: etmWarehouses,
        base_url_override: $("#settings-etm-base-url").value.trim(),
      },
    },
  };
  if (apiKey) payload.api_key = apiKey;
  if (aiApiKey) payload.ai_api_key = aiApiKey;
  if (etmLogin) payload.etm_login = etmLogin;
  if (etmPassword) payload.etm_password = etmPassword;
  try {
    state.settings = await api("/api/settings", {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    const health = await api("/api/health");
    state.ocrHealth = health.cloud_ocr;
    state.sourcingHealth = health.sourcing || null;
    $("#settings-api-key").value = "";
    $("#settings-ai-api-key").value = "";
    $("#settings-etm-login").value = "";
    $("#settings-etm-password").value = "";
    initializeSettings(state.settings);
    updateCloudStatus();
    updateSourcingStatus();
    $("#settings-modal").close();
    toast("Настройки Yandex Vision сохранены", "success");
  } catch (error) { toast(error.message, "error"); }
}

function populateFilters() {
  const type = $("#type-filter");
  Object.entries(state.config.row_types).forEach(([value, label]) => type.add(new Option(label, value)));
  const status = $("#status-filter");
  Object.entries(state.config.statuses).forEach(([value, label]) => status.add(new Option(label, value)));
}

function initializeExportColumns() {
  const available = new Set(state.config.columns.map((column) => column.key));
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem("averonExportConfig") || "null"); } catch (_) {}
  const savedOrder = Array.isArray(saved?.order) ? saved.order.filter((key) => available.has(key)) : [];
  const remaining = state.config.columns.map((column) => column.key).filter((key) => !savedOrder.includes(key));
  state.exportOrder = [...savedOrder, ...remaining];
  const selected = Array.isArray(saved?.selected) ? saved.selected.filter((key) => available.has(key)) : state.config.default_export_columns;
  state.exportSelected = new Set(selected);
  renderExportColumns();
}

function saveExportPreferences() {
  localStorage.setItem("averonExportConfig", JSON.stringify({
    order: state.exportOrder,
    selected: [...state.exportSelected],
  }));
}

function newManualRowId() {
  if (globalThis.crypto?.randomUUID) return `manual-${globalThis.crypto.randomUUID()}`;
  return `manual-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function manualRow(values = {}) {
  const row = {id: String(values.id || newManualRowId()), row_type: "item", source_origin: "manual", selected: values.selected !== false};
  MANUAL_FIELDS.forEach((key) => { row[key] = String(values[key] ?? ""); });
  return row;
}

function manualDraftPayload() {
  return {
    active: Boolean(state.manual.active),
    rows: state.manual.rows.map((row) => ({
      id: row.id,
      selected: row.selected !== false,
      ...Object.fromEntries(MANUAL_FIELDS.map((key) => [key, String(row[key] ?? "")])),
    })),
  };
}

function saveManualDraft() {
  try { sessionStorage.setItem(MANUAL_DRAFT_KEY, JSON.stringify(manualDraftPayload())); } catch (_) {}
}

function loadManualDraft() {
  try {
    const payload = JSON.parse(sessionStorage.getItem(MANUAL_DRAFT_KEY) || "null");
    if (!payload || !Array.isArray(payload.rows)) return;
    state.manual.active = Boolean(payload.active);
    state.manual.rows = payload.rows.map((row) => manualRow(row));
  } catch (_) {
    state.manual = {active: false, rows: []};
  }
}

function manualRowById(id) {
  return state.manual.rows.find((row) => row.id === id);
}

function manualRowValidation(row) {
  const errors = [];
  const warnings = [];
  const identity = ["name", "type_mark", "code"].some((key) => String(row[key] || "").trim());
  if (!identity) errors.push("Укажите наименование, модель или артикул.");
  const quantity = String(row.quantity || "").trim();
  const unit = String(row.unit || "").trim();
  let quantityValid = true;
  if (quantity) {
    quantityValid = /^\d+(?:[.,]\d+)?$/.test(quantity) && Number(quantity.replace(",", ".")) > 0;
    if (!quantityValid) errors.push("Количество должно быть числом больше нуля.");
    else if (!unit) warnings.push("Для расчёта итоговой стоимости укажите количество и единицу.");
  }
  return {valid: !errors.length, errors, warnings, quantityTrusted: Boolean(quantity && quantityValid && unit)};
}

function isBlankManualPlaceholder(row) {
  return MANUAL_FIELDS.every((key) => !String(row[key] || "").trim());
}

function manualFeedbackHtml(validation) {
  return [...validation.errors.map((message) => `<span class="manual-error">${escapeHtml(message)}</span>`), ...validation.warnings.map((message) => `<span class="manual-warning">${escapeHtml(message)}</span>`)].join("");
}

function updateManualRowFeedback(row) {
  const node = document.querySelector(`#manual-body tr[data-manual-id="${CSS.escape(row.id)}"]`);
  if (!node) return;
  const validation = manualRowValidation(row);
  node.classList.toggle("manual-invalid", !validation.valid);
  const feedback = node.querySelector(".manual-row-feedback");
  if (feedback) feedback.innerHTML = manualFeedbackHtml(validation);
  updateManualSummary();
}

function updateManualSummary() {
  const selected = state.manual.rows.filter((row) => row.selected !== false).length;
  const badge = $("#manual-selected-count");
  if (badge) badge.textContent = `Выбрано: ${selected}`;
  const selectAll = $("#manual-select-all");
  if (selectAll) {
    selectAll.checked = Boolean(state.manual.rows.length) && state.manual.rows.every((row) => row.selected !== false);
    selectAll.indeterminate = state.manual.rows.some((row) => row.selected !== false) && !selectAll.checked;
  }
}

function renderManualRows() {
  const body = $("#manual-body");
  if (!body) return;
  body.innerHTML = state.manual.rows.map((row, index) => {
    const validation = manualRowValidation(row);
    const input = (key, label, placeholder = "") => `<input class="manual-cell-input" data-manual-id="${escapeHtml(row.id)}" data-key="${key}" aria-label="${escapeHtml(label)}" placeholder="${escapeHtml(placeholder)}" value="${escapeHtml(row[key])}">`;
    return `<tr data-manual-id="${escapeHtml(row.id)}" class="${validation.valid ? "" : "manual-invalid"}">
      <td class="manual-selector"><input class="manual-row-select" data-manual-id="${escapeHtml(row.id)}" type="checkbox" ${row.selected !== false ? "checked" : ""} aria-label="Выбрать строку ${index + 1}"></td>
      <td class="manual-number">${index + 1}</td>
      <td>${input("name", "Наименование или описание", "Например: насос циркуляционный")}</td>
      <td>${input("type_mark", "Модель или тип", "Модель / тип")}</td>
      <td>${input("manufacturer", "Производитель", "Производитель")}</td>
      <td>${input("code", "Артикул или код", "Артикул / код")}</td>
      <td>${input("quantity", "Количество", "2 или 2,5")}</td>
      <td><input class="manual-cell-input" list="manual-unit-suggestions" data-manual-id="${escapeHtml(row.id)}" data-key="unit" aria-label="Единица измерения" placeholder="шт." value="${escapeHtml(row.unit)}"></td>
      <td class="manual-actions"><div><button class="button text manual-understand" type="button" data-manual-id="${escapeHtml(row.id)}">Разобрать</button><button class="button text manual-delete" type="button" data-manual-id="${escapeHtml(row.id)}" aria-label="Удалить строку ${index + 1}">Удалить</button></div><div class="manual-row-feedback">${manualFeedbackHtml(validation)}</div></td>
    </tr>`;
  }).join("");
  $("#manual-empty").hidden = state.manual.rows.length > 0;
  body.querySelectorAll(".manual-cell-input").forEach((input) => {
    input.addEventListener("input", () => {
      const row = manualRowById(input.dataset.manualId);
      if (!row) return;
      row[input.dataset.key] = input.value;
      saveManualDraft();
      updateManualRowFeedback(row);
    });
    input.addEventListener("focus", () => { const row = manualRowById(input.dataset.manualId); if (row) input.closest("tr")?.classList.add("manual-focused"); });
    input.addEventListener("blur", () => input.closest("tr")?.classList.remove("manual-focused"));
  });
  body.querySelectorAll(".manual-row-select").forEach((input) => input.addEventListener("change", () => {
    const row = manualRowById(input.dataset.manualId);
    if (!row) return;
    row.selected = input.checked;
    saveManualDraft();
    updateManualSummary();
  }));
  body.querySelectorAll(".manual-delete").forEach((button) => button.addEventListener("click", () => {
    state.manual.rows = state.manual.rows.filter((row) => row.id !== button.dataset.manualId);
    saveManualDraft();
    renderManualRows();
  }));
  body.querySelectorAll(".manual-understand").forEach((button) => button.addEventListener("click", () => {
    const row = manualRowById(button.dataset.manualId);
    if (row) openManualUnderstanding(row);
  }));
  updateManualSummary();
}

function addManualRow(values = {}) {
  state.manual.rows.push(manualRow(values));
  saveManualDraft();
  renderManualRows();
  const last = state.manual.rows[state.manual.rows.length - 1];
  requestAnimationFrame(() => document.querySelector(`.manual-cell-input[data-manual-id="${CSS.escape(last.id)}"][data-key="name"]`)?.focus());
}

function openManualWorkspace() {
  state.manual.active = true;
  if (!state.manual.rows.length) state.manual.rows.push(manualRow());
  saveManualDraft();
  renderManualRows();
  setView("manual");
}

function returnToStartFromManual() {
  state.manual.active = false;
  saveManualDraft();
  setView("upload");
}

function parseManualPaste(text) {
  const lines = String(text || "").split(/\r?\n/).filter((line) => line.trim());
  if (!lines.length) return [];
  const tsv = lines.some((line) => line.includes("\t"));
  return lines.map((line) => {
    if (!tsv) return manualRow({name: line.trim()});
    const cells = line.split("\t").map((cell) => cell.trim());
    if (cells.length > MANUAL_FIELDS.length) throw new Error("TSV должен содержать не более шести столбцов в фиксированном порядке");
    return manualRow(Object.fromEntries(MANUAL_FIELDS.map((key, index) => [key, cells[index] || ""])));
  });
}

function applyManualPaste() {
  try {
    const rows = parseManualPaste($("#manual-paste-input").value);
    if (!rows.length) { toast("Вставьте хотя бы одну непустую строку", "error"); return; }
    if (state.manual.rows.length === 1 && isBlankManualPlaceholder(state.manual.rows[0])) state.manual.rows = rows;
    else state.manual.rows.push(...rows);
    saveManualDraft();
    renderManualRows();
    $("#manual-paste-modal").close();
    $("#manual-paste-input").value = "";
  } catch (error) { toast(error.message, "error"); }
}

function clearManualDraft() {
  if (!state.manual.rows.length) return;
  if (!confirm("Очистить все введённые позиции?")) return;
  state.manual.rows = [];
  saveManualDraft();
  renderManualRows();
}

function manualRowsForSourcing() {
  return state.manual.rows.filter((row) => row.selected !== false).map((row) => {
    const validation = manualRowValidation(row);
    return {
      id: row.id,
      row_type: "item",
      selected: true,
      name: row.name,
      type_mark: row.type_mark,
      manufacturer: row.manufacturer,
      code: row.code,
      quantity: row.quantity,
      unit: row.unit,
      quantity_trusted: validation.quantityTrusted,
      status: "recognized",
      source_origin: "manual",
    };
  });
}

function renderManualUnderstandingWarnings(warnings) {
  const values = (warnings || []).filter((warning) => String(warning || "").trim());
  return values.length ? `<div class="manual-diagnostic-warnings"><b>Диагностические сообщения</b>${values.map((warning) => `<span>${escapeHtml(warning)}</span>`).join("")}</div>` : "";
}

async function openManualUnderstanding(row) {
  const validation = manualRowValidation(row);
  if (!validation.valid) {
    updateManualRowFeedback(row);
    toast(validation.errors[0], "error");
    const focusKey = !["name", "type_mark", "code"].some((key) => String(row[key] || "").trim()) ? "name" : "quantity";
    requestAnimationFrame(() => document.querySelector(`.manual-cell-input[data-manual-id="${CSS.escape(row.id)}"][data-key="${focusKey}"]`)?.focus());
    return;
  }
  state.sourcing.row = row;
  state.sourcing.result = null;
  $("#sourcing-subtitle").textContent = "Разбираем ручную позицию…";
  $("#sourcing-content").innerHTML = `<div class="sourcing-loading"><span class="spinner"></span><b>Product Understanding</b><small>Исходные поля останутся без изменений</small></div>`;
  $("#sourcing-modal").showModal();
  try {
    const response = await api("/api/sourcing/understand", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({row})});
    $("#sourcing-subtitle").textContent = "Разбор ручной позиции завершён";
    $("#sourcing-content").innerHTML = `${renderSourcingNotices(response.notices)}${renderManualUnderstandingWarnings(response.warnings)}${renderProductUnderstanding(response.understanding)}`;
  } catch (error) {
    $("#sourcing-subtitle").textContent = "Разбор не выполнен";
    $("#sourcing-content").innerHTML = `<div class="sourcing-warning">${escapeHtml(error.message)}</div>`;
  }
}

async function resumeLastDocument() {
  const documentId = localStorage.getItem("averonCurrentDocument");
  if (!documentId) return;
  try {
    await openExistingDocument(documentId, {announce: false});
    toast("Последний документ восстановлен", "success");
  } catch (_) {
    localStorage.removeItem("averonCurrentDocument");
  }
}

async function loadRecentDocuments() {
  const payload = await api("/api/documents?limit=50");
  state.recentDocuments = Array.isArray(payload?.documents) ? payload.documents : [];
  renderRecentDocuments();
}

function formatRecentTimestamp(value) {
  if (!value) return "Дата неизвестна";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "Дата неизвестна";
  return new Intl.DateTimeFormat("ru-RU", {dateStyle:"short", timeStyle:"short"}).format(parsed);
}

function renderRecentDocuments() {
  const panel = $("#recent-documents-panel");
  const list = $("#recent-documents-list");
  if (!panel || !list) return;
  panel.hidden = !state.recentDocuments.length;
  list.innerHTML = state.recentDocuments.map((item) => {
    const unavailable = item.available === false;
    const meta = `${item.page_count || 0} стр. · ${item.has_result ? "результат сохранён" : "без результата"} · ${formatRecentTimestamp(item.updated_at)}`;
    return `<div class="recent-document-item${unavailable ? " unavailable" : ""}"><div><b>${escapeHtml(item.filename || item.title || "Документ")}</b><small>${escapeHtml(meta)}${unavailable ? ` · ${escapeHtml(item.availability_error || "недоступен")}` : ""}</small></div><button class="button ghost recent-document-open" type="button" data-document-id="${escapeHtml(item.document_id)}"${unavailable ? " disabled" : ""}>${unavailable ? "Недоступен" : "Открыть"}</button></div>`;
  }).join("");
  list.querySelectorAll(".recent-document-open").forEach((button) => button.addEventListener("click", () => {
    openExistingDocument(button.dataset.documentId).catch((error) => toast(error.message, "error"));
  }));
}

async function openExistingDocument(documentId, {announce = true} = {}) {
  if (!documentId) throw new Error("Документ не выбран");
  const encodedId = encodeURIComponent(documentId);
  const documentData = await api(`/api/documents/${encodedId}`);
  state.document = documentData;
  state.selectedPages = new Set();
  state.previewPage = null;
  state.crop = null;
  state.rows = [];
  state.result = null;
  state.activeRowId = null;
  state.dirty = false;
  localStorage.setItem("averonCurrentDocument", documentData.document_id);
  $("#document-name").textContent = documentData.filename;
  $("#document-meta").textContent = `${documentData.page_count} стр. · ${bytes(documentData.size)}`;
  $("#new-document-button").hidden = false;
  if (documentData.has_result) {
    const result = await api(`/api/documents/${encodedId}/results`);
    loadResult(result, {announce});
  } else {
    setView("pages");
    renderThumbnails();
  }
  if (announce && !documentData.has_result) toast("Документ открыт", "success");
}

async function uploadFile(file) {
  if (!file || !file.name.toLowerCase().endsWith(".pdf")) {
    toast("Выберите PDF-файл", "error");
    return;
  }
  const form = new FormData();
  form.append("file", file);
  const card = $("#drop-zone");
  card.classList.add("drag");
  try {
    const documentData = await api("/api/documents", {method:"POST", body:form});
    state.document = documentData;
    localStorage.setItem("averonCurrentDocument", documentData.document_id);
    state.selectedPages.clear();
    state.previewPage = null;
    state.crop = null;
    $("#document-name").textContent = documentData.filename;
    $("#document-meta").textContent = `${documentData.page_count} стр. · ${bytes(documentData.size)}`;
    $("#new-document-button").hidden = false;
    setView("pages");
    renderThumbnails();
    loadRecentDocuments().catch(() => {});
  } catch (error) {
    toast(error.message, "error");
  } finally {
    card.classList.remove("drag");
    $("#pdf-file").value = "";
  }
}

function renderThumbnails() {
  const grid = $("#thumbnail-grid");
  grid.innerHTML = "";
  for (let page = 1; page <= state.document.page_count; page += 1) {
    const node = document.createElement("div");
    node.className = "thumbnail";
    node.dataset.page = page;
    node.innerHTML = `
      <img loading="lazy" src="/api/documents/${state.document.document_id}/page/${page}?dpi=72" alt="Страница ${page}">
      <div class="thumbnail-footer"><span>Страница ${page}</span><input type="checkbox" aria-label="Выбрать страницу ${page}"></div>`;
    node.addEventListener("click", (event) => {
      event.preventDefault();
      togglePage(page);
      showCropPreview(page);
    });
    grid.appendChild(node);
  }
  updatePageSelection();
}

function togglePage(page, force) {
  const selected = force === undefined ? !state.selectedPages.has(page) : force;
  if (selected) state.selectedPages.add(page); else state.selectedPages.delete(page);
  updatePageSelection();
}

function updatePageSelection() {
  $$(".thumbnail").forEach((node) => {
    const page = Number(node.dataset.page);
    const selected = state.selectedPages.has(page);
    node.classList.toggle("selected", selected);
    node.querySelector("input").checked = selected;
    node.classList.toggle("previewing", state.previewPage === page);
  });
  $("#selected-pages-badge").textContent = `${state.selectedPages.size} выбрано`;
  const recognizeButton = $("#recognize-button");
  if (recognizeButton) recognizeButton.disabled = state.selectedPages.size === 0;
  if (state.selectedPages.size) {
    $("#page-range").value = compactRanges([...state.selectedPages].sort((a,b)=>a-b));
  }
}

function compactRanges(pages) {
  if (!pages.length) return "";
  const groups = [];
  let start = pages[0], previous = pages[0];
  for (const page of pages.slice(1)) {
    if (page === previous + 1) previous = page;
    else { groups.push(start === previous ? `${start}` : `${start}-${previous}`); start = previous = page; }
  }
  groups.push(start === previous ? `${start}` : `${start}-${previous}`);
  return groups.join(", ");
}

function parseRanges(value) {
  const pages = new Set();
  const tokens = value.trim().split(/[,;\s]+/).filter(Boolean);
  for (const token of tokens) {
    if (/\d+[-–—]\d+/.test(token)) {
      let [start, end] = token.split(/[-–—]/).map(Number);
      if (start > end) [start,end] = [end,start];
      for (let page=start; page<=end; page++) pages.add(page);
    } else if (/^\d+$/.test(token)) pages.add(Number(token));
    else throw new Error(`Некорректный диапазон: ${token}`);
  }
  const invalid = [...pages].filter((page) => page < 1 || page > state.document.page_count);
  if (invalid.length) throw new Error(`Страницы вне документа: ${invalid.join(", ")}`);
  return pages;
}

async function showCropPreview(page) {
  state.previewPage = page;
  updatePageSelection();
  const image = $("#crop-image");
  $("#crop-placeholder").hidden = true;
  image.hidden = false;
  image.src = `/api/documents/${state.document.document_id}/page/${page}?dpi=120`;
  if (state.crop) positionCropBox();
}

function positionCropBox() {
  const box = $("#crop-box");
  if (!state.crop) { box.hidden = true; return; }
  const container = $("#crop-preview").getBoundingClientRect();
  const image = $("#crop-image").getBoundingClientRect();
  box.hidden = false;
  box.style.left = `${image.left - container.left + state.crop.x * image.width}px`;
  box.style.top = `${image.top - container.top + state.crop.y * image.height}px`;
  box.style.width = `${state.crop.width * image.width}px`;
  box.style.height = `${state.crop.height * image.height}px`;
  $("#clear-crop").hidden = false;
}

async function startRecognition() {
  const pages = [...state.selectedPages].sort((a,b)=>a-b);
  if (!pages.length) return;
  setView("processing");
  $("#processing-progress").style.width = "0%";
  $("#processing-title").textContent = "Облачное распознавание";
  $("#processing-message").textContent = "Подготовка страниц для Yandex Vision OCR…";
  try {
    const job = await api(`/api/documents/${state.document.document_id}/recognize`, {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        pages,
        crop:state.crop,
        dpi:300,
        ocr_mode:"standard",
        ai_provider:"off",
        processing_mode:"cloud",
      }),
    });
    await pollJob(job.id);
  } catch (error) {
    toast(error.message, "error");
    setView("pages");
  }
}

async function pollJob(jobId) {
  while (true) {
    const job = await api(`/api/jobs/${jobId}`);
    const total = job.total || state.selectedPages.size;
    const percent = total ? Math.round(job.current / total * 100) : 0;
    $("#processing-progress").style.width = `${percent}%`;
    $("#processing-count").textContent = `${job.current} / ${total}`;
    $("#processing-message").textContent = job.message;
    if (job.status === "completed") {
      loadResult(job.result);
      return;
    }
    if (job.status === "failed") throw new Error(job.error || "Ошибка распознавания");
    await new Promise((resolve) => setTimeout(resolve, 700));
  }
}

function isYandexCriticalRow(row) {
  return row?.ocr_metadata?.provider === "yandex_vision"
    && ["item", "component"].includes(row.row_type)
    && ["name", "position", "type_mark", "code", "manufacturer"]
      .some((key) => String(row[key] ?? "").trim());
}

function missingCriticalFields(row) {
  if (!isYandexCriticalRow(row)) return [];
  const semantic = row?.semantic_authoritative || row?.ocr_metadata?.semantic_authoritative;
  const declared = row?.ocr_metadata?.semantic_required_critical_fields;
  const fields = semantic
    ? (Array.isArray(declared) ? CRITICAL_FIELDS.filter((key) => declared.includes(key)) : CRITICAL_FIELDS)
    : CRITICAL_FIELDS;
  return fields.filter((key) => !String(row[key] ?? "").trim());
}

function numericSuspectFields(row) {
  if (row.status === "verified" && !missingCriticalFields(row).length) return [];
  const normalization = row?.ocr_metadata?.normalization || {};
  const edited = new Set(row?.edited_fields || []);
  return CRITICAL_FIELDS.filter((key) => {
    if (!["quantity", "mass"].includes(key)) return false;
    const details = normalization[key];
    const shapeSuspect = key === "quantity" && details?.integer_like_decimal;
    if (!details?.numeric_suspect && !shapeSuspect) return false;
    if (shapeSuspect && numericShapeAgreed(row)) return false;
    if (!edited.has(key)) return true;
    if (shapeSuspect && key === "quantity") {
      return /^-?\d+[.,]0$/.test(String(row[key] ?? "").trim());
    }
    return !/^-?\d+(?:[.,]\d+)?$/.test(String(row[key] ?? "").trim());
  });
}

function numericShapeAgreed(row) {
  const candidate = row?.value_candidates?.quantity
    || row?.ocr_metadata?.value_candidates?.quantity;
  const safety = row?.ocr_metadata?.target_cell_structural_safety?.quantity;
  return candidate?.agreement_with_primary === true
    && candidate?.verified_by_exact_cell_ocr === true
    && String(candidate?.candidate_source || "").startsWith("yandex_exact_cell")
    && safety?.safe === true
    && String(row?.quantity ?? "").trim() === String(candidate?.value_candidate ?? "").trim();
}

function criticalBlockers(row) {
  const missing = missingCriticalFields(row);
  const suspect = numericSuspectFields(row);
  const explicitlyVerified = row.status === "verified" && !missing.length;
  const reasons = new Set([
    ...(row?.review_reasons || []),
    ...(row?.critical_blockers || []),
  ]);
  const conflictFields = new Set(row?.ocr_metadata?.secondary_conflict_fields || []);
  const edited = new Set(row?.edited_fields || []);
  const conflictActive = reasons.has("secondary_conflict")
    && ![...conflictFields].some((field) => edited.has(field));
  const blockers = [];
  if (missing.length) blockers.push("critical_value_missing");
  if (suspect.length && !explicitlyVerified) blockers.push("numeric_suspect");
  if (reasons.has("ambiguous_columns") && !explicitlyVerified) blockers.push("ambiguous_columns");
  if (conflictActive && !explicitlyVerified) blockers.push("secondary_conflict");
  return [...new Set(blockers)];
}

function criticalFieldCount(row) {
  const missing = missingCriticalFields(row);
  const suspect = row.status === "verified" && !missing.length
    ? []
    : numericSuspectFields(row).filter((field) => !missing.includes(field));
  const blockers = criticalBlockers(row);
  return missing.length + suspect.length
    || (blockers.some((reason) => ["ambiguous_columns", "secondary_conflict"].includes(reason)) ? 1 : 0)
    || (blockers.includes("numeric_suspect") ? 1 : 0);
}

function refreshClientReview(row) {
  const missing = missingCriticalFields(row);
  const suspect = numericSuspectFields(row);
  const reasons = new Set(row.review_reasons || []);
  const previousBlockers = new Set(row.critical_blockers || []);
  const edited = new Set(row.edited_fields || []);
  const conflicts = new Set(row.ocr_metadata?.secondary_conflict_fields || []);
  reasons.delete("critical_value_missing");
  reasons.delete("numeric_suspect");
  reasons.delete("numeric_shape_suspect");
  if (missing.length) reasons.add("critical_value_missing");
  if (suspect.length) reasons.add("numeric_suspect");
  if (numericSuspectFields(row).includes("quantity") && !numericShapeAgreed(row)) {
    reasons.add("numeric_shape_suspect");
  }
  if (!(conflicts.size && [...conflicts].some((field) => edited.has(field)))) {
    if (previousBlockers.has("secondary_conflict")) reasons.add("secondary_conflict");
  } else {
    reasons.delete("secondary_conflict");
  }
  if (row.status === "verified" && !missing.length) {
    reasons.delete("ambiguous_columns");
    reasons.delete("secondary_conflict");
  }
  row.critical_fields = missing;
  row.review_reasons = [...reasons];
  row.critical_blockers = criticalBlockers(row);
  if (row.critical_blockers.length && ["recognized", "verified"].includes(row.status)) row.status = "review";
  return row;
}

function loadResult(result, options = {}) {
  state.result = result;
  state.previewPage = null;
  setZoom(1);
  state.rows = result.rows.map((row) => ({
    ...row,
    selected: row.selected ?? (
      ["item", "component"].includes(row.row_type)
      || (row.row_type === "note" && row.structured_table)
    ),
  }));
  state.rows.forEach(refreshClientReview);
  state.reviewFilter = "";
    state.dirty = false;
    buildResultHeader();
  renderRows();
  updateSummary();
  setView("review");
  const first = state.rows.find((row) => row.selected) || state.rows[0];
  if (first) selectRow(first.id);
  if (options.announce === false) return;
  if (result.errors?.length) {
    const pages = result.errors.map((error) => error.page).join(", ");
    const details = [...new Set(result.errors.map((error) => error.error).filter(Boolean))]
      .slice(0, 2)
      .join("; ");
    toast(`Не удалось обработать страницы: ${pages}${details ? `. Причина: ${details}` : ""}`, "error");
  } else if (result.ai?.enabled && ["failed", "partial"].includes(result.ai.status)) {
    const warning = result.ai.warnings?.[0] || "AI-проверка завершилась с ошибкой";
    toast(`OCR сохранён. ${warning}`, "error");
  } else if (result.ai?.enabled) {
    toast(`OCR и AI завершены · исправлено строк: ${result.ai.changed_rows || 0}`, "success");
  } else {
    toast("Распознавание завершено", "success");
  }
}

const displayColumns = [
  "position", "name", "type_mark", "code", "manufacturer", "unit", "quantity", "mass", "note",
  "section", "system", "row_type", "page", "confidence", "status",
];

function buildResultHeader() {
  const head = $("#result-head");
  head.innerHTML = `<tr><th class="selector"><input type="checkbox" id="select-all-rows" title="Выбрать все позиции"></th>${displayColumns.map((key) => {
    const column = state.config.columns.find((item) => item.key === key);
    return `<th style="min-width:${columnWidth(key)}px">${escapeHtml(column?.title || key)}</th>`;
  }).join("")}<th class="sourcing-column">Подбор</th></tr>`;
  $("#select-all-rows").addEventListener("change", (event) => {
    filteredRows().forEach((row) => row.selected = event.target.checked);
    renderRows(); updateSummary(); markDirty();
  });
}

function columnWidth(key) {
  const widths = {position:85,name:350,type_mark:220,code:150,manufacturer:180,unit:105,quantity:100,mass:110,note:260,section:145,system:100,row_type:155,page:90,confidence:120,status:155};
  return widths[key] || 130;
}

function filteredRows() {
  const query = $("#table-search").value.trim().toLowerCase();
  const type = $("#type-filter").value;
  const status = $("#status-filter").value;
  const reviewFilter = $("#review-filter").value;
  return state.rows.filter((row) => {
    if (type && row.row_type !== type) return false;
    if (status && row.status !== status) return false;
    const missing = missingCriticalFields(row);
    const blockers = criticalBlockers(row);
    if (reviewFilter === "critical" && !blockers.length) return false;
    if (reviewFilter === "quantity-missing" && !missing.includes("quantity")) return false;
    if (reviewFilter === "unit-missing" && !missing.includes("unit")) return false;
    if (reviewFilter === "numeric-suspect" && !blockers.includes("numeric_suspect")) return false;
    if (reviewFilter === "verified" && row.status !== "verified") return false;
    if (query && !displayColumns.some((key) => String(row[key] ?? "").toLowerCase().includes(query))) return false;
    return true;
  });
}

function renderRows() {
  const body = $("#result-body");
  const rows = filteredRows();
  body.innerHTML = rows.map((row) => rowHtml(row)).join("");
  $("#empty-table").hidden = rows.length > 0;
  body.querySelectorAll("tr").forEach((tr) => {
    tr.addEventListener("click", (event) => {
      if (!event.target.matches("input,textarea,select,option")) selectRow(tr.dataset.id);
    });
  });
  body.querySelectorAll(".row-select").forEach((input) => input.addEventListener("change", (event) => {
    const row = rowById(event.target.dataset.id); row.selected = event.target.checked; updateSummary(); markDirty();
  }));
  body.querySelectorAll(".cell-input").forEach((input) => {
    autoHeight(input);
    input.addEventListener("input", () => {
      const row = rowById(input.dataset.id);
      row[input.dataset.key] = input.value;
      row.edited_fields = [...new Set([...(row.edited_fields || []), input.dataset.key])];
      if (row.status !== "verified") row.status = "edited";
      row.edited = true;
      refreshClientReview(row);
      autoHeight(input); markDirty(); updateSummary();
    });
    input.addEventListener("focus", () => selectRow(input.dataset.id));
  });
  body.querySelectorAll(".cell-select").forEach((select) => select.addEventListener("change", () => {
    const row = rowById(select.dataset.id);
    row[select.dataset.key] = select.value;
    row.edited_fields = [...new Set([...(row.edited_fields || []), select.dataset.key])];
    row.edited = true;
    if (select.dataset.key !== "status" && row.status !== "verified") row.status = "edited";
    refreshClientReview(row);
    markDirty(); updateSummary(); renderRows();
  }));
  body.querySelectorAll(".candidate-accept").forEach((button) => button.addEventListener("click", (event) => {
    event.stopPropagation();
    const row = rowById(button.dataset.id);
    const candidate = row?.value_candidates?.[button.dataset.key];
    if (!row || !candidate?.value_candidate) return;
    submitHumanDecision(row, {
      decision: "ACCEPT_FIELD_CANDIDATE",
      field: button.dataset.key,
      candidate_value: String(candidate.value_candidate),
    }, `${CRITICAL_LABELS[button.dataset.key]} подтверждено пользователем`);
  }));
  body.querySelectorAll(".continuation-accept").forEach((button) => button.addEventListener("click", (event) => {
    event.stopPropagation();
    const row = rowById(button.dataset.id);
    const parent = row && continuationParent(row);
    if (!row || !parent) return;
    const fragment = continuationFragment(row);
    submitHumanDecision(row, {
      decision: "ACCEPT_CONTINUATION_RELATION",
      relation: "human_confirmed_continuation",
      candidate_value: fragment,
      target: {parent_physical_refs: physicalRefs(parent)},
    }, "Продолжение привязано пользователем");
  }));
  body.querySelectorAll(".candidate-reject").forEach((button) => button.addEventListener("click", (event) => {
    event.stopPropagation();
    const row = rowById(button.dataset.id);
    const candidate = row?.value_candidates?.[button.dataset.key];
    if (!row || !candidate?.value_candidate) return;
    submitHumanDecision(row, {
      decision: "REJECT_CANDIDATE",
      field: button.dataset.key,
      candidate_value: String(candidate.value_candidate),
    }, "Кандидат отклонён и оставлен на проверке");
  }));
  body.querySelectorAll(".candidate-edit").forEach((button) => button.addEventListener("click", (event) => {
    event.stopPropagation();
    const row = rowById(button.dataset.id);
    if (!row) return;
    selectRow(row.id);
    const input = [...body.querySelectorAll(`.cell-input[data-id="${row.id}"]`)]
      .find((element) => element.dataset.key === button.dataset.key);
    if (input) { input.focus(); input.select(); }
  }));
  body.querySelectorAll(".sourcing-row-button").forEach((button) => button.addEventListener("click", (event) => {
    event.stopPropagation();
    const row = rowById(button.dataset.id);
    if (row) openSourcingForRow(row);
  }));
}

function physicalRefs(row) {
  return row?.ocr_metadata?.physical_row_refs || row?.physical_row_refs || [];
}

function sourceRowIndex(row) {
  const refs = physicalRefs(row);
  const values = refs.map((ref) => Number(ref.row_index)).filter(Number.isFinite);
  return values.length ? Math.min(...values) : Number.MAX_SAFE_INTEGER;
}

function continuationParentRefs(row) {
  const asRefSets = (value) => {
    if (!Array.isArray(value) || !value.length) return [];
    const isRef = (item) => item && typeof item === "object" && !Array.isArray(item)
      && (Object.prototype.hasOwnProperty.call(item, "table") || Object.prototype.hasOwnProperty.call(item, "row_index"));
    return value.every(isRef) ? [value] : value.filter(Array.isArray);
  };
  const sources = [
    row?.continuation_evidence,
    row?.ocr_metadata?.continuation_evidence,
    row?.candidate_parent_physical_refs,
    row?.ocr_metadata?.candidate_parent_physical_refs,
  ];
  for (const source of sources) {
    if (Array.isArray(source)) {
      const refSets = asRefSets(source);
      if (refSets.length) return refSets;
    }
    if (!source || typeof source !== "object") continue;
    for (const key of ["candidate_parent_physical_refs", "candidate_parent_refs", "parent_physical_refs", "candidate_target_refs"]) {
      const refSets = asRefSets(source[key]);
      if (refSets.length) return refSets;
    }
  }
  return [];
}

function samePhysicalRefs(left, right) {
  return JSON.stringify(left || []) === JSON.stringify(right || []);
}

function continuationParent(row) {
  const candidates = continuationParentRefs(row);
  if (!candidates.length) return null;
  return state.rows.find((candidate) => candidate.page === row.page
    && ["item", "component", "item_candidate"].includes(candidate.row_type)
    && candidates.some((refs) => samePhysicalRefs(refs, physicalRefs(candidate)))) || null;
}

function continuationFragment(row) {
  const preview = String(row.semantic_review_preview || row.ocr_metadata?.semantic_review_preview || "").trim();
  const candidates = row.value_candidates || {};
  for (const key of ["name", "type_mark", "manufacturer", "note"]) {
    const value = candidates[key]?.value_candidate;
    if (value) return String(value).trim();
  }
  return preview;
}

async function submitHumanDecision(row, payload, successMessage) {
  if (!state.document) return;
  try {
    const response = await api(`/api/documents/${state.document.document_id}/review/decision`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({page: row.page, physical_refs: physicalRefs(row), ...payload}),
    });
    loadResult(response.result, {announce: false});
    toast(`${successMessage} · Проверено пользователем ✓`, "success");
  } catch (error) {
    toast(error.message, "error");
  }
}

function rowHtml(row) {
  const active = row.id === state.activeRowId ? "active" : "";
  const review = ["review","unrecognized"].includes(row.status) ? "review" : "";
  const critical = criticalBlockers(row).length ? "critical-review" : "";
  const human = row.verification_state === "HUMAN_VERIFIED" || row.human_review ? `<span class="human-verified">Проверено пользователем ✓</span>` : "";
  const ocrVerified = !human && (row.status === "verified" || row.ocr_metadata?.semantic_state === "VERIFIED") ? `<span class="ocr-verified">Подтверждено OCR</span>` : "";
  return `<tr data-id="${row.id}" class="${active} ${review} ${critical}">
    <td class="selector"><input class="row-select" data-id="${row.id}" type="checkbox" ${row.selected ? "checked" : ""}></td>
    ${displayColumns.map((key) => cellHtml(row,key)).join("")}
    <td class="sourcing-cell">${human || ocrVerified}${sourcingEligible(row) ? `<button type="button" class="button text sourcing-row-button" data-id="${row.id}">Найти предложения</button>` : ""}</td>
  </tr>`;
}

function sourcingEligible(row) {
  return ["item", "component", "item_candidate"].includes(row.row_type)
    && Boolean(String(row.name || row.type_mark || row.code || "").trim());
}

function formatMoney(value, currency = "") {
  if (value === null || value === undefined || value === "") return "Цена не указана";
  return `${escapeHtml(String(value))}${currency ? ` ${escapeHtml(currency)}` : ""}`;
}

const SOURCING_DECISION_LABELS = Object.freeze({
  MATCH: "Совпадение",
  LIKELY_MATCH: "Вероятное совпадение",
  ALTERNATIVE: "Альтернатива",
  REVIEW: "Требует проверки",
  REJECT: "Не подходит",
  WITHOUT_OFFERS: "Нет предложений",
});

const SOURCING_DECISION_CLASSES = Object.freeze({
  MATCH: "match",
  LIKELY_MATCH: "likely-match",
  ALTERNATIVE: "alternative",
  REVIEW: "review",
  REJECT: "reject",
  WITHOUT_OFFERS: "without-offers",
});

const SOURCING_FILTERS = Object.freeze([
  {key: "all", label: "Все"},
  {key: "matches", label: "Совпадения", decisions: ["MATCH", "LIKELY_MATCH"]},
  {key: "review", label: "Требуют проверки", decisions: ["REVIEW", "REJECT"]},
  {key: "alternative", label: "Альтернативы", decisions: ["ALTERNATIVE"]},
  {key: "without-offers", label: "Без предложений", decisions: ["WITHOUT_OFFERS"]},
]);

function sourcingDecisionLabel(decision) {
  return SOURCING_DECISION_LABELS[String(decision || "").toUpperCase()] || "Требует проверки";
}

function sourcingDecisionClass(decision) {
  const normalized = String(decision || "").toUpperCase();
  return SOURCING_DECISION_CLASSES[normalized] || "unknown";
}

function renderSourcingDecision(decision, reason = "") {
  return `<span class="sourcing-decision sourcing-decision-${sourcingDecisionClass(decision)}"><span class="sourcing-decision-label">${escapeHtml(sourcingDecisionLabel(decision))}</span>${reason ? `<small class="sourcing-decision-reason">${escapeHtml(reason)}</small>` : ""}</span>`;
}

function renderSourcingNotices(notices) {
  if (!Array.isArray(notices)) return "";
  const rendered = notices.map((notice) => {
    if (!notice || notice.user_visible === false) return "";
    const message = String(notice.message ?? "").trim();
    if (!message) return "";
    const severity = ["info", "warning", "error"].includes(notice.severity)
      ? notice.severity : "warning";
    const role = severity === "error" ? "alert" : "status";
    return `<div class="sourcing-notice sourcing-notice-${severity}" role="${role}">${escapeHtml(message)}</div>`;
  }).join("");
  return rendered ? `<div class="sourcing-notices">${rendered}</div>` : "";
}

function safeOfferUrl(value) {
  if (value === null || value === undefined || value === "") return null;
  try {
    const url = new URL(String(value));
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch (_) {
    return null;
  }
}

function offerTitleHtml(offer) {
  const title = escapeHtml(offer?.title || "Без названия");
  const url = safeOfferUrl(offer?.url);
  return url
    ? `<a class="project-offer-link" target="_blank" rel="noopener noreferrer" href="${escapeHtml(url)}">${title} ↗</a>`
    : title;
}

function nonNegativeSourcingNumber(value) {
  if (value === null || value === undefined || (typeof value === "string" && !value.trim())) return null;
  if (!(["number", "string"].includes(typeof value))) return null;
  const numeric = typeof value === "number" ? value : Number(value.trim().replace(",", "."));
  return Number.isFinite(numeric) && numeric >= 0 ? numeric : null;
}

function sourcingProviderLabel(offer) {
  const source = offer?.data_provenance?.source || offer?.provider || "";
  return {
    averon_demo_store: "Averon Demo Store",
    demo_store_http: "Averon Demo Store",
    local_catalog: "Локальный каталог",
    etm_ipro: "ЭТМ iPRO",
  }[source] || "Поставщик";
}

const PRICE_UNIT_FAMILIES = {
  "шт": "piece", "штука": "piece", "штуки": "piece", "штук": "piece",
  "м": "meter", "m": "meter", "м2": "square_meter", "m2": "square_meter",
  "м3": "cubic_meter", "m3": "cubic_meter", "кг": "kilogram", "kg": "kilogram",
  "т": "tonne", "ton": "tonne", "tonne": "tonne", "тонна": "tonne", "тонны": "tonne", "тонн": "tonne",
  "компл": "set", "комплект": "set", "упак": "pack", "упаковка": "pack",
  "л": "litre", "l": "litre", "litre": "litre", "литр": "litre", "литра": "litre", "литров": "litre",
};

function priceUnitFamily(value) {
  let normalized = String(value ?? "").trim().toLowerCase().replace(/\s+/g, " ");
  if (!normalized) return null;
  normalized = normalized.replaceAll("²", "2").replaceAll("³", "3").replace(/\.$/, "");
  return PRICE_UNIT_FAMILIES[normalized] || null;
}

function priceUnitsCompatible(sourceUnit, priceUnit) {
  const sourceFamily = priceUnitFamily(sourceUnit);
  const priceFamily = priceUnitFamily(priceUnit);
  return sourceFamily !== null && sourceFamily === priceFamily;
}

function estimatedOfferTotal(offer, intent) {
  const quantity = nonNegativeSourcingNumber(intent?.quantity);
  const price = nonNegativeSourcingNumber(offer?.price);
  if (quantity === null || price === null || !priceUnitsCompatible(intent?.unit, offer?.price_unit)) return null;
  return (quantity * price).toFixed(2);
}

function sourcingCount(value) {
  const count = Number(value);
  return Number.isFinite(count) && count > 0 ? Math.floor(count) : 0;
}

function russianPlural(count, one, few, many) {
  const value = Math.abs(Math.trunc(Number(count) || 0));
  const mod100 = value % 100;
  if (mod100 >= 11 && mod100 <= 14) return many;
  const mod10 = value % 10;
  if (mod10 === 1) return one;
  if ([2, 3, 4].includes(mod10)) return few;
  return many;
}

function formatUnpricedSummary(matchedCount, alternativeCount) {
  const parts = [];
  if (matchedCount > 0) parts.push(`${matchedCount} ${russianPlural(matchedCount, "совпадение", "совпадения", "совпадений")}`);
  if (alternativeCount > 0) parts.push(`${alternativeCount} ${russianPlural(alternativeCount, "альтернатива", "альтернативы", "альтернатив")}`);
  return parts.join(" · ");
}

function renderOfferCard(result, compact = false, intent = null) {
  const offer = result.offer || result;
  const decision = result.decision || "";
  const explanation = result.explanation || "";
  const total = estimatedOfferTotal(offer, intent);
  const supplier = sourcingProviderLabel(offer);
  const offerUrl = safeOfferUrl(offer?.url);
  const matched = (result.matched_attributes || []).map((key) => `<span class="matched">Совпало: ${escapeHtml(key)}</span>`).join("");
  const conflicts = (result.conflicting_attributes || []).map((key) => `<span class="conflict">Конфликт: ${escapeHtml(key)}</span>`).join("");
  return `<article class="offer-card ${compact ? "compact" : "recommended"}">
    <div class="offer-card-heading">${renderSourcingDecision(decision)}<b>${offerTitleHtml(offer)}</b></div>
    <div class="offer-price">${formatMoney(offer.price, offer.currency)} <small>${offer.price_unit ? `/ ${escapeHtml(offer.price_unit)}` : "Единица цены не указана"}</small></div>
    <div class="offer-meta"><span>Поставщик: ${escapeHtml(supplier)}</span><span>${escapeHtml(offer.manufacturer || offer.brand || "Производитель не указан")}</span><span>${escapeHtml(offer.article || "Артикул не указан")}</span><span>${escapeHtml(offer.availability_text || (offer.availability === true ? "В наличии" : "Наличие уточняется"))}</span></div>
    ${intent?.quantity ? `<div class="offer-total"><span>Количество: <b>${escapeHtml(String(intent.quantity))} ${escapeHtml(intent.unit || "")}</b></span><span>Расчётная стоимость: <b>${total === null ? "Требует проверки" : formatMoney(total, offer.currency)}</b></span></div>` : ""}
    ${matched || conflicts ? `<div class="offer-evidence">${matched}${conflicts}</div>` : ""}
    ${explanation ? `<p class="offer-explanation">${escapeHtml(explanation)}</p>` : ""}
    ${offerUrl ? `<a class="button text" target="_blank" rel="noopener noreferrer" href="${escapeHtml(offerUrl)}">Открыть у поставщика ↗</a>` : ""}
  </article>`;
}

const understandingAttributeLabels = {
  power:"Мощность, кВт", voltage:"Напряжение, В", current:"Ток, А",
  diameter:"Диаметр, мм", pressure:"Давление, PN", cores:"Число жил",
  cable_section:"Сечение, мм²", protection_class:"Степень защиты",
  dimensions:"Размеры, мм", material:"Материал", mounting_type:"Монтаж",
  features:"Особенности",
};

const understandingFieldLabels = {
  manufacturer:"Производитель", brand:"Бренд", model:"Модель", article:"Артикул",
  product_class:"Категория", normalized_name:"Наименование",
};

function understandingValue(value) {
  if (Array.isArray(value)) return value.join(", ");
  if (value && typeof value === "object") return Object.values(value).join(" ");
  return String(value ?? "");
}

function renderProductUnderstanding(understanding) {
  if (!understanding) return "";
  const baseline = understanding.baseline_intent || {};
  const resolved = understanding.resolved_intent || {};
  const status = understanding.mode === "qwen" ? "AI Studio · Qwen" : "Локальный разбор · Qwen недоступен";
  const resolvedFacts = [
    ["Категория", resolved.product_class],
    ["Производитель", resolved.manufacturer || resolved.brand],
    ["Модель", resolved.model],
    ["Артикул", resolved.article],
    ...Object.entries(resolved.attributes || {}).map(([key, value]) => [understandingAttributeLabels[key] || key, understandingValue(value)]),
  ].filter(([, value]) => String(value ?? "").trim());
  const proposals = (understanding.suggestions || []).filter((item) =>
    ["PREFERRED_AI_INFERENCE", "REJECTED_UNGROUNDED", "SOURCE_LOCKED"].includes(item.resolution)
    && String(item.proposed_value ?? "").trim()
  );
  return `<section class="understanding-panel">
    <div class="understanding-heading"><div><small>Исходные данные</small><b>${escapeHtml(baseline.source_text || "Нет исходного текста")}</b></div><span class="technical-badge">${escapeHtml(status)}</span></div>
    <div class="understanding-source-meta">${baseline.manufacturer ? `<span>${escapeHtml(baseline.manufacturer)}</span>` : ""}${baseline.model ? `<span>${escapeHtml(baseline.model)}</span>` : ""}${baseline.article ? `<span>${escapeHtml(baseline.article)}</span>` : ""}</div>
    <div class="understanding-resolved"><small>Разбор позиции</small>${resolvedFacts.length ? resolvedFacts.map(([label, value]) => `<div><span>${escapeHtml(label)}</span><b>${escapeHtml(String(value))}</b></div>`).join("") : `<p>Характеристики не определены.</p>`}</div>
    ${proposals.length ? `<div class="understanding-suggestions"><small>Предположение AI</small>${proposals.slice(0, 5).map((item) => { const key = String(item.field || "").replace(/^attributes\./, ""); const label = understandingFieldLabels[key] || understandingAttributeLabels[key] || "Характеристика"; return `<span><b>${escapeHtml(label)}:</b> ${escapeHtml(understandingValue(item.proposed_value))}</span>`; }).join("")}</div>` : ""}
  </section>`;
}

function projectRecommendedMatch(item) {
  const offer = item.recommended_offer;
  return offer ? (item.match_results || []).find((match) => match.offer?.offer_id === offer.offer_id) || null : null;
}

function projectMatch(item) {
  const recommended = projectRecommendedMatch(item);
  if (recommended && ["MATCH", "LIKELY_MATCH"].includes(recommended.decision)) return recommended;
  if (item.review_candidate) return item.review_candidate;
  return recommended
    || (item.match_results || []).find((match) => match.decision === "REVIEW")
    || (item.match_results || []).find((match) => match.decision === "REJECT")
    || null;
}

function projectDecision(item) {
  const match = projectMatch(item);
  if (match?.decision) return match.decision;
  return item.offers?.length ? "REVIEW" : "WITHOUT_OFFERS";
}

function projectReason(item) {
  const match = projectMatch(item);
  if (!item.offers?.length) return "Точное предложение не найдено";
  if (match?.decision === "MATCH" || match?.decision === "LIKELY_MATCH") {
    const matched = match.matched_attributes || [];
    return matched.length ? `Совпали: ${matched.join(", ")}` : "Совпали обязательные характеристики";
  }
  if (match?.missing_attributes?.length) return `Не подтверждено: ${match.missing_attributes.join(", ")}`;
  if (match?.conflicting_attributes?.length) return `Конфликт: ${match.conflicting_attributes.join(", ")}`;
  const differences = match?.deterministic_evidence?.preferred_differences || [];
  if (projectDecision(item) === "ALTERNATIVE" && differences.length) return `Альтернатива; отличается: ${differences.join(", ")}`;
  if (projectDecision(item) === "ALTERNATIVE") return "Найдена другая модель или производитель";
  return "Предложение требует проверки";
}

function formatProjectTotals(value, totals, currency) {
  if (value !== null && value !== undefined && value !== "") return formatMoney(value, currency);
  const entries = Object.entries(totals || {});
  return entries.length
    ? entries.map(([code, amount]) => formatMoney(amount, code)).join(", ")
    : "Требует проверки";
}

function sourcingFilterMatches(decision, filterKey) {
  const filter = SOURCING_FILTERS.find((item) => item.key === filterKey);
  return !filter?.decisions || filter.decisions.includes(decision);
}

function renderSourcingFilters(activeFilter) {
  return `<div class="sourcing-filters" role="group" aria-label="Фильтр по статусу">${SOURCING_FILTERS.map((filter) => `<button type="button" class="sourcing-filter ${filter.key === activeFilter ? "active" : ""}" data-filter="${filter.key}" aria-pressed="${filter.key === activeFilter}">${escapeHtml(filter.label)}</button>`).join("")}</div>`;
}

function projectResultView(item) {
  const match = projectMatch(item);
  const offer = match?.offer || item.recommended_offer;
  const decision = match?.decision || projectDecision(item);
  const total = offer && ["MATCH", "LIKELY_MATCH", "ALTERNATIVE"].includes(decision)
    ? estimatedOfferTotal(offer, item.intent)
    : null;
  return {item, offer, decision, total, reason: projectReason(item)};
}

function projectReviewCandidates(item) {
  const candidates = [];
  const seenOfferIds = new Set();
  const add = (candidate) => {
    if (!candidate || candidate.decision === "REJECT" || !candidate.offer) return;
    const offerId = String(candidate.offer.offer_id || "").trim();
    if (offerId && seenOfferIds.has(offerId)) return;
    if (offerId) seenOfferIds.add(offerId);
    candidates.push(candidate);
  };
  add(item.review_candidate);
  (item.match_results || [])
    .slice()
    .sort((left, right) => Number(left.rank ?? Number.MAX_SAFE_INTEGER) - Number(right.rank ?? Number.MAX_SAFE_INTEGER))
    .forEach(add);
  return candidates.slice(0, 5);
}

function projectHasReviewCandidates(item) {
  return projectDecision(item) === "REVIEW" && projectReviewCandidates(item).length > 0;
}

function renderProjectResultRow(view, itemIndex) {
  const {item, offer, decision, total, reason} = view;
  const alternative = item.review_candidate
    && item.recommended_offer
    && item.recommended_offer.offer_id !== offer?.offer_id
    ? `<small class="project-result-secondary">Альтернатива: ${offerTitleHtml(item.recommended_offer)}</small>`
    : "";
  const inspectCandidates = projectHasReviewCandidates(item)
    ? `<button type="button" class="button text project-review-candidates" data-project-item-index="${itemIndex}">Посмотреть варианты</button>`
    : "";
  return `<div class="project-result-row" role="row">
    <span>${escapeHtml(item.intent.normalized_name || item.intent.source_text)}</span>
    <span>${escapeHtml(item.intent.quantity || "—")}</span>
    <span>${offer ? offerTitleHtml(offer) : "Нет подтверждённого предложения"}${alternative}${inspectCandidates}</span>
    <span>${offer ? formatMoney(offer.price, offer.currency) : "—"}</span>
    <span>${total === null ? (offer ? "Требует проверки" : "—") : formatMoney(total, offer.currency)}</span>
    <span>${renderSourcingDecision(decision, reason)}</span>
    <span>${escapeHtml(offer ? sourcingProviderLabel(offer) : "—")}</span>
  </div>`;
}

function renderProjectSourcingList(result) {
  const activeFilter = SOURCING_FILTERS.some((filter) => filter.key === state.sourcing.projectFilter)
    ? state.sourcing.projectFilter
    : "all";
  state.sourcing.projectFilter = activeFilter;
  const views = (result.results || []).map((item, itemIndex) => ({...projectResultView(item), itemIndex}));
  const visible = views.filter((view) => sourcingFilterMatches(view.decision, activeFilter));
  const header = ["Позиция", "Кол-во", "Предложение", "Цена", "Сумма", "Статус", "Поставщик"]
    .map((label) => `<span>${label}</span>`)
    .join("");
  const body = visible.length
    ? visible.map((view) => renderProjectResultRow(view, view.itemIndex)).join("")
    : `<div class="project-result-empty">В этой категории позиций нет.</div>`;
  return `${renderSourcingFilters(activeFilter)}<div class="sourcing-project-list" role="table"><div class="project-result-grid"><div class="project-result-header" role="row">${header}</div>${body}</div></div>`;
}

function renderProjectItemDetails(projectResult, item) {
  const content = $("#sourcing-content");
  const intent = item.intent || {};
  const candidates = projectReviewCandidates(item);
  const sourceLabel = intent.normalized_name || intent.source_text || "Позиция без исходного текста";
  $("#sourcing-subtitle").textContent = "Проверка позиции";
  content.innerHTML = `<button type="button" class="button text project-results-back">← К результатам подбора</button>
    <h3>Проверка позиции</h3>
    <p class="project-item-source">${escapeHtml(sourceLabel)}</p>
    ${renderProductUnderstanding(item.understanding)}
    <h3>Варианты для проверки</h3>
    <div class="offer-grid">${candidates.map((candidate) => renderOfferCard(candidate, true, intent)).join("")}</div>`;
  $(".project-results-back").addEventListener("click", () => renderSourcingResult(projectResult));
}

function bindSourcingFilters(result) {
  $("#sourcing-content").querySelectorAll(".sourcing-filter").forEach((button) => button.addEventListener("click", () => {
    state.sourcing.projectFilter = button.dataset.filter || "all";
    renderSourcingResult(result);
  }));
}

function bindProjectCandidateActions(result) {
  $("#sourcing-content").querySelectorAll(".project-review-candidates").forEach((button) => button.addEventListener("click", () => {
    const itemIndex = Number(button.dataset.projectItemIndex);
    const item = result.results?.[itemIndex];
    if (item) renderProjectItemDetails(result, item);
  }));
}

function renderSourcingResult(result, row = null) {
  const content = $("#sourcing-content");
  state.sourcing.result = result;
  if (result.positions_total !== undefined) {
    const qwenUsed = (result.results || []).some((item) => item.ai_mode === "qwen");
    $("#sourcing-subtitle").textContent = qwenUsed ? "Интеллектуальный подбор завершён" : "Подбор по каталогу завершён";
    const confirmedTotal = result.confirmed_total ?? result.estimated_total;
    const confirmedTotals = result.confirmed_totals || result.estimated_totals || {};
    const alternativeTotal = result.alternative_total;
    const alternativeTotals = result.alternative_totals || {};
    const confirmedCurrency = result.confirmed_currency || result.currency || "";
    const alternativeCurrency = result.alternative_currency || "";
    const matchedUnpriced = sourcingCount(result.matched_unpriced_count);
    const alternativeUnpriced = sourcingCount(result.alternative_unpriced_count);
    const unitConfirmation = sourcingCount(result.unit_confirmation_count);
    const unresolved = Number(result.unresolved_count ?? ((result.positions_review || 0) + (result.positions_without_offers || 0)));
    const confirmedIncomplete = matchedUnpriced > 0 || unitConfirmation > 0 || unresolved > 0;
    const confirmedLabel = confirmedIncomplete
      ? "Подтверждённая стоимость по позициям с ценой"
      : "Подтверждённая стоимость";
    const unpricedCard = matchedUnpriced || alternativeUnpriced
      ? `<div><small>Без цены</small><b>${escapeHtml(formatUnpricedSummary(matchedUnpriced, alternativeUnpriced))}</b></div>`
      : "";
    const unitConfirmationCard = unitConfirmation
      ? `<div><small>Проверить единицу цены</small><b>${unitConfirmation}</b></div>`
      : "";
    const runMeta = result.run_id ? `<div class="sourcing-run-meta"><span>Поставщик: <b>${escapeHtml(result.provider_label || "Поставщик")}</b></span><span>Версия каталога: <b>${escapeHtml(result.catalog_version || "—")}</b></span><span>Запуск: <b>${escapeHtml(String(result.run_id).slice(0, 10))}</b></span><span>Время: <b>${escapeHtml(formatRecentTimestamp(result.run_completed_at || result.run_created_at))}</b></span></div>` : "";
    content.innerHTML = `${runMeta}${renderSourcingNotices(result.notices)}<div class="sourcing-project-summary"><div><small>Позиции</small><b>${result.positions_processed}/${result.positions_total}</b></div><div><small>Подтверждены</small><b>${result.positions_matched}</b></div><div><small>Альтернативы</small><b>${result.positions_alternatives || 0}</b></div><div><small>Позиции на проверке</small><b>${result.positions_review}</b></div><div><small>Без предложений</small><b>${result.positions_without_offers}</b></div><div><small>${confirmedLabel}</small><b>${formatProjectTotals(confirmedTotal, confirmedTotals, confirmedCurrency)}</b></div><div><small>Стоимость альтернатив</small><b>${formatProjectTotals(alternativeTotal, alternativeTotals, alternativeCurrency)}</b></div>${unpricedCard}${unitConfirmationCard}<div><small>Требуют проверки</small><b>${unresolved}</b></div></div>${renderProjectSourcingList(result)}`;
    bindSourcingFilters(result);
    bindProjectCandidateActions(result);
    return;
  }
  const intent = result.intent || {};
  const recommended = result.match_results?.find((item) => ["MATCH", "LIKELY_MATCH", "ALTERNATIVE"].includes(item.decision));
  const best = (recommended && ["MATCH", "LIKELY_MATCH"].includes(recommended.decision))
    ? recommended
    : result.review_candidate || recommended;
  const alternatives = (result.match_results || []).filter((item) => item !== best && item.decision !== "REJECT").slice(0, 5);
  const quantity = intent.quantity ? `${escapeHtml(intent.quantity)} ${escapeHtml(intent.unit || "")}` : "Количество требует проверки";
  $("#sourcing-subtitle").textContent = result.ai_mode === "qwen" ? "Интеллектуальный подбор завершён" : "Подбор по каталогу завершён";
  content.innerHTML = `${renderSourcingNotices(result.notices)}${renderProductUnderstanding(result.understanding)}<div class="intent-summary"><div><small>Нормализованное наименование</small><b>${escapeHtml(intent.normalized_name || intent.source_text || "Не определено")}</b></div><div><small>Класс</small><b>${escapeHtml(intent.product_class || "Не определён")}</b></div><div><small>Количество</small><b>${quantity}</b></div><div class="intent-badges"><span class="technical-badge">${result.ai_mode === "qwen" ? "Qwen · AI Studio" : "Без AI · резервный режим"}</span>${Object.entries(intent.attributes || {}).map(([key, value]) => `<span class="technical-badge">${escapeHtml(key)}: ${escapeHtml(String(value))}</span>`).join("")}</div></div>${best ? `<h3>Рекомендуемое предложение</h3>${renderOfferCard(best, false, intent)}` : `<div class="sourcing-warning">Подтверждённого совпадения нет. Показаны результаты для проверки.</div>`}${alternatives.length ? `<h3>Альтернативы</h3><div class="offer-grid">${alternatives.map((item) => renderOfferCard(item, true, intent)).join("")}</div>` : ""}`;
}

async function openSourcingForRow(row) {
  state.sourcing.row = row;
  state.sourcing.result = null;
  $("#sourcing-subtitle").textContent = "Анализируем позицию и ищем в каталоге…";
  $("#sourcing-content").innerHTML = `<div class="sourcing-loading"><span class="spinner"></span><b>Ищем в каталоге</b><small>Сравниваем структурированные характеристики</small></div>`;
  $("#sourcing-modal").showModal();
  try {
    const result = await api("/api/sourcing/search", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({row, limit:20})});
    state.sourcing.result = result;
    $("#sourcing-subtitle").textContent = result.ai_mode === "qwen" ? "Интеллектуальный подбор завершён" : "Подбор по каталогу завершён";
    renderSourcingResult(result, row);
  } catch (error) {
    $("#sourcing-subtitle").textContent = "Поиск не выполнен";
    $("#sourcing-content").innerHTML = `<div class="sourcing-warning">${escapeHtml(error.message)}<br><small>Можно продолжить с локальным каталогом после его наполнения.</small></div>`;
  }
}

async function runProjectSourcing(rows, documentId = null) {
  if (!rows.length) { toast("Нет выбранных позиций для подбора", "error"); return; }
  state.sourcing.projectFilter = "all";
  $("#sourcing-subtitle").textContent = "Подбираем предложения для выбранных позиций…";
  $("#sourcing-content").innerHTML = `<div class="sourcing-loading"><span class="spinner"></span><b>Анализируем выбранные позиции</b><small>Позиции обрабатываются последовательно; после анализа выполняется поиск в выбранном каталоге.</small></div>`;
  $("#sourcing-modal").showModal();
  try {
    const url = documentId ? `/api/documents/${documentId}/sourcing/search-all` : "/api/sourcing/search-all";
    const job = await api(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({rows, limit:20})});
    await pollSourcingJob(job.id, rows.length);
  } catch (error) {
    $("#sourcing-subtitle").textContent = "Подбор не выполнен";
    $("#sourcing-content").innerHTML = `<div class="sourcing-warning">${escapeHtml(error.message)}</div>`;
  }
}

async function openProjectSourcing() {
  const rows = state.rows.filter((row) => row.selected && sourcingEligible(row));
  await runProjectSourcing(rows, state.document?.document_id || null);
}

async function openManualProjectSourcing() {
  const selected = state.manual.rows.filter((row) => row.selected !== false);
  if (!selected.length) { toast("Нет выбранных позиций для подбора", "error"); return; }
  const invalid = selected.filter((row) => !manualRowValidation(row).valid);
  renderManualRows();
  if (invalid.length) {
    toast("Исправьте ошибки в выбранных позициях перед подбором", "error");
    const first = invalid[0];
    requestAnimationFrame(() => document.querySelector(`.manual-cell-input[data-manual-id="${CSS.escape(first.id)}"]`)?.focus());
    return;
  }
  await runProjectSourcing(manualRowsForSourcing(), null);
}

async function pollSourcingJob(jobId, expectedTotal) {
  while (true) {
    const job = await api(`/api/jobs/${jobId}`);
    const total = job.total || expectedTotal;
    const current = Math.min(Number(job.current || 0), total || Number(job.current || 0));
    $("#sourcing-subtitle").textContent = "Подбираем предложения";
    if (job.status === "running" || job.status === "queued") {
      $("#sourcing-content").innerHTML = `<div class="sourcing-loading"><span class="spinner"></span><b>${escapeHtml(job.message || "Обрабатываем позиции")}</b><strong>${current} из ${total}</strong><small>Product Understanding → поиск → deterministic matching</small></div>`;
    }
    if (job.status === "completed") {
      $("#sourcing-subtitle").textContent = "Проектный подбор завершён";
      renderSourcingResult(job.result);
      return;
    }
    if (job.status === "failed") throw new Error(job.error || "Ошибка подбора");
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
}

function cellHtml(row, key) {
  if (key === "row_type") return `<td><select class="cell-select" data-id="${row.id}" data-key="row_type">${options(state.config.row_types,row.row_type)}</select></td>`;
  if (key === "status") return `<td><select class="cell-select" data-id="${row.id}" data-key="status">${options(state.config.statuses,row.status)}</select></td>`;
  if (key === "confidence") {
    if (row.confidence === null || row.confidence === undefined || row.confidence === "") {
      return `<td><div class="confidence"><span><i style="width:0%"></i></span>—</div></td>`;
    }
    const value = Number(row.confidence);
    return `<td><div class="confidence"><span><i style="width:${Math.max(0,Math.min(100,value))}%"></i></span>${value.toFixed(0)}%</div></td>`;
  }
  if (key === "page") return `<td><span class="status-pill">${escapeHtml(String(row.page ?? ""))}</span></td>`;
  const value = String(row[key] ?? "");
  if (key === "name" && row.row_type === "semantic_review") {
    const preview = String(row.semantic_review_preview || row.ocr_metadata?.semantic_review_preview || "").trim();
    const label = preview ? `Проверить: ${preview}` : "Проверить: строка не разрешена";
    const parent = continuationParent(row);
    const fragment = continuationFragment(row);
    const parentLabel = parent ? String(parent.name || parent.type_mark || parent.code || `строка ${sourceRowIndex(parent)}`) : "";
    const action = parent && fragment ? `<div class="candidate-actions"><small>Кандидат родителя: ${escapeHtml(parentLabel)}</small><button type="button" class="continuation-accept" data-id="${row.id}">Привязать продолжение</button><button type="button" class="candidate-edit" data-id="${row.id}" data-key="${key}">Оставить на проверке</button></div>` : "";
    return `<td><div class="semantic-review-preview">${escapeHtml(label)}${action}</div><textarea rows="1" class="cell-input" data-id="${row.id}" data-key="${key}">${escapeHtml(value)}</textarea></td>`;
  }
  if (!CRITICAL_FIELDS.includes(key) || !isYandexCriticalRow(row)) {
    return `<td><textarea rows="1" class="cell-input" data-id="${row.id}" data-key="${key}">${escapeHtml(value)}</textarea></td>`;
  }
  const missing = missingCriticalFields(row).includes(key);
  const suspect = numericSuspectFields(row).includes(key);
  const candidate = row.value_candidates?.[key];
  let annotation = "";
  const humanConfirmed = (row.human_verified_fields || []).includes(key);
  const humanRejected = (row.human_rejected_candidates || []).some((item) => item.field === key && String(item.candidate_value) === String(candidate?.value_candidate));
  if (humanConfirmed) {
    annotation = `<small class="human-verified">Проверено пользователем ✓</small>`;
  } else if (humanRejected) {
    annotation = `<small class="critical-warning">Кандидат отклонён пользователем · оставлено на проверке</small>`;
  } else if (candidate?.auto_trusted) {
    annotation = `<small class="human-verified">Проверено локальной ячейкой ✓</small>`;
  } else if (candidate?.value_candidate) {
    annotation = `<div class="secondary-candidate">Проверить · Yandex повторно распознал: <b>${escapeHtml(String(candidate.value_candidate))}</b>
      <div class="candidate-actions"><button type="button" class="candidate-accept" data-id="${row.id}" data-key="${key}">Принять</button><button type="button" class="candidate-reject" data-id="${row.id}" data-key="${key}">Отклонить</button><button type="button" class="candidate-edit" data-id="${row.id}" data-key="${key}">Изменить</button></div></div>`;
  } else if (missing) {
    annotation = `<small class="critical-warning">⚠ ${CRITICAL_LABELS[key]} не распознано</small>`;
  } else if (suspect) {
    annotation = `<small class="critical-warning">⚠ Подозрительное числовое значение</small>`;
  }
  return `<td><div class="critical-cell"><textarea rows="1" class="cell-input" data-id="${row.id}" data-key="${key}">${escapeHtml(value)}</textarea>${annotation}</div></td>`;
}

function options(map, selected) {
  return Object.entries(map).map(([value,label]) => `<option value="${value}" ${value===selected?"selected":""}>${escapeHtml(label)}</option>`).join("");
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;","'":"&#39;",'"':"&quot;"}[char]));
}

function autoHeight(element) {
  element.style.height = "auto";
  element.style.height = `${Math.max(37, element.scrollHeight)}px`;
}

function rowById(id) { return state.rows.find((row) => row.id === id); }

async function selectRow(id) {
  const row = rowById(id); if (!row) return;
  state.activeRowId = id;
  $("#result-body").querySelectorAll("tr").forEach((tr) => tr.classList.toggle("active", tr.dataset.id === id));
  const pageChanged = state.previewPage !== row.page;
  if (pageChanged || !$("#pdf-preview").src) {
    if (pageChanged) setZoom(1);
    state.previewPage = row.page;
    $("#preview-page-label").textContent = `Страница ${row.page}`;
    const image = $("#pdf-preview");
    image.onload = () => positionHighlight(row);
    image.src = `/api/documents/${state.document.document_id}/page/${row.page}?dpi=160`;
  } else positionHighlight(row);
}

function positionHighlight(row) {
  const highlight = $("#row-highlight");
  if (!row?.bbox) { highlight.hidden = true; return; }
  highlight.hidden = false;
  highlight.style.left = `${row.bbox.x * 100}%`;
  highlight.style.top = `${row.bbox.y * 100}%`;
  highlight.style.width = `${row.bbox.width * 100}%`;
  highlight.style.height = `${row.bbox.height * 100}%`;
  requestAnimationFrame(() => highlight.scrollIntoView({block:"center",inline:"nearest",behavior:"smooth"}));
}

function updateSummary() {
  const ready = state.rows.filter((r) => ["recognized","verified","edited"].includes(r.status)).length;
  const review = state.rows.filter((r) => ["review","unrecognized"].includes(r.status)).length;
  const critical = state.rows.reduce((total, row) => total + criticalFieldCount(row), 0);
  const selected = state.rows.filter((r) => r.selected).length;
  $("#summary-total").textContent = state.rows.length;
  $("#summary-ready").textContent = ready;
  $("#summary-critical").textContent = critical;
  $("#summary-review").textContent = review;
  $("#summary-selected").textContent = selected;
  updateExportSafety();
}

function backendExportBlockers() {
  const result = state.result || {};
  const rows = state.rows || [];
  const statuses = Object.values(result.page_statuses || {});
  const blockers = [];
  if (rows.length && result.page_statuses && !statuses.length) {
    blockers.push("Статус страниц отсутствует");
  }
  const statusPages = new Set();
  statuses.forEach((status) => {
    if (!status || typeof status !== "object") return;
    const page = status.page ?? "?";
    statusPages.add(String(page));
    const output = String(status.output_status || "UNKNOWN").toUpperCase();
    const disposition = String(
      status.page_disposition
      || status.diagnostics?.page_disposition?.disposition
      || ""
    ).toUpperCase();
    const pageReasons = Array.isArray(status.blockers)
      ? status.blockers.map((item) => String(item)).filter(Boolean)
      : [];
    if (output !== "USABLE" && disposition !== "CONFIRMED_NON_SPEC") {
      blockers.push(`Страница ${page}: output_status=${output}${pageReasons.length ? ` · ${pageReasons.join(", ")}` : ""}`);
    } else if (output === "USABLE" && pageReasons.length) {
      blockers.push(`Страница ${page}: ${pageReasons.join(", ")}`);
    }
  });
  const rowPages = new Set(
    rows
      .map((row) => row.page)
      .filter((page) => page !== undefined && page !== null)
      .map(String)
  );
  for (const page of rowPages) {
    if (statuses.length && !statusPages.has(page)) blockers.push(`Страница ${page}: статус отсутствует`);
  }
  const unresolved = rows.reduce((total, row) => {
    if (row.selected === false || ["section", "system", "skip"].includes(row.row_type)) return total;
    if (row.row_type === "note" && !row.structured_table) return total;
    return total + criticalFieldCount(row);
  }, 0);
  if (unresolved) blockers.push(`Не проверено критичных значений: ${unresolved}`);
  return [...new Set(blockers)];
}

function updateExportSafety() {
  const node = $("#export-safety");
  if (!node) return;
  const blockers = backendExportBlockers();
  node.classList.toggle("blocked", blockers.length > 0);
  node.textContent = blockers.length
    ? `Экспорт заблокирован: ${blockers.slice(0, 3).join("; ")}`
    : "Backend подтвердил: экспорт разрешён.";
  const button = $("#download-excel");
  if (button) {
    button.disabled = blockers.length > 0;
    button.title = blockers.length ? "Экспорт заблокирован проверками backend" : "Скачать XLSX";
  }
}

function markDirty() {
  state.dirty = true;
  $("#save-button").textContent = "Сохранить правки •";
  updateExportSafety();
}

async function saveRows(showToast = true) {
  if (!state.document || !state.rows.length) return null;
  await api(`/api/documents/${state.document.document_id}/results`, {
    method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({rows:state.rows}),
  });
  const authoritative = await api(`/api/documents/${state.document.document_id}/results`);
  loadResult(authoritative, {announce:false});
  state.dirty = false; $("#save-button").textContent = "Сохранить правки";
  if (showToast) toast("Правки сохранены", "success");
  return authoritative;
}

function copyRows(rows, columns, includeHeader = true) {
  const selected = rows.filter((row) => row.selected && (
    ["item", "component"].includes(row.row_type)
    || (row.row_type === "note" && row.structured_table)
  ));
  if (!selected.length) { toast("Нет выбранных строк", "error"); return; }
  if (!columns.length) { toast("Не выбраны столбцы", "error"); return; }
  const header = columns.map((key) => state.config.columns.find((c)=>c.key===key)?.title || key).join("\t");
  const lines = selected.map((row) => columns.map((key) => String(row[key] ?? "").replace(/\t/g," ").replace(/\n/g," ")).join("\t"));
  const payload = includeHeader ? [header, ...lines] : lines;
  navigator.clipboard.writeText(payload.join("\n"))
    .then(() => toast(`Скопировано строк: ${selected.length}`, "success"))
    .catch(() => toast("Браузер не разрешил доступ к буферу обмена", "error"));
}

function selectedExportColumns() {
  return [...$("#export-columns").children]
    .filter((item) => item.querySelector("input").checked)
    .map((item) => item.dataset.key);
}

function renderExportColumns() {
  const root = $("#export-columns");
  root.innerHTML = state.exportOrder.map((key) => {
    const column = state.config.columns.find((item) => item.key === key);
    return `<div class="column-item" draggable="true" data-key="${key}"><span class="drag-handle">⋮⋮</span><label><input type="checkbox" ${state.exportSelected.has(key)?"checked":""}><span>${escapeHtml(column.title)}</span></label></div>`;
  }).join("");
  let dragging = null;
  root.querySelectorAll(".column-item").forEach((item) => {
    const checkbox = item.querySelector("input");
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.exportSelected.add(item.dataset.key);
      else state.exportSelected.delete(item.dataset.key);
      saveExportPreferences();
    });
    item.addEventListener("dragstart", () => { dragging = item; item.classList.add("dragging"); });
    item.addEventListener("dragend", () => {
      item.classList.remove("dragging"); dragging = null;
      state.exportOrder = [...root.children].map((node)=>node.dataset.key);
      saveExportPreferences();
    });
    item.addEventListener("dragover", (event) => {
      event.preventDefault(); if (!dragging || dragging===item) return;
      const rect=item.getBoundingClientRect(); root.insertBefore(dragging, event.clientY < rect.top+rect.height/2 ? item : item.nextSibling);
    });
  });
}

async function downloadExcel() {
  const columns = selectedExportColumns();
  if (!columns.length) { toast("Выберите хотя бы один столбец", "error"); return; }
  try {
    const authoritative = await saveRows(false);
    if (!authoritative) return;
    const blockers = backendExportBlockers();
    if (blockers.length) {
      updateExportSafety();
      toast(`Экспорт заблокирован: ${blockers.slice(0, 3).join("; ")}`, "error");
      return;
    }
    const payload = {
      columns, rows:state.rows,
      include_headers:$("#export-headers").checked,
      only_exportable:$("#export-items-only").checked,
      review_export:false,
      filename:$("#export-filename").value,
      sheet_name:$("#export-sheet").value,
    };
    const response = await api(`/api/documents/${state.document.document_id}/export`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    const blob = await response.blob();
    const url=URL.createObjectURL(blob); const link=document.createElement("a");
    const disposition=response.headers.get("content-disposition")||"";
    const match=disposition.match(/filename\*=UTF-8''([^;]+)/i) || disposition.match(/filename="?([^";]+)"?/i);
    link.download=match?decodeURIComponent(match[1]):payload.filename;
    link.href=url; link.click(); URL.revokeObjectURL(url);
    $("#export-modal").close(); toast("Excel сформирован", "success");
  } catch (error) { toast(error.message,"error"); }
}

function reviewExportFilename() {
  const filename = ($("#export-filename").value || "averon_import.xlsx").trim();
  const stem = filename.replace(/\.xlsx$/i, "") || "averon_import";
  return /_review$/i.test(stem) ? `${stem}.xlsx` : `${stem}_review.xlsx`;
}

async function downloadReviewExcel() {
  const button = $("#download-review-excel");
  if (button.disabled) return;
  button.disabled = true;
  button.textContent = "Формируем Excel…";
  try {
    if (!state.document) { toast("Сначала откройте документ", "error"); return; }
    if (!state.rows.length) { toast("Нет строк для экспорта", "error"); return; }
    const columns = selectedExportColumns();
    if (!columns.length) { toast("Выберите хотя бы один столбец", "error"); return; }
    const payload = {
      columns, rows:state.rows,
      include_headers:$("#export-headers").checked,
      only_exportable:false,
      review_export:true,
      filename:reviewExportFilename(),
      sheet_name:$("#export-sheet").value,
    };
    const response = await api(`/api/documents/${state.document.document_id}/export`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    const blob = await response.blob();
    const url=URL.createObjectURL(blob); const link=document.createElement("a");
    const disposition=response.headers.get("content-disposition")||"";
    const match=disposition.match(/filename\*=UTF-8''([^;]+)/i) || disposition.match(/filename="?([^";]+)"?/i);
    link.download=match?decodeURIComponent(match[1]):payload.filename;
    link.href=url; link.click(); URL.revokeObjectURL(url);
    $("#export-modal").close(); toast("Проверочный Excel сформирован", "success");
  } catch (error) { toast(error.message,"error"); }
  finally {
    button.disabled = false;
    button.textContent = "Экспорт для проверки";
  }
}

function resetApp() {
  if (state.dirty && !confirm("Несохранённые правки будут потеряны. Продолжить?")) return;
  Object.assign(state,{document:null,selectedPages:new Set(),previewPage:null,crop:null,rows:[],result:null,activeRowId:null,zoom:1,dirty:false});
  localStorage.removeItem("averonCurrentDocument");
  $("#thumbnail-grid").innerHTML=""; $("#new-document-button").hidden=true; setView("upload");
}

function setupEvents() {
  $("#pdf-file").addEventListener("change", (event) => uploadFile(event.target.files[0]));
  $("#open-manual-entry").addEventListener("click", openManualWorkspace);
  $("#manual-back-to-start").addEventListener("click", returnToStartFromManual);
  $("#manual-add-row").addEventListener("click", () => addManualRow());
  $("#manual-paste-list").addEventListener("click", () => $("#manual-paste-modal").showModal());
  $("#manual-paste-apply").addEventListener("click", applyManualPaste);
  $("#manual-clear-draft").addEventListener("click", clearManualDraft);
  $("#manual-project-sourcing-button").addEventListener("click", () => openManualProjectSourcing().catch((error) => toast(error.message, "error")));
  $("#manual-select-all").addEventListener("change", (event) => {
    state.manual.rows.forEach((row) => { row.selected = event.target.checked; });
    saveManualDraft();
    renderManualRows();
  });
  const drop=$("#drop-zone");
  ["dragenter","dragover"].forEach((name)=>drop.addEventListener(name,(e)=>{e.preventDefault();drop.classList.add("drag");}));
  ["dragleave","drop"].forEach((name)=>drop.addEventListener(name,(e)=>{e.preventDefault();drop.classList.remove("drag");}));
  drop.addEventListener("drop",(e)=>uploadFile(e.dataTransfer.files[0]));
  $("#refresh-recent-documents").addEventListener("click",()=>loadRecentDocuments().catch((e)=>toast(e.message,"error")));
  $("#apply-range").addEventListener("click",()=>{try{state.selectedPages=parseRanges($("#page-range").value);updatePageSelection();const p=[...state.selectedPages][0];if(p)showCropPreview(p);}catch(e){toast(e.message,"error");}});
  $("#suggest-pages").addEventListener("click",async()=>{
    const button=$("#suggest-pages"); const old=button.textContent; button.disabled=true; button.textContent="Анализируем…";
    try {
      const job=await api(`/api/documents/${state.document.document_id}/suggest-pages`,{method:"POST"});
      while(true){
        const current=await api(`/api/jobs/${job.id}`);
        button.textContent=current.total?`Анализ ${current.current}/${current.total}`:"Анализируем…";
        if(current.status==="completed"){state.selectedPages=new Set(current.result.pages);updatePageSelection();const p=current.result.pages[0];if(p)showCropPreview(p);toast(`Найдено страниц: ${current.result.pages.length}`,"success");break;}
        if(current.status==="failed")throw new Error(current.error||"Ошибка анализа");
        await new Promise(r=>setTimeout(r,500));
      }
    } catch(e){toast(e.message,"error");} finally {button.disabled=false;button.textContent=old;}
  });
  $("#select-all-pages").addEventListener("click",()=>{state.selectedPages=new Set(Array.from({length:state.document.page_count},(_,i)=>i+1));updatePageSelection();});
  $("#clear-pages").addEventListener("click",()=>{state.selectedPages.clear();updatePageSelection();});
  $("#recognize-button").addEventListener("click",startRecognition);
  $("#new-document-button").addEventListener("click",resetApp);
  $("#settings-button").addEventListener("click",()=>{
    if (String(state.currentUser?.role || "").toLowerCase() !== "admin") return;
    initializeSettings(state.settings || {});
    $("#settings-modal").showModal();
  });
  $("#save-settings").addEventListener("click",saveSettings);
  $("#settings-sourcing-provider").addEventListener("change",updateSourcingProviderFields);
  $("#table-search").addEventListener("input",renderRows); $("#type-filter").addEventListener("change",renderRows); $("#status-filter").addEventListener("change",renderRows);
  $("#review-filter").addEventListener("change",(event)=>{state.reviewFilter=event.target.value;renderRows();});
  $("#save-button").addEventListener("click",()=>saveRows().catch((e)=>toast(e.message,"error")));
  $("#copy-selected").addEventListener("click",()=>copyRows(state.rows,state.config.default_export_columns));
  $("#open-export").addEventListener("click",()=>{renderExportColumns();updateExportSafety();$("#export-modal").showModal();});
  $("#export-items-only").addEventListener("change",updateExportSafety);
  $("#copy-export").addEventListener("click",()=>copyRows(state.rows,selectedExportColumns(),$("#export-headers").checked));
  $("#download-excel").addEventListener("click",downloadExcel);
  $("#download-review-excel").addEventListener("click",downloadReviewExcel);
  $("#project-sourcing-button").addEventListener("click",openProjectSourcing);
  $("#close-sourcing").addEventListener("click",()=>$("#sourcing-modal").close());
  $("#help-button").addEventListener("click",()=>$("#help-modal").showModal()); $("#close-help").addEventListener("click",()=>$("#help-modal").close());
  $("#zoom-in").addEventListener("click",()=>setZoom(Math.min(1.8,state.zoom+.1))); $("#zoom-out").addEventListener("click",()=>setZoom(Math.max(.5,state.zoom-.1)));
  $("#clear-crop").addEventListener("click",()=>{state.crop=null;positionCropBox();$("#clear-crop").hidden=true;});
  $("#crop-button").addEventListener("click",()=>{
    if(!state.previewPage){toast("Сначала выберите страницу","error");return;}
    state.cropSelecting=!state.cropSelecting; $("#crop-preview").classList.toggle("selecting",state.cropSelecting);
    $("#crop-button").textContent=state.cropSelecting?"Выделите область на странице":"Выбрать область";
  });
  setupCropEvents();
  window.addEventListener("resize",()=>{if(state.crop)positionCropBox();});
  window.addEventListener("beforeunload",(event)=>{if(state.dirty){event.preventDefault();event.returnValue="";}});
}

function setZoom(value) {
  state.zoom=value; $("#zoom-label").textContent=`${Math.round(value*100)}%`; $("#pdf-image-wrap").style.transform=`scale(${value})`;
}

function setupCropEvents() {
  const area=$("#crop-preview"), image=$("#crop-image"); let start=null;
  area.addEventListener("pointerdown",(event)=>{
    if(!state.cropSelecting||image.hidden)return;
    const rect=image.getBoundingClientRect(); if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)return;
    start={x:event.clientX,y:event.clientY,rect}; area.setPointerCapture(event.pointerId); event.preventDefault();
  });
  area.addEventListener("pointermove",(event)=>{
    if(!start)return; const {rect}=start; const x1=Math.max(rect.left,Math.min(start.x,event.clientX)); const x2=Math.min(rect.right,Math.max(start.x,event.clientX)); const y1=Math.max(rect.top,Math.min(start.y,event.clientY)); const y2=Math.min(rect.bottom,Math.max(start.y,event.clientY));
    state.crop={x:(x1-rect.left)/rect.width,y:(y1-rect.top)/rect.height,width:(x2-x1)/rect.width,height:(y2-y1)/rect.height}; positionCropBox();
  });
  area.addEventListener("pointerup",()=>{
    if(!start)return; start=null; state.cropSelecting=false; area.classList.remove("selecting"); $("#crop-button").textContent="Изменить область";
    if(state.crop.width<.05||state.crop.height<.05){state.crop=null;positionCropBox();toast("Выделенная область слишком мала","error");}
  });
}

setupEvents();
boot();
