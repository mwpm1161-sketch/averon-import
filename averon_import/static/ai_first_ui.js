/* The AI-first review surface is isolated from the standard OCR UI handlers. */
function renderAIFirstResumeControls() {
  const root = document.querySelector("#ai-first-resume-controls");
  if (!root) return;
  const retryable = (state.aiFirst.data?.pages || []).filter((page) => ["failed", "incomplete", "unknown_outcome"].includes(page.state));
  root.hidden = state.aiFirst.mode === "standard" || retryable.length === 0;
  root.innerHTML = retryable.length ? `<b>Продолжение обработки</b><div class="ai-first-retry-pages">${retryable.map((page) => `<label><input type="checkbox" data-ai-first-retry-page="${page.page_number}" ${state.aiFirst.retryPages.has(page.page_number) ? "checked" : ""}><span>Повторить страницу ${page.page_number}${page.state === "unknown_outcome" ? " · предыдущий исход неизвестен" : ""}${page.safe_error_code ? ` · ${escapeHtml(page.safe_error_code)}` : ""}</span></label>`).join("")}</div><small>Успешные страницы будут восстановлены из защищённого checkpoint. Неизвестный исход может привести к повторной тарификации.</small>` : "";
  root.querySelectorAll("[data-ai-first-retry-page]").forEach((input) => input.addEventListener("change", () => {
    const page = Number(input.dataset.aiFirstRetryPage);
    if (input.checked) state.aiFirst.retryPages.add(page); else state.aiFirst.retryPages.delete(page);
  }));
}

async function startAIFirstRecognition() {
  const pages = [...state.selectedPages].sort((a,b)=>a-b);
  if (!pages.length || !state.document) return;
  const mode = state.aiFirst.mode;
  if (mode === "qwen" && !document.querySelector("#ai-first-live-confirm").checked) {
    toast("Подтвердите отправку страниц в Qwen Vision.", "error");
    return;
  }
  const retryPages = [...state.aiFirst.retryPages].filter((page) => pages.includes(page)).sort((a,b)=>a-b);
  const button = document.querySelector("#recognize-button");
  const priorLabel = button.textContent;
  button.disabled = true;
  button.textContent = "Запускаем AI-first…";
  document.querySelector("#processing-title").textContent = mode === "fake" ? "Проверяем интерфейс на синтетических данных" : "Распознаём документ через Qwen Vision";
  document.querySelector("#processing-message").textContent = "Подготовка локальной обработки страниц…";
  document.querySelector("#cancel-ai-first-button").hidden = false;
  document.querySelector("#cancel-ai-first-button").disabled = false;
  document.querySelector("#processing-progress").style.width = "0%";
  state.aiFirst.jobId = null;
  setView("processing");
  try {
    const job = await api(`/api/documents/${encodeURIComponent(state.document.document_id)}/ai-first/recognize`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        mode, pages, retry_pages:retryPages,
        user_context:document.querySelector("#ai-first-context").value || null,
        live_acknowledged:mode === "qwen" && document.querySelector("#ai-first-live-confirm").checked,
      }),
    });
    state.aiFirst.jobId = job.id;
    await pollAIFirstJob(job.id);
  } catch (error) {
    toast(error.message || "AI-first обработка не выполнена.", "error");
    setView("pages");
  } finally {
    state.aiFirst.jobId = null;
    state.aiFirst.retryPages.clear();
    button.disabled = state.selectedPages.size === 0 || (state.aiFirst.mode === "qwen" && !document.querySelector("#ai-first-live-confirm").checked);
    button.textContent = priorLabel;
    document.querySelector("#cancel-ai-first-button").hidden = true;
    if (mode === "qwen") {
      document.querySelector("#ai-first-live-confirm").checked = false;
      updateAIFirstModeControls();
    }
  }
}

