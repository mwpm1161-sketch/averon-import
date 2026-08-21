(() => {
  const FIELD_LABELS = {
    position: "Позиция",
    name: "Наименование",
    type_mark: "Тип / марка",
    code: "Код",
    manufacturer: "Производитель",
    unit: "Ед. изм.",
    quantity: "Количество",
    mass: "Масса",
    note: "Примечание",
  };

  let aiHealth = null;
  let pendingSuggestion = null;
  let pendingRowId = null;

  function ensureUi() {
    const toolbar = document.querySelector(".review-toolbar");
    if (!toolbar || document.querySelector("#ai-review-row")) return;

    const button = document.createElement("button");
    button.className = "button ghost";
    button.id = "ai-review-row";
    button.type = "button";
    button.textContent = "AI-проверка строки";
    button.hidden = true;
    const save = document.querySelector("#save-button");
    toolbar.insertBefore(button, save || null);
    button.addEventListener("click", reviewActiveRow);

    const dialog = document.createElement("dialog");
    dialog.className = "modal";
    dialog.id = "ai-review-modal";
    dialog.innerHTML = `
      <div class="modal-card">
        <div class="modal-header">
          <div><span class="eyebrow">Локальный AI</span><h2>Проверка строки</h2>
          <p id="ai-review-meta">Предложение модели не применяется автоматически.</p></div>
          <button class="icon-button" type="button" id="ai-review-close" aria-label="Закрыть">×</button>
        </div>
        <div id="ai-review-content"></div>
        <div class="modal-footer">
          <button class="button ghost" type="button" id="ai-review-cancel">Оставить как есть</button>
          <button class="button primary" type="button" id="ai-review-apply">Принять предложения</button>
        </div>
      </div>`;
    document.body.appendChild(dialog);
    dialog.querySelector("#ai-review-close").addEventListener("click", () => dialog.close());
    dialog.querySelector("#ai-review-cancel").addEventListener("click", () => dialog.close());
    dialog.querySelector("#ai-review-apply").addEventListener("click", applySuggestion);
  }

  function escape(value) {
    return String(value ?? "").replace(/[&<>\"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '\"': "&quot;", "'": "&#39;",
    }[char]));
  }

  async function refreshAiHealth() {
    ensureUi();
    const button = document.querySelector("#ai-review-row");
    try {
      const health = await api("/api/health");
      aiHealth = health.ai || null;
      const enabled = Boolean(aiHealth?.enabled);
      button.hidden = !enabled;
      button.disabled = !aiHealth?.available;
      button.title = aiHealth?.available
        ? `Локальный AI: ${aiHealth.model || "готов"}`
        : enabled ? "AI включён, но модель/Ollama пока недоступны" : "AI выключен";
    } catch (_) {
      button.hidden = true;
    }
  }

  async function reviewActiveRow() {
    const row = rowById(state.activeRowId);
    if (!row || !state.document) {
      toast("Сначала выберите строку", "error");
      return;
    }
    const button = document.querySelector("#ai-review-row");
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "AI проверяет…";
    try {
      const suggestion = await api(
        `/api/documents/${state.document.document_id}/rows/${row.id}/ai-review`,
        {method: "POST"},
      );
      showSuggestion(row, suggestion);
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = !aiHealth?.available;
      button.textContent = original;
    }
  }

  function showSuggestion(row, suggestion) {
    pendingSuggestion = suggestion;
    pendingRowId = row.id;
    const content = document.querySelector("#ai-review-content");
    const changes = Object.entries(suggestion.fields || {})
      .filter(([key, value]) => key in FIELD_LABELS && value !== null && String(value) !== String(row[key] ?? ""));
    const uncertain = new Set(suggestion.uncertain_fields || []);

    if (!changes.length && !uncertain.size) {
      content.innerHTML = `<div class="export-note"><b>Изменений не предложено</b><p>${escape(suggestion.notes || "Модель не нашла обоснованных исправлений.")}</p></div>`;
      document.querySelector("#ai-review-apply").disabled = true;
    } else {
      const rows = changes.map(([key, value]) => `
        <div class="export-note">
          <b>${escape(FIELD_LABELS[key])}${uncertain.has(key) ? " · требует проверки" : ""}</b>
          <p><strong>Сейчас:</strong> ${escape(row[key] || "—")}</p>
          <p><strong>AI:</strong> ${escape(value || "—")}</p>
        </div>`).join("");
      const uncertainOnly = [...uncertain]
        .filter((key) => !changes.some(([changed]) => changed === key))
        .map((key) => `<p>Не уверен: <b>${escape(FIELD_LABELS[key] || key)}</b></p>`).join("");
      content.innerHTML = `${rows}${uncertainOnly ? `<div class="export-note">${uncertainOnly}</div>` : ""}${suggestion.notes ? `<p class="hint">${escape(suggestion.notes)}</p>` : ""}`;
      document.querySelector("#ai-review-apply").disabled = changes.length === 0;
    }
    document.querySelector("#ai-review-meta").textContent = `${suggestion.provider || "AI"} · ${suggestion.model || "локальная модель"}. Ничего не меняется без вашего подтверждения.`;
    document.querySelector("#ai-review-modal").showModal();
  }

  function applySuggestion() {
    const row = rowById(pendingRowId);
    if (!row || !pendingSuggestion) return;
    let changed = false;
    for (const [key, value] of Object.entries(pendingSuggestion.fields || {})) {
      if (!(key in FIELD_LABELS) || value === null || String(value) === String(row[key] ?? "")) continue;
      row[key] = value;
      if (row.ocr_evidence?.[key]) row.ocr_evidence[key].final_text = value;
      changed = true;
    }
    if (changed) {
      row.status = "edited";
      row.edited = true;
      markDirty();
      renderRows();
      updateSummary();
      selectRow(row.id);
      toast("Предложения AI применены. Проверьте строку по PDF.", "success");
    }
    document.querySelector("#ai-review-modal").close();
    pendingSuggestion = null;
    pendingRowId = null;
  }

  document.addEventListener("DOMContentLoaded", refreshAiHealth, {once: true});
})();