async function pollAIFirstJob(jobId) {
  let queuedPoll = 0;
  while (true) {
    const job = await getJobForPolling(jobId);
    const total = job.total || state.selectedPages.size;
    const percent = total ? Math.round(job.current / total * 100) : 0;
    document.querySelector("#processing-progress").style.width = `${percent}%`;
    document.querySelector("#processing-count").textContent = `${job.current} / ${total}`;
    document.querySelector("#processing-message").textContent = job.message || "Обработка страниц…";
    try {
      const live = await api(`/api/documents/${encodeURIComponent(state.document.document_id)}/ai-first/progress`);
      renderAIFirstLiveProgress(live.progress || {});
    } catch (_) { /* The durable report is loaded after the job finishes. */ }
    if (job.status === "completed") {
      state.aiFirst.data = await api(`/api/documents/${encodeURIComponent(state.document.document_id)}/ai-first`);
      state.document.has_ai_first_result = true;
      document.querySelector("#ai-first-open-saved").hidden = false;
      state.aiFirst.page = Math.max(1, Math.min(state.aiFirst.page || 1, state.document.page_count));
      state.aiFirst.selectedRows.clear();
      renderAIFirstReview();
      setView("ai-first-review");
      const status = job.result?.status;
      toast(status === "cancelled" ? "Обработка отменена. Сохранены доступные страницы и checkpoint." : status === "completed_with_errors" ? "Готово частично. Страницы с ошибками требуют явного продолжения." : "Распознавание сохранено. Сверьте строки с исходным PDF.", status === "completed" ? "success" : "warning");
      return;
    }
    if (job.status === "failed") throw new Error(job.error || "Ошибка AI-first распознавания");
    if (job.status === "expired") throw new Error(job.error || "Задание истекло в очереди. Запустите его повторно.");
    if (job.status === "queued") {
      queuedPoll += 1;
      await new Promise((resolve) => setTimeout(resolve, Math.min(1000 * (2 ** (queuedPoll - 1)), 6000)));
    } else await new Promise((resolve) => setTimeout(resolve, 700));
  }
}

function renderAIFirstLiveProgress(progress) {
  const root = document.querySelector("#ai-first-live-pages");
  root.hidden = false;
  const labels = {pending:"Ожидает",running:"Выполняется",completed:"Готово",incomplete:"Неполный ответ",failed:"Ошибка",unknown_outcome:"Исход неизвестен"};
  root.innerHTML = Object.entries(progress).sort((a,b)=>Number(a[0])-Number(b[0])).map(([number,status]) => `<span class="${["failed","incomplete","unknown_outcome"].includes(status) ? "warning" : status === "completed" ? "success" : ""}">Страница ${escapeHtml(number)} · ${escapeHtml(labels[status] || "Ожидает")}</span>`).join("");
}

function aiFirstDecision(recordId) { return state.aiFirst.data?.review?.decisions?.[recordId] || null; }
function aiFirstPageRecord(pageNumber) { return state.aiFirst.data?.draft?.pages?.find((page) => page.page_number === pageNumber) || null; }

function aiFirstRowIssues(row, page) {
  const issues = [...(page?.issues || []), ...(state.aiFirst.data?.draft?.issues || [])];
  const ids = new Set([row.id, ...(row.raw_cells || []).map((cell) => cell.id)]);
  return issues.filter((issue, index) => issue.record_ids?.some((id) => ids.has(id))
    && issues.findIndex((candidate) => candidate.id === issue.id) === index);
}

function renderAIFirstReview() {
  const data = state.aiFirst.data;
  if (!data?.draft || !state.document) return;
  const draft = data.draft, review = data.review;
  const pageNumbers = draft.expected_page_numbers || draft.pages.map((page) => page.page_number);
  if (!pageNumbers.includes(state.aiFirst.page)) state.aiFirst.page = pageNumbers[0] || 1;
  const page = aiFirstPageRecord(state.aiFirst.page);
  const pageReports = new Map((data.pages || []).map((item) => [item.page_number, item]));
  const sourceRows = (draft.pages || []).flatMap((item) => item.tables.flatMap((table) => table.rows));
  const manualRows = (review.manual_rows || []).filter((row) => row.active);
  const totalRows = sourceRows.length + manualRows.length;
  const reviewedRows = Object.values(review.decisions || {}).filter((decision) => decision.active).length;
  const unresolved = (draft.issues || []).filter((issue) => ["review", "blocking"].includes(issue.severity) && !(review.resolved_issue_ids || []).includes(issue.id)).length;
  const confirmedPages = (review.confirmed_complete_pages || []).length;
  const pageIssues = (page?.issues || []).filter((issue) => ["review", "blocking"].includes(issue.severity) && !(review.resolved_issue_ids || []).includes(issue.id)).length;
  const report = pageReports.get(state.aiFirst.page);
  const pageError = report?.safe_error_code ? ` · Код ошибки: ${report.safe_error_code}` : "";
  document.querySelector("#ai-first-review-status").textContent = `Статус страницы: ${report?.state || page?.status || "нет данных"}${pageError} · ${totalRows} строк · действия сохраняются локально`;
  document.querySelector("#ai-first-review-summary").innerHTML = `<span>Страницы: ${pageNumbers.length} / ${state.document.page_count}</span><span>Строки: ${reviewedRows} решений из ${totalRows}</span><span class="${pageIssues ? "warning" : "success"}">Замечания на странице: ${pageIssues}</span><span>Полнота страниц: ${confirmedPages} / ${state.document.page_count}</span><span class="${unresolved ? "warning" : "success"}">Неразрешённые замечания: ${unresolved}</span><span class="${data.review_ready_for_guard?.ready ? "success" : "warning"}">Export guard: ${data.review_ready_for_guard?.ready ? "проверки пройдены" : "заблокирован"}</span>`;
  const selector = document.querySelector("#ai-first-page-select");
  selector.innerHTML = pageNumbers.map((number) => `<option value="${number}" ${number === state.aiFirst.page ? "selected" : ""}>Страница ${number} · ${pageReports.get(number)?.state || aiFirstPageRecord(number)?.status || "ожидает"}</option>`).join("");
  document.querySelector("#ai-first-pdf-page-label").textContent = `Страница ${state.aiFirst.page}`;
  document.querySelector("#ai-first-pdf-image").src = documentPageImageUrl(state.document.document_id, state.aiFirst.page, 200);
  document.querySelector("#ai-first-prev-page").disabled = pageNumbers.indexOf(state.aiFirst.page) <= 0;
  document.querySelector("#ai-first-next-page").disabled = pageNumbers.indexOf(state.aiFirst.page) >= pageNumbers.length - 1;
  document.querySelector("#ai-first-confirm-page").disabled = !page || !["extracted", "no_specification"].includes(page.status) || (review.confirmed_complete_pages || []).includes(state.aiFirst.page);
  const issueMap = new Map();
  for (const issue of [...(page?.issues || []), ...(draft.issues || [])]) issueMap.set(issue.id, issue);
  const unresolvedIssues = [...issueMap.values()].filter((issue) => ["review", "blocking"].includes(issue.severity) && !(review.resolved_issue_ids || []).includes(issue.id));
  document.querySelector("#ai-first-page-issues").innerHTML = unresolvedIssues.map((issue) => `<div class="ai-first-issue ${issue.severity === "blocking" ? "blocking" : ""}"><div><b>${escapeHtml(issue.code)}</b> · ${escapeHtml(issue.detail)}<div class="ai-first-resolve-row"><input class="input" data-ai-first-issue-note="${escapeHtml(issue.id)}" maxlength="2000" placeholder="Что проверили по PDF?"><button class="button secondary" type="button" data-ai-first-resolve="${escapeHtml(issue.id)}">Разрешить</button></div></div></div>`).join("");
  document.querySelector("#ai-first-page-issues").querySelectorAll("[data-ai-first-resolve]").forEach((button) => button.addEventListener("click", () => {
    const id = button.dataset.aiFirstResolve;
    const detail = document.querySelector(`[data-ai-first-issue-note="${CSS.escape(id)}"]`)?.value || "";
    void postAIFirstReview("resolve-issue", {issue_id:id,detail});
  }));
  renderAIFirstTables(page, issueMap);
  document.querySelector("#ai-first-confirm-selected").disabled = state.aiFirst.selectedRows.size === 0;
  document.querySelector("#ai-first-export-note").textContent = data.review_ready_for_guard?.ready
    ? "Результат сохранён локально. AI-first не запускает Excel или sourcing; действующие export guards остаются обязательными."
    : `Результат сохранён локально. Блокер допуска: ${data.review_ready_for_guard?.code || "проверка страниц или замечаний"}.`;
}

function renderAIFirstTables(page) {
  const root = document.querySelector("#ai-first-table-scroll");
  if (!page) { root.innerHTML = '<div class="ai-first-empty-page">Страница ещё не обработана.</div>'; return; }
  const tables = [...(page.tables || [])];
  const manualRows = (state.aiFirst.data.review.manual_rows || []).filter((row) => row.active && row.page_number === state.aiFirst.page);
  if (!tables.length && ["failed", "incomplete", "no_specification"].includes(page.status)) {
    root.innerHTML = `<div class="ai-first-empty-page">Статус страницы: ${escapeHtml(page.status)}. Проверьте PDF; ошибочные страницы можно повторить только явным выбором на экране страниц.</div>`;
    return;
  }
  if (!tables.length) tables.push({id:"manual-table",columns:["Поз.","Наименование","Артикул","Ед. изм.","Количество","Примечание"].map((raw_header,index)=>({id:`manual-column-${index}`,index,raw_header})),rows:[]});
  root.innerHTML = tables.map((table, tableIndex) => {
    const columns = [...(table.columns || [])].sort((a,b)=>a.index-b.index);
    const rows = [...(table.rows || []).map((row) => ({row,manual:false}))];
    if (tableIndex === 0) for (const row of manualRows) rows.push({row,manual:true});
    const header = columns.map((column) => `<th>${escapeHtml(column.raw_header || `Колонка ${column.index + 1}`)}</th>`).join("");
    const body = rows.map(({row,manual}) => {
      const decision = aiFirstDecision(row.id);
      const rowIssues = manual ? [] : aiFirstRowIssues(row, page);
      const flagged = rowIssues.some((issue) => ["review", "blocking"].includes(issue.severity) && !(state.aiFirst.data.review.resolved_issue_ids || []).includes(issue.id));
      const excluded = decision?.active && decision.disposition === "excluded";
      const cells = columns.map((column) => {
        const key = `cell:${column.index}`;
        const cell = manual ? null : (row.raw_cells || []).find((item) => item.column_index === column.index);
        const source = manual ? row.values?.[key] || "" : cell?.raw_value ?? "";
        const edited = decision?.disposition === "corrected" && Object.prototype.hasOwnProperty.call(decision.values || {}, key);
        const shown = edited ? decision.values[key] : source;
        const sourceNote = edited && decision.values[key] !== source ? `<small class="ai-first-cell-source">Исходно: ${escapeHtml(source || "пусто")}</small>` : "";
        const uncertain = !manual && (cell?.unreadable || cell?.struck_through || cell?.handwritten || cell?.alternatives?.length);
        return `<td class="${flagged || uncertain ? "ai-first-row-flagged" : ""}"><input class="ai-first-cell-input" data-ai-first-cell="${escapeHtml(row.id)}" data-column-index="${column.index}" aria-label="${escapeHtml(column.raw_header || `Колонка ${column.index + 1}`)}" value="${escapeHtml(shown)}">${sourceNote}${uncertain ? `<small class="ai-first-cell-source">Неопределённость${cell?.alternatives?.length ? ` · ${escapeHtml(cell.alternatives.join(" / "))}` : ""}</small>` : ""}</td>`;
      }).join("");
      const label = manual ? "ручная позиция" : (row.model_kind || "тип не указан");
      const decisionLabel = excluded ? "исключена" : decision?.active ? (decision.disposition === "corrected" ? "исправлена" : "подтверждена") : "требует проверки";
      return `<tr class="${flagged ? "is-flagged" : ""} ${excluded ? "is-excluded" : ""}"><td><input type="checkbox" data-ai-first-select-row="${escapeHtml(row.id)}" aria-label="Выбрать строку" ${state.aiFirst.selectedRows.has(row.id) ? "checked" : ""}></td><td>${manual ? "ручная" : row.index + 1}<small class="ai-first-cell-source">${escapeHtml(label)} · ${decisionLabel}</small></td>${cells}<td><div class="ai-first-row-actions"><button class="button secondary" type="button" data-ai-first-save-row="${escapeHtml(row.id)}">Сохранить правки</button><button class="button ghost" type="button" data-ai-first-confirm-row="${escapeHtml(row.id)}">Подтвердить</button><button class="button text" type="button" data-ai-first-exclude-row="${escapeHtml(row.id)}">${excluded ? "Восстановить" : "Исключить"}</button></div></td></tr>`;
    }).join("");
    const addForm = tableIndex === 0 && state.aiFirst.addRowOpen ? `<div class="ai-first-add-form"><b>Новая ручная позиция · страница ${state.aiFirst.page}</b><div class="ai-first-add-fields">${columns.map((column) => `<label><span>${escapeHtml(column.raw_header || `Колонка ${column.index + 1}`)}</span><input class="input" data-ai-first-new-cell="${column.index}" maxlength="2000"></label>`).join("")}</div><button class="button primary" type="button" id="ai-first-add-row-submit">Добавить позицию с записью в аудит</button></div>` : "";
    return `${addForm}<div class="ai-first-table-caption">Таблица ${tableIndex + 1} · исходные колонки сохранены</div><table class="ai-first-table"><thead><tr><th>Выбор</th><th>Строка</th>${header}<th>Решение</th></tr></thead><tbody>${body || `<tr><td colspan="${columns.length + 3}" class="ai-first-empty-page">В таблице пока нет строк.</td></tr>`}</tbody></table>`;
  }).join("");
  root.querySelectorAll("[data-ai-first-select-row]").forEach((input) => input.addEventListener("change", () => {
    const id = input.dataset.aiFirstSelectRow;
    if (input.checked) state.aiFirst.selectedRows.add(id); else state.aiFirst.selectedRows.delete(id);
    document.querySelector("#ai-first-confirm-selected").disabled = state.aiFirst.selectedRows.size === 0;
  }));
  root.querySelectorAll("[data-ai-first-save-row]").forEach((button) => button.addEventListener("click", () => {
    const id = button.dataset.aiFirstSaveRow, values = {};
    root.querySelectorAll("[data-ai-first-cell]").forEach((input) => { if (input.dataset.aiFirstCell === id) values[`cell:${input.dataset.columnIndex}`] = input.value; });
    void postAIFirstReview("row", {record_id:id,disposition:"corrected",values});
  }));
  root.querySelectorAll("[data-ai-first-confirm-row]").forEach((button) => button.addEventListener("click", () => void postAIFirstReview("row", {record_id:button.dataset.aiFirstConfirmRow,disposition:"confirmed",values:{}})));
  root.querySelectorAll("[data-ai-first-exclude-row]").forEach((button) => button.addEventListener("click", () => {
    const id = button.dataset.aiFirstExcludeRow, current = aiFirstDecision(id);
    const disposition = current?.active && current.disposition === "excluded" ? "confirmed" : "excluded";
    if (disposition === "excluded" && !confirm("Исключить строку из проверенной спецификации? Решение сохранится в аудите.")) return;
    void postAIFirstReview("row", {record_id:id,disposition,values:{}});
  }));
  root.querySelectorAll("[data-ai-first-resolve]").forEach((button) => button.addEventListener("click", () => {
    const id = button.dataset.aiFirstResolve;
    const detail = document.querySelector(`[data-ai-first-issue-note="${CSS.escape(id)}"]`)?.value || "";
    void postAIFirstReview("resolve-issue", {issue_id:id,detail});
  }));
  const add = document.querySelector("#ai-first-add-row-submit");
  if (add) add.addEventListener("click", () => {
    const values = {};
    root.querySelectorAll("[data-ai-first-new-cell]").forEach((input) => { values[`cell:${input.dataset.aiFirstNewCell}`] = input.value; });
    void postAIFirstReview("add-row", {page_number:state.aiFirst.page,values});
  });
}

async function postAIFirstReview(action, body) {
  try {
    await api(`/api/documents/${encodeURIComponent(state.document.document_id)}/ai-first/review/${action}`, {
      method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body),
    });
    state.aiFirst.data = await api(`/api/documents/${encodeURIComponent(state.document.document_id)}/ai-first`);
    renderAIFirstReview();
    toast("Решение сохранено локально и добавлено в аудит.", "success");
  } catch (error) { toast(error.message || "Не удалось сохранить решение.", "error"); }
}

async function confirmSelectedAIFirstRows() {
  const recordIds = [...state.aiFirst.selectedRows];
  if (!recordIds.length || !confirm(`Подтвердить ${recordIds.length} выбранных строк после сверки с PDF?`)) return;
  await postAIFirstReview("bulk-confirm", {record_ids:recordIds});
  state.aiFirst.selectedRows.clear();
  renderAIFirstReview();
}

async function cancelAIFirstRecognition() {
  if (!state.aiFirst.jobId) return;
  const button = document.querySelector("#cancel-ai-first-button");
  button.disabled = true;
  try {
    await api(`/api/jobs/${encodeURIComponent(state.aiFirst.jobId)}/cancel`, {method:"POST"});
    document.querySelector("#processing-message").textContent = "Запрошена отмена. Ожидаем завершения текущей страницы и сохранения checkpoint…";
  } catch (error) {
    button.disabled = false;
    toast(error.message || "Не удалось отменить обработку.", "error");
  }
}

async function saveAIFirstReview() {
  try {
    await api(`/api/documents/${encodeURIComponent(state.document.document_id)}/ai-first/review/save`, {method:"POST"});
    toast("Проверенный результат и история исправлений сохранены локально.", "success");
  } catch (error) { toast(error.message || "Не удалось сохранить результат.", "error"); }
}

function installAIFirstUIEvents() {
  const mode = document.querySelector("#ai-first-mode");
  mode.addEventListener("change", () => {
    const next = mode.value;
    if (next !== "standard" && state.aiFirst.mode === "standard") state.aiFirst.previousPages = new Set(state.selectedPages);
    if (next === "standard" && state.aiFirst.mode !== "standard" && state.aiFirst.previousPages) state.selectedPages = new Set(state.aiFirst.previousPages);
    state.aiFirst.mode = next;
    state.aiFirst.retryPages.clear();
    if (next !== "standard") selectAllPagesForAIFirst(); else updatePageSelection();
    document.querySelector("#ai-first-live-confirm").checked = false;
    updateAIFirstModeControls();
  });
  document.querySelector("#ai-first-context").addEventListener("input", () => {
    document.querySelector("#ai-first-context-count").textContent = `${document.querySelector("#ai-first-context").value.length} / 1000`;
  });
  document.querySelector("#ai-first-live-confirm").addEventListener("change", updateAIFirstModeControls);
  document.querySelector("#cancel-ai-first-button").addEventListener("click", () => void cancelAIFirstRecognition());
  document.querySelector("#ai-first-back-to-pages").addEventListener("click", () => { setView("pages"); renderAIFirstResumeControls(); });
  document.querySelector("#ai-first-save-review").addEventListener("click", () => void saveAIFirstReview());
  document.querySelector("#ai-first-confirm-selected").addEventListener("click", () => void confirmSelectedAIFirstRows());
  document.querySelector("#ai-first-confirm-page").addEventListener("click", async () => {
    await postAIFirstReview("complete-page", {page_number:state.aiFirst.page});
  });
  document.querySelector("#ai-first-add-row").addEventListener("click", () => {
    state.aiFirst.addRowOpen = !state.aiFirst.addRowOpen;
    renderAIFirstReview();
    document.querySelector("#ai-first-add-row").textContent = state.aiFirst.addRowOpen ? "Отменить добавление" : "+ Добавить позицию";
  });
  document.querySelector("#ai-first-page-select").addEventListener("change", (event) => {
    state.aiFirst.page = Number(event.target.value);
    state.aiFirst.selectedRows.clear();
    renderAIFirstReview();
  });
  document.querySelector("#ai-first-prev-page").addEventListener("click", () => changeAIFirstPage(-1));
  document.querySelector("#ai-first-next-page").addEventListener("click", () => changeAIFirstPage(1));
  document.querySelector("#ai-first-open-saved").addEventListener("click", async () => {
    if (!state.document) return;
    try {
      const data = await api(`/api/documents/${encodeURIComponent(state.document.document_id)}/ai-first`);
      state.aiFirst.data = data;
      const modeValue = data.mode === "fake_vision" ? "fake" : data.mode === "qwen_vision" ? "qwen" : "standard";
      if (modeValue !== "standard" && !document.querySelector(`#ai-first-mode option[value="${modeValue}"]`)?.hidden) {
        document.querySelector("#ai-first-mode").value = modeValue;
        state.aiFirst.mode = modeValue;
        updateAIFirstModeControls();
      }
      state.aiFirst.page = data.draft.expected_page_numbers?.[0] || 1;
      renderAIFirstReview();
      setView("ai-first-review");
    } catch (error) { toast(error.message || "Не удалось открыть AI-first результат.", "error"); }
  });
}

function changeAIFirstPage(delta) {
  const pages = state.aiFirst.data?.draft?.expected_page_numbers || [];
  const index = pages.indexOf(state.aiFirst.page);
  const next = pages[index + delta];
  if (next) { state.aiFirst.page = next; state.aiFirst.selectedRows.clear(); renderAIFirstReview(); }
}

document.addEventListener("DOMContentLoaded", installAIFirstUIEvents, {once:true});
