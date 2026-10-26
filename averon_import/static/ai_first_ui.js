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
      const hasSuccessfulPage = (state.aiFirst.data.pages || []).some((page) => page.state === "completed");
      document.querySelector("#ai-first-open-saved").hidden = !hasSuccessfulPage;
      state.aiFirst.page = Math.max(1, Math.min(state.aiFirst.page || 1, state.document.page_count));
      state.aiFirst.selectedRows.clear();
      const result = job.result || {};
      if ((result.completed_pages || 0) === 0 && result.status !== "completed") {
        renderAIFirstRunFeedback(state.aiFirst.data, true);
        setView("pages");
        toast("Ни одна страница не распознана. Документ не открыт как готовая проверка; изучите причины и повторите только после устранения ошибки.", "error");
        return;
      }
      renderAIFirstRunFeedback(state.aiFirst.data);
      renderAIFirstReview();
      setView("ai-first-review");
      const status = result.status;
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

function aiFirstRowInputSnapshot(recordId) {
  const sourceRow = (state.aiFirst.data?.draft?.pages || []).flatMap((page) => (page.tables || []).flatMap((table) => table.rows || [])).find((row) => row.id === recordId);
  const manualRow = (state.aiFirst.data?.review?.manual_rows || []).find((row) => row.id === recordId && row.active);
  const sourceValues = {};
  if (manualRow) Object.assign(sourceValues, manualRow.values || {});
  else for (const cell of sourceRow?.raw_cells || []) sourceValues[`cell:${cell.column_index}`] = cell.raw_value || "";
  const decision = aiFirstDecision(recordId);
  return [...document.querySelectorAll("#ai-first-table-scroll [data-ai-first-cell]")]
    .filter((input) => input.dataset.aiFirstCell === recordId)
    .map((input) => {
      const key = `cell:${input.dataset.columnIndex}`;
      const retainedAfterRoleChange = decision?.invalidation_reason === "role_change";
      const baseline = (decision?.active || retainedAfterRoleChange) && Object.prototype.hasOwnProperty.call(decision.values || {}, key)
        ? decision.values[key] : (sourceValues[key] || "");
      return {key, label:input.getAttribute("aria-label") || key, value:input.value, baseline};
    });
}

function aiFirstRowValues(snapshot) {
  return Object.fromEntries(snapshot.map((item) => [item.key, item.value]));
}

function aiFirstRowConfirmationPayload(snapshot) {
  const changed = snapshot.some((item) => item.value !== item.baseline);
  return {disposition:changed ? "corrected" : "confirmed",confirmed:true,values:changed ? aiFirstRowValues(snapshot) : {}};
}

function aiFirstRowIssues(row, page) {
  const pageNumber = Number(page?.page_number || row.page_number);
  const issues = [...(page?.issues || []), ...(state.aiFirst.data?.draft?.issues || [])];
  const ids = new Set([row.id, row.table_id, ...(row.raw_cells || []).map((cell) => cell.id)].filter(Boolean));
  const resolved = new Set(state.aiFirst.data?.review?.resolved_issue_ids || []);
  return issues.filter((issue, index) => {
    if (issue.code === "page_extraction_failed" || !["review", "blocking"].includes(issue.severity) || resolved.has(issue.id)) return false;
    if (issue.page_number != null && Number(issue.page_number) !== pageNumber) return false;
    const references = issue.record_ids || [];
    if (references.length && !references.some((id) => ids.has(id))) return false;
    return issues.findIndex((candidate) => candidate.id === issue.id) === index;
  });
}

function aiFirstRowReviewState(entry) {
  const {row,page,manual=false} = entry;
  const review = state.aiFirst.data?.review || {};
  const role = review.row_roles?.[row.id];
  const decision = aiFirstDecision(row.id);
  const pageNumber = Number(page?.page_number || row.page_number);
  const report = aiFirstReportPages(state.aiFirst.data).get(pageNumber);
  const pageReady = report?.state === "completed" && ["extracted", "no_specification"].includes(page?.status);
  const tableId = entry.table?.id || row.table_id || null;
  const roleVerified = Boolean(role?.active && role.role !== "unknown"
    && role.source_document_id === state.aiFirst.data?.draft?.source_document_id
    && role.page_number === pageNumber && role.page_extraction_fingerprint === page?.extraction_fingerprint
    && (tableId ? role.table_id === tableId : role.table_id == null));
  const excluded = Boolean(decision?.active && decision.disposition === "excluded");
  const decisionCurrent = Boolean(decision?.active && !decision.invalidation_reason
    && decision.source_document_id === state.aiFirst.data?.draft?.source_document_id && decision.page_number === pageNumber
    && decision.human_confirmed !== false && ["confirmed", "corrected"].includes(decision.disposition));
  const openIssues = aiFirstRowIssues(row, page);
  const cells = row.raw_cells || [];
  const hasUncertainty = cells.some((cell) => cell.unreadable || cell.struck_through || cell.handwritten
    || cell.alternatives?.length || cell.handwritten_replacements?.length);
  const evidenceIssueExists = !hasUncertainty || cells.filter((cell) => cell.unreadable || cell.struck_through || cell.handwritten
    || cell.alternatives?.length || cell.handwritten_replacements?.length).every((cell) => {
      const references = new Set([row.id, cell.id, tableId].filter(Boolean));
      return [...(page?.issues || []), ...(state.aiFirst.data?.draft?.issues || [])].some((issue) =>
        ["review", "blocking"].includes(issue.severity)
        && (issue.page_number == null || Number(issue.page_number) === pageNumber)
        && (!(issue.record_ids || []).length || issue.record_ids.some((id) => references.has(id))));
    });
  const pendingValues = state.aiFirst.pendingRowValues?.[row.id];
  const unsaved = !manual && (Object.keys(pendingValues || {}).length > 0
    || aiFirstRowInputSnapshot(row.id).some((item) => item.value !== item.baseline));
  const roleInput = document.querySelector(`#ai-first-table-scroll [data-ai-first-row-role="${CSS.escape(row.id)}"]`);
  const parentInput = document.querySelector(`#ai-first-table-scroll [data-ai-first-parent-row="${CSS.escape(row.id)}"]`);
  const roleBaseline = role?.active ? role.role : "unknown";
  const pendingRole = state.aiFirst.pendingRowRoles?.[row.id];
  const roleUnsaved = Boolean(pendingRole || (roleInput && (roleInput.value !== roleBaseline
    || (roleInput.value === "component" && parentInput && parentInput.value !== (role?.parent_record_id || "")))));
  const hasBlockingState = !pageReady || openIssues.length > 0 || !roleVerified || !evidenceIssueExists
    || (!excluded && (!decisionCurrent || (hasUncertainty && !decisionCurrent)));
  let status = "attention", label = "Требует проверки", reason = "";
  if (unsaved || roleUnsaved) { status = "unsaved"; label = "Есть несохранённые изменения"; }
  else if (openIssues.length) { status = "attention"; label = "Есть открытое замечание"; }
  else if (!pageReady) { status = "attention"; label = "Страница требует обработки"; }
  else if (excluded) { status = "neutral"; label = "Исключена · сохранена для аудита"; }
  else if (!roleVerified) { status = "attention"; label = "Роль не подтверждена"; }
  else if (!evidenceIssueExists) { status = "attention"; label = "Неопределённость источника требует замечания и проверки"; }
  else if (["header", "column_numbering", "section", "note", "continuation"].includes(role.role)) {
    status = "neutral"; label = `${({header:"Заголовок",column_numbering:"Нумерация колонок",section:"Раздел",note:"Примечание",continuation:"Продолжение строки"})[role.role]} · роль подтверждена`;
  }
  else if (decision?.invalidation_reason || (decision && !decision.active)) { status = "attention"; label = "Решение устарело"; }
  else if (!excluded && !decisionCurrent) {
    status = "attention";
    label = decision?.active && decision.disposition === "corrected" && decision.human_confirmed === false
      ? "Правки сохранены · подтвердите значения"
      : hasUncertainty ? "Сверьте исходные пометки и подтвердите значения" : "Значения не проверены";
  }
  else { status = "checked"; label = "Значения проверены"; }
  const pageComplete = (review.confirmed_complete_pages || []).includes(pageNumber);
  const exportReady = !hasBlockingState && !unsaved && !roleUnsaved && role?.role === "standalone_item"
    && decisionCurrent && !openIssues.length && pageComplete && page?.status === "extracted";
  return {status,label,reason,role,excluded,roleVerified,decisionCurrent,openIssues,hasUncertainty,pageReady,pageComplete,evidenceIssueExists,exportReady};
}

function aiFirstRowStatusLabel(reviewState) {
  if (reviewState.status !== "checked") return reviewState.label;
  if (reviewState.exportReady) return "Значения проверены · допущена в тестовый XLSX";
  if (!reviewState.pageComplete) return "Значения проверены · полнота страницы не подтверждена";
  if (reviewState.role?.role === "component") return "Значения проверены · компонент комплекта";
  if (reviewState.role?.role !== "standalone_item") return "Значения проверены · не самостоятельная позиция";
  return "Значения проверены · допуск к тестовому XLSX не подтверждён";
}

function updateAIFirstRenderedRowState(recordId) {
  const root = document.querySelector("#ai-first-table-scroll");
  const rowElement = root?.querySelector(`[data-ai-first-row="${CSS.escape(recordId)}"]`);
  if (!rowElement) return;
  const entry = aiFirstDocumentEntries(state.aiFirst.data.draft, state.aiFirst.data.review)
    .find((item) => item.row.id === recordId);
  if (!entry) return;
  const reviewState = aiFirstRowReviewState(entry);
  const excluded = aiFirstDecision(recordId)?.active && aiFirstDecision(recordId)?.disposition === "excluded";
  rowElement.className = `ai-first-row--${reviewState.status} ${excluded ? "is-excluded" : ""} ${Number(rowElement.dataset.pageNumber) === state.aiFirst.page ? "active" : ""}`;
  rowElement.dataset.reviewState = reviewState.status;
  const label = rowElement.querySelector("[data-ai-first-row-status]");
  if (label) label.textContent = aiFirstRowStatusLabel(reviewState);
  for (const input of rowElement.querySelectorAll("[data-ai-first-cell]")) {
    const cell = input.closest("td");
    if (!cell) continue;
    cell.classList.remove("ai-first-cell--attention", "ai-first-cell--checked", "ai-first-cell--unsaved");
    if (reviewState.status === "attention") cell.classList.add("ai-first-cell--attention");
    else if (reviewState.status === "checked") cell.classList.add("ai-first-cell--checked");
    else if (reviewState.status === "unsaved") cell.classList.add("ai-first-cell--unsaved");
  }
}

function aiFirstProcessingMessage(code) {
  const messages = {
    image_pixel_limit:"Страница слишком велика для безопасной обработки с текущим разрешением.",
    image_byte_limit:"Изображение страницы превышает допустимый размер.",
    page_render_failed:"Не удалось подготовить изображение страницы.",
    page_dimensions_invalid:"Не удалось определить размер страницы PDF.",
    page_extraction_failed:"Не удалось распознать содержимое страницы.",
    transcription_incomplete:"Ответ распознавания неполный; доступные данные требуют проверки.",
    adapter_outcome_unknown:"Не удалось достоверно получить результат распознавания.",
    page_not_processed:"Обработка страницы не была завершена.",
    page_result_missing:"В результате нет данных по странице.",
    request_limit_exhausted:"Достигнут настроенный лимит запросов.",
    budget_exhausted:"Достигнут настроенный лимит расходов.",
    pending:"Страница ожидает запуска обработки.",
  };
  return messages[code] || "Страницу не удалось обработать. Проверьте PDF и повторите обработку.";
}

function aiFirstIssueMessage(code) {
  const messages = {
    row_has_uncertain_visual_evidence:"В строке есть нечитаемый фрагмент, зачёркивание или рукописная пометка. Сверьте ячейки с PDF.",
    continuation_unverified:"Возможное продолжение таблицы с другой страницы не подтверждено.",
    unreadable_page_fragment:"На странице есть текст, который не удалось прочитать.",
    page_handwriting_unlocalized:"Найдена рукописная пометка без надёжной привязки к строке.",
    duplicate_position_candidate:"Номер позиции встречается в нескольких строках.",
    position_gap_candidate:"В нумерации есть пропуск; проверьте, не потерялась ли позиция.",
    page_transcription_incomplete:"Часть страницы распознана неполно. Сверьте результат с оригиналом.",
  };
  return messages[code] || "Проверьте отмеченный фрагмент по PDF.";
}

function aiFirstReportPages(data) {
  return new Map((data?.pages || []).map((page) => [page.page_number, page]));
}

function showAIFirstPage(pageNumber) {
  const pageNumbers = Array.from({length:state.document.page_count}, (_, index) => index + 1);
  if (!pageNumbers.includes(pageNumber)) return;
  const changedPage = state.aiFirst.page !== pageNumber;
  if (changedPage) captureAIFirstPendingEdits();
  state.aiFirst.locatorRequestId = (state.aiFirst.locatorRequestId || 0) + 1;
  const highlight = document.querySelector("#ai-first-row-highlight");
  if (highlight) highlight.hidden = true;
  state.aiFirst.page = pageNumber;
  const selector = document.querySelector("#ai-first-page-select");
  selector.value = String(pageNumber);
  document.querySelector("#ai-first-pdf-page-label").textContent = `Страница ${pageNumber}`;
  document.querySelector("#ai-first-pdf-image").src = documentPageImageUrl(state.document.document_id, pageNumber, 200);
  document.querySelector("#ai-first-prev-page").disabled = pageNumber <= 1;
  document.querySelector("#ai-first-next-page").disabled = pageNumber >= pageNumbers.length;
  const page = aiFirstPageRecord(pageNumber), report = aiFirstReportPages(state.aiFirst.data).get(pageNumber);
  const ready = report?.state === "completed" && ["extracted", "no_specification"].includes(page?.status);
  document.querySelector("#ai-first-confirm-page").disabled = !ready || (state.aiFirst.data.review.confirmed_complete_pages || []).includes(pageNumber);
  document.querySelectorAll("[data-ai-first-row]").forEach((row) => row.classList.toggle("active", Number(row.dataset.pageNumber) === pageNumber));
  renderAIFirstPageIssues(pageNumber);
  renderAIFirstAddRowForm();
  if (changedPage && state.aiFirst.data?.draft) renderAIFirstTables(aiFirstDocumentEntries(state.aiFirst.data.draft, state.aiFirst.data.review));
}

async function locateAIFirstSource(recordId, cellIndex = null) {
  const page = (state.aiFirst.data?.draft?.pages || []).find((item) =>
    (item.tables || []).some((table) => (table.rows || []).some((row) => row.id === recordId))
    || (state.aiFirst.data?.review?.manual_rows || []).some((row) => row.id === recordId && row.page_number === item.page_number));
  const pageNumber = page?.page_number
    ?? (state.aiFirst.data?.review?.manual_rows || []).find((row) => row.id === recordId && row.active)?.page_number;
  if (!pageNumber) return;
  showAIFirstPage(pageNumber);
  const requestId = (state.aiFirst.locatorRequestId || 0) + 1;
  state.aiFirst.locatorRequestId = requestId;
  const highlight = document.querySelector("#ai-first-row-highlight");
  if (highlight) highlight.hidden = true;
  const params = new URLSearchParams({record_id:recordId});
  if (Number.isInteger(cellIndex)) params.set("cell_index", String(cellIndex));
  try {
    const result = await api(`/api/documents/${encodeURIComponent(state.document.document_id)}/ai-first/locator?${params.toString()}`);
    if (requestId !== state.aiFirst.locatorRequestId || state.aiFirst.page !== pageNumber || !highlight) return;
    if (result?.matched && result.bbox && typeof positionHighlight === "function") {
      positionHighlight({bbox:result.bbox}, highlight);
    }
  } catch (_) {
    // Exact matching is optional. The selected source page remains visible
    // without a guessed rectangle when the local locator cannot confirm it.
  }
}

function renderAIFirstPageIssues(pageNumber) {
  const root = document.querySelector("#ai-first-page-issues");
  if (!root) return;
  const page = aiFirstPageRecord(pageNumber);
  const draft = state.aiFirst.data?.draft;
  const review = state.aiFirst.data?.review;
  const pageIssues = [...(page?.issues || []), ...(draft?.issues || [])]
    .filter((issue, index, all) => (issue.page_number == null || issue.page_number === pageNumber) && issue.code !== "page_extraction_failed"
      && ["review", "blocking"].includes(issue.severity) && !(review?.resolved_issue_ids || []).includes(issue.id)
      && all.findIndex((item) => item.id === issue.id) === index);
  root.innerHTML = pageIssues.map((issue) => `<div class="ai-first-issue ${issue.severity === "blocking" ? "blocking" : ""}"><div>${escapeHtml(aiFirstIssueMessage(issue.code))}<div class="ai-first-resolve-row"><input class="input" data-ai-first-issue-note="${escapeHtml(issue.id)}" maxlength="2000" value="${escapeHtml(state.aiFirst.pendingIssueNotes?.[issue.id] || "")}" placeholder="Что проверили по PDF?"><button class="button secondary" type="button" data-ai-first-resolve="${escapeHtml(issue.id)}">Отметить проверенным</button></div></div></div>`).join("");
  root.querySelectorAll("[data-ai-first-resolve]").forEach((button) => button.addEventListener("click", () => {
    const id = button.dataset.aiFirstResolve;
    const detail = root.querySelector(`[data-ai-first-issue-note="${CSS.escape(id)}"]`)?.value || "";
    void postAIFirstReview("resolve-issue", {issue_id:id,detail});
  }));
  root.querySelectorAll("[data-ai-first-issue-note]").forEach((input) => input.addEventListener("input", () => {
    state.aiFirst.pendingIssueNotes ||= {};
    if (input.value.trim()) state.aiFirst.pendingIssueNotes[input.dataset.aiFirstIssueNote] = input.value;
    else delete state.aiFirst.pendingIssueNotes[input.dataset.aiFirstIssueNote];
  }));
}

function renderAIFirstAddRowForm(preserveCurrentValues = true) {
  const root = document.querySelector("#ai-first-add-row-form");
  if (!root) return;
  const previousValues = new Map((preserveCurrentValues ? [...root.querySelectorAll("[data-ai-first-new-cell]")] : [])
    .map((input) => [input.dataset.aiFirstNewColumnKey, input.value]));
  state.aiFirst.addRowValues ||= {};
  if (!state.aiFirst.addRowOpen) {
    root.innerHTML = "";
    return;
  }
  const page = aiFirstPageRecord(state.aiFirst.page);
  const fallback = {columns:["Поз.","Наименование","Артикул","Ед. изм.","Количество","Примечание"].map((raw_header,index)=>({index,raw_header}))};
  const columns = aiFirstColumnList(page?.tables?.[0] || fallback);
  root.innerHTML = `<div class="ai-first-add-form"><b>Новая позиция · страница ${state.aiFirst.page}</b><div class="ai-first-add-fields">${columns.map((column) => `<label><span>${escapeHtml(column.label)}</span><input class="input" data-ai-first-new-cell="${column.index}" data-ai-first-new-column-key="${escapeHtml(column.valueKey)}" maxlength="2000"></label>`).join("")}</div><button class="button primary" type="button" id="ai-first-add-row-submit">Добавить позицию</button></div>`;
  root.querySelectorAll("[data-ai-first-new-cell]").forEach((input) => {
    const key = input.dataset.aiFirstNewColumnKey;
    input.value = previousValues.has(key) ? previousValues.get(key) : (state.aiFirst.addRowValues[key] || "");
    input.addEventListener("input", () => { state.aiFirst.addRowValues[key] = input.value; });
  });
  const add = root.querySelector("#ai-first-add-row-submit");
  if (add) add.addEventListener("click", async () => {
    const values = {};
    root.querySelectorAll("[data-ai-first-new-cell]").forEach((input) => { values[`cell:${input.dataset.aiFirstNewCell}`] = input.value; });
    const result = await postAIFirstReview("add-row", {page_number:state.aiFirst.page,values});
    if (!result?.saved) return;
    state.aiFirst.addRowValues = {};
    renderAIFirstAddRowForm(false);
  });
}

function renderAIFirstRunFeedback(data, includePending = false) {
  const root = document.querySelector("#ai-first-run-feedback");
  const failed = (data?.pages || []).filter((page) => page.state !== "completed" && (includePending || page.state !== "pending" || page.safe_error_code));
  if (!failed.length) { root.hidden = true; root.innerHTML = ""; return; }
  const groups = new Map();
  for (const page of failed) {
    const code = page.safe_error_code || page.state;
    const item = groups.get(code) || {pages:[],message:aiFirstProcessingMessage(code)};
    item.pages.push(page.page_number);
    groups.set(code, item);
  }
  const retryable = [...groups.entries()].filter(([code]) => !["image_pixel_limit","image_byte_limit","page_dimensions_invalid"].includes(code)).flatMap(([, item]) => item.pages);
  root.hidden = false;
  root.innerHTML = `<b>Распознавание завершилось не полностью</b><p>Документ не считается проверенным. Для ${failed.length} страниц нет завершённого результата; проверьте статусы ниже.</p><ul>${[...groups.values()].map((item) => `<li>${item.pages.length} стр. (${item.pages.join(", ")}) — ${escapeHtml(item.message)}</li>`).join("")}</ul>${retryable.length ? `<button class="button secondary" type="button" data-ai-first-retry-pages="${retryable.join(",")}">Повторить обработку доступных страниц</button>` : `<small>Для этих страниц текущие ограничения рендеринга не позволяют повторить обработку с теми же настройками.</small>`}`;
  root.querySelector("[data-ai-first-retry-pages]")?.addEventListener("click", () => retryAIFirstPages(retryable));
}

function retryAIFirstPages(pageNumbers) {
  if (!pageNumbers.length) return;
  const modeValue = state.aiFirst.data?.mode === "fake_vision" ? "fake" : "qwen";
  const modeSelect = document.querySelector("#ai-first-mode");
  if (modeSelect.querySelector(`option[value="${modeValue}"]`)?.hidden) {
    toast("Этот режим сейчас выключен. Попросите администратора проверить настройки локального тестового режима.", "error");
    return;
  }
  state.aiFirst.mode = modeValue;
  modeSelect.value = modeValue;
  state.selectedPages = new Set(pageNumbers);
  const reportPages = aiFirstReportPages(state.aiFirst.data);
  state.aiFirst.retryPages = new Set(pageNumbers.filter((number) => ["failed","incomplete","unknown_outcome"].includes(reportPages.get(number)?.state)));
  updatePageSelection();
  renderAIFirstResumeControls();
  document.querySelector("#ai-first-live-confirm").checked = false;
  updateAIFirstModeControls();
  if (modeValue === "fake") void startAIFirstRecognition();
  else {
    setView("pages");
    toast("Страницы выбраны для явного повтора. Подтвердите платную обработку перед запуском.", "warning");
  }
}

function renderAIFirstReview() {
  const data = state.aiFirst.data;
  if (!data?.draft || !state.document) return;
  const draft = data.draft, review = data.review;
  state.aiFirst.exportPages ||= new Set();
  const pageNumbers = Array.from({length:state.document.page_count}, (_, index) => index + 1);
  if (!pageNumbers.includes(state.aiFirst.page)) state.aiFirst.page = pageNumbers[0] || 1;
  const pageReports = aiFirstReportPages(data);
  const exportablePages = new Set(pageNumbers.filter((number) => pageReports.get(number)?.state === "completed"
    && ["extracted", "no_specification"].includes(aiFirstPageRecord(number)?.status)));
  state.aiFirst.exportPages = new Set([...(state.aiFirst.exportPages || [])].filter((number) => exportablePages.has(number)));
  const entries = aiFirstDocumentEntries(draft, review);
  const totalRows = entries.length;
  const reviewedRows = entries.filter((entry) => review.decisions?.[entry.row.id]?.active).length;
  const humanRoles = review.row_roles || {};
  const subjectRows = entries.filter((entry) => {
    const role = humanRoles[entry.row.id];
    return role?.active && ["standalone_item", "component"].includes(role.role);
  }).length;
  const confirmedItems = entries.filter((entry) => {
    const role = humanRoles[entry.row.id], decision = review.decisions?.[entry.row.id];
    return role?.active && role.role === "standalone_item" && decision?.active
      && decision.human_confirmed !== false && ["confirmed", "corrected"].includes(decision.disposition);
  }).length;
  const unresolved = (draft.issues || []).filter((issue) => issue.code !== "page_extraction_failed" && ["review", "blocking"].includes(issue.severity) && !(review.resolved_issue_ids || []).includes(issue.id)).length;
  const confirmedPages = (review.confirmed_complete_pages || []).length;
  const processedPages = pageNumbers.filter((number) => pageReports.get(number)?.state === "completed" && ["extracted","no_specification"].includes(aiFirstPageRecord(number)?.status)).length;
  const failedPages = (data.pages || []).filter((item) => item.state !== "completed" && (item.state !== "pending" || item.safe_error_code)).length;
  document.querySelector("#ai-first-review-status").textContent = `${processedPages} из ${state.document.page_count} страниц обработано · ${totalRows} строк распознано · изменения сохраняются локально`;
  const summaries = [
    ["Строк распознано", totalRows, ""],
    ["Предметных строк", subjectRows, ""],
    ["Подтверждённые самостоятельные позиции", confirmedItems, ""],
    ["Решения сохранены", reviewedRows, reviewedRows < totalRows ? "warning" : ""],
    ["Страницы обработаны", `${processedPages} / ${state.document.page_count}`, processedPages < state.document.page_count ? "warning" : ""],
    ["Полнота подтверждена", `${confirmedPages} / ${state.document.page_count}`, confirmedPages < state.document.page_count ? "warning" : ""],
  ];
  document.querySelector("#ai-first-review-summary").innerHTML = summaries.map(([label,value,kind]) => `<div class="summary-card ${kind}"><small>${label}</small><b>${value}</b></div>`).join("");
  const selector = document.querySelector("#ai-first-page-select");
  selector.innerHTML = pageNumbers.map((number) => `<option value="${number}">Страница ${number}</option>`).join("");
  document.querySelector("#ai-first-page-status-list").innerHTML = pageNumbers.map((number) => {
    const page = aiFirstPageRecord(number), report = pageReports.get(number);
    const complete = report?.state === "completed" && ["extracted","no_specification"].includes(page?.status);
    let label = "Не входила в последний запуск";
    let detail = "Эта страница не подтверждена в текущем результате.";
    if (complete && page.status === "no_specification") { label = "Таблица не обнаружена"; detail = "Сверьте PDF и подтвердите полноту, если на странице нет позиций."; }
    else if (complete) { label = "Обработана"; detail = `${page.tables.reduce((sum, table) => sum + table.rows.length, 0)} позиций на странице.`; }
    else if (report?.state === "pending" && !report.safe_error_code) { label = "Не запускалась"; detail = "Страница не входила в последний запуск."; }
    else if (report) { label = "Требует обработки"; detail = aiFirstProcessingMessage(report.safe_error_code || report.state); }
    const confirmed = (review.confirmed_complete_pages || []).includes(number);
    const retryable = report && (report.state !== "pending" || report.safe_error_code) && report.state !== "completed" && !["image_pixel_limit","image_byte_limit","page_dimensions_invalid"].includes(report.safe_error_code);
    const exportChecked = state.aiFirst.exportPages.has(number) ? "checked" : "";
    return `<div class="ai-first-page-status"><button class="button ghost" type="button" data-ai-first-show-page="${number}">Страница ${number} · ${label}</button><span>${escapeHtml(detail)}${confirmed ? " · полнота подтверждена" : ""}</span>${complete && !confirmed ? `<button class="button text" type="button" data-ai-first-complete-page="${number}">Подтвердить полноту</button>` : ""}${retryable ? `<button class="button text" type="button" data-ai-first-retry-one="${number}">Повторить</button>` : ""}${complete ? `<label class="ai-first-export-page"><input type="checkbox" data-ai-first-export-page="${number}" ${exportChecked}> В тестовый XLSX</label>` : ""}</div>`;
  }).join("");
  document.querySelector("#ai-first-page-details-label").textContent = `Страницы: ${processedPages} обработано из ${state.document.page_count} · ${confirmedPages} подтверждено${failedPages ? ` · ${failedPages} требуют внимания` : ""}`;
  document.querySelectorAll("[data-ai-first-show-page]").forEach((button) => button.addEventListener("click", () => showAIFirstPage(Number(button.dataset.aiFirstShowPage))));
  document.querySelectorAll("[data-ai-first-complete-page]").forEach((button) => button.addEventListener("click", () => void confirmAIFirstPageCompleteness(Number(button.dataset.aiFirstCompletePage))));
  document.querySelectorAll("[data-ai-first-retry-one]").forEach((button) => button.addEventListener("click", () => retryAIFirstPages([Number(button.dataset.aiFirstRetryOne)])));
  document.querySelectorAll("[data-ai-first-export-page]").forEach((input) => input.addEventListener("change", () => {
    const pageNumber = Number(input.dataset.aiFirstExportPage);
    if (input.checked && state.aiFirst.exportPages.size >= 3) {
      input.checked = false;
      toast("Для тестовой выгрузки выберите не более трёх страниц.", "error");
      return;
    }
    if (input.checked) state.aiFirst.exportPages.add(pageNumber); else state.aiFirst.exportPages.delete(pageNumber);
    updateAIFirstExportControls();
  }));
  renderAIFirstTables(entries);
  document.querySelector("#ai-first-export-note").textContent = "Тестовый XLSX содержит отдельный лист подтверждённых самостоятельных позиций и полный лист распознанных строк. Он не является коммерческой сметой и не запускает sourcing.";
  updateAIFirstExportControls();
  showAIFirstPage(state.aiFirst.page);
}

function updateAIFirstExportControls() {
  const button = document.querySelector("#ai-first-export-pilot");
  const label = document.querySelector("#ai-first-export-note");
  if (!button || !label) return;
  state.aiFirst.exportPages ||= new Set();
  button.disabled = state.aiFirst.exportPages.size === 0;
  const selected = [...state.aiFirst.exportPages].sort((a,b)=>a-b);
  label.textContent = selected.length
    ? `В тестовый XLSX выбраны страницы ${selected.join(", ")}. Охват документа будет явно отмечен; файл не является коммерческой сметой.`
    : "Выберите от одной до трёх обработанных страниц в сводке. Неподтверждённые строки останутся на отдельном листе; файл не является коммерческой сметой.";
}

function aiFirstColumnList(table) {
  const occurrences = new Map();
  return [...(table.columns || [])].sort((a,b)=>a.index-b.index).map((column) => {
    const label = (column.raw_header || `Колонка ${column.index + 1}`).trim();
    const base = label.toLocaleLowerCase("ru-RU");
    const occurrence = (occurrences.get(base) || 0) + 1;
    occurrences.set(base, occurrence);
    return {index:column.index,label:occurrence > 1 ? `${label} (${occurrence})` : label,key:`${base}\0${occurrence}`,valueKey:`${base}#${occurrence}`};
  });
}

function aiFirstDocumentEntries(draft, review) {
  const entries = [];
  for (const page of draft.pages || []) for (const table of page.tables || []) {
    const columns = aiFirstColumnList(table);
    for (const row of table.rows || []) entries.push({row,page,table,columns,manual:false});
  }
  const defaultHeaders = ["Поз.","Наименование","Артикул","Ед. изм.","Количество","Примечание"];
  for (const row of (review.manual_rows || []).filter((item) => item.active)) {
    const page = (draft.pages || []).find((item) => item.page_number === row.page_number);
    const table = page?.tables?.[0] || {columns:defaultHeaders.map((raw_header,index)=>({index,raw_header}))};
    entries.push({row,page,table,columns:aiFirstColumnList(table),manual:true});
  }
  return entries;
}

function aiFirstRowsWithoutReviewedRole(pageNumber) {
  const data = state.aiFirst.data;
  if (!data?.draft) return [];
  const roles = data.review?.row_roles || {};
  return aiFirstDocumentEntries(data.draft, data.review).filter((entry) => {
    const page = Number(entry.page?.page_number || entry.row.page_number);
    const role = roles[entry.row.id];
    return page === Number(pageNumber) && (!role?.active || role.role === "unknown");
  });
}

function captureAIFirstPendingEdits() {
  const root = document.querySelector("#ai-first-table-scroll");
  if (!root) return;
  state.aiFirst.pendingRowValues ||= {};
  state.aiFirst.pendingRowRoles ||= {};
  state.aiFirst.pendingIssueNotes ||= {};
  state.aiFirst.openRowPanels ||= new Set();
  const rowIds = new Set([...root.querySelectorAll("[data-ai-first-cell]")].map((input) => input.dataset.aiFirstCell));
  for (const id of rowIds) {
    const changed = aiFirstRowInputSnapshot(id).filter((item) => item.value !== item.baseline);
    if (changed.length) state.aiFirst.pendingRowValues[id] = aiFirstRowValues(changed);
    else delete state.aiFirst.pendingRowValues[id];
  }
  for (const select of root.querySelectorAll("[data-ai-first-row-role]")) {
    const id = select.dataset.aiFirstRowRole, role = state.aiFirst.data?.review?.row_roles?.[id];
    const roleValue = select.value, parentValue = root.querySelector(`[data-ai-first-parent-row="${CSS.escape(id)}"]`)?.value || "";
    if (roleValue !== (role?.active ? role.role : "unknown")
        || (roleValue === "component" && parentValue !== (role?.active ? role.parent_record_id || "" : ""))) {
      state.aiFirst.pendingRowRoles[id] = {role:roleValue,parent_record_id:roleValue === "component" ? parentValue : null};
    } else delete state.aiFirst.pendingRowRoles[id];
  }
  for (const input of document.querySelectorAll("#ai-first-page-issues [data-ai-first-issue-note]")) {
    const id = input.dataset.aiFirstIssueNote;
    if (input.value.trim()) state.aiFirst.pendingIssueNotes[id] = input.value;
    else delete state.aiFirst.pendingIssueNotes[id];
  }
  for (const panel of root.querySelectorAll("[data-ai-first-row-panel]")) {
    if (panel.open) state.aiFirst.openRowPanels.add(panel.dataset.aiFirstRowPanel);
    else state.aiFirst.openRowPanels.delete(panel.dataset.aiFirstRowPanel);
  }
}

function captureAIFirstViewState() {
  const root = document.querySelector("#ai-first-table-scroll");
  const active = document.activeElement, dataset = active?.dataset || {};
  let focus = null;
  if (dataset.aiFirstCell) focus = {type:"cell",id:dataset.aiFirstCell,column:dataset.columnIndex};
  else if (dataset.aiFirstRowRole) focus = {type:"role",id:dataset.aiFirstRowRole};
  else if (dataset.aiFirstParentRow) focus = {type:"parent",id:dataset.aiFirstParentRow};
  else if (dataset.aiFirstSelectRow) focus = {type:"select-row",id:dataset.aiFirstSelectRow};
  else if (dataset.aiFirstSaveRow) focus = {type:"row-action",attribute:"save-row",id:dataset.aiFirstSaveRow};
  else if (dataset.aiFirstConfirmRow) focus = {type:"row-action",attribute:"confirm-row",id:dataset.aiFirstConfirmRow};
  else if (dataset.aiFirstExcludeRow) focus = {type:"row-action",attribute:"exclude-row",id:dataset.aiFirstExcludeRow};
  else if (dataset.aiFirstSaveRole) focus = {type:"row-action",attribute:"save-role",id:dataset.aiFirstSaveRole};
  else if (["ai-first-search", "ai-first-review-filter", "ai-first-bulk-role", "ai-first-page-select"].includes(active?.id)) focus = {type:"id",id:active.id};
  return {scrollTop:root?.scrollTop || 0,focus,selectionStart:active?.selectionStart,selectionEnd:active?.selectionEnd};
}

function restoreAIFirstViewState(view) {
  const root = document.querySelector("#ai-first-table-scroll");
  if (root) root.scrollTop = view?.scrollTop || 0;
  if (!view?.focus) return;
  let target = null;
  if (view.focus.type === "cell") target = root?.querySelector(`[data-ai-first-cell="${CSS.escape(view.focus.id)}"][data-column-index="${CSS.escape(view.focus.column)}"]`);
  else if (view.focus.type === "role") target = root?.querySelector(`[data-ai-first-row-role="${CSS.escape(view.focus.id)}"]`);
  else if (view.focus.type === "parent") target = root?.querySelector(`[data-ai-first-parent-row="${CSS.escape(view.focus.id)}"]`);
  else if (view.focus.type === "select-row") target = root?.querySelector(`[data-ai-first-select-row="${CSS.escape(view.focus.id)}"]`);
  else if (view.focus.type === "row-action") target = root?.querySelector(`[data-ai-first-${view.focus.attribute}="${CSS.escape(view.focus.id)}"]`);
  else if (view.focus.type === "id") target = document.getElementById(view.focus.id);
  target?.focus();
  if (target && Number.isInteger(view.selectionStart) && typeof target.setSelectionRange === "function") {
    target.setSelectionRange(view.selectionStart, view.selectionEnd);
  }
}

function aiFirstEntrySearchText(entry) {
  const decision = aiFirstDecision(entry.row.id), role = state.aiFirst.data?.review?.row_roles?.[entry.row.id];
  return [entry.row.model_kind, role?.role, ...entry.columns.map((column) => column.label),
    ...(entry.row.raw_cells || []).map((cell) => cell.raw_value), ...Object.values(decision?.values || {}),
    ...Object.values(state.aiFirst.pendingRowValues?.[entry.row.id] || {}),
    ...Object.values(entry.manual ? entry.row.values || {} : {})].join(" ").toLocaleLowerCase("ru-RU");
}

function aiFirstVisibleSelectedIds() {
  const root = document.querySelector("#ai-first-table-scroll");
  return [...(root?.querySelectorAll("tr[data-ai-first-row]") || [])]
    .filter((row) => !row.hidden && state.aiFirst.selectedRows.has(row.dataset.aiFirstRow))
    .map((row) => row.dataset.aiFirstRow);
}

function updateAIFirstBulkControls() {
  const root = document.querySelector("#ai-first-table-scroll");
  const visibleIds = aiFirstVisibleSelectedIds();
  const confirmButton = root?.querySelector("#ai-first-confirm-selected");
  const roleButton = root?.querySelector("[data-ai-first-bulk-role-submit]");
  if (confirmButton) confirmButton.disabled = visibleIds.length === 0;
  if (roleButton) roleButton.disabled = visibleIds.length === 0;
  const count = root?.querySelector("[data-ai-first-selected-count]");
  if (count) count.textContent = visibleIds.length ? `Выбрано: ${visibleIds.length}` : "Выберите строки для группового действия";
}

function applyAIFirstReviewFilters() {
  const root = document.querySelector("#ai-first-table-scroll");
  if (!root) return;
  const query = (root.querySelector("#ai-first-search")?.value || "").trim().toLocaleLowerCase("ru-RU");
  const filter = root.querySelector("#ai-first-review-filter")?.value || "all";
  state.aiFirst.reviewSearch = query;
  state.aiFirst.reviewFilter = filter;
  for (const row of root.querySelectorAll("tr[data-ai-first-row]")) {
    const entry = state.aiFirst.renderedEntries?.get(row.dataset.aiFirstRow);
    if (!entry) continue;
    const reviewState = aiFirstRowReviewState(entry);
    const role = state.aiFirst.data?.review?.row_roles?.[entry.row.id];
    const decision = aiFirstDecision(entry.row.id);
    const service = ["header", "column_numbering", "section", "note", "continuation"].includes(role?.role);
    const filterMatch = filter === "all"
      || (filter === "attention" && ["attention", "unsaved"].includes(reviewState.status))
      || (filter === "unassigned" && !reviewState.roleVerified)
      || (filter === "checked" && reviewState.status === "checked")
      || (filter === "corrected" && decision?.active && decision.disposition === "corrected")
      || (filter === "excluded" && decision?.active && decision.disposition === "excluded")
      || (filter === "service" && service);
    row.hidden = !filterMatch || (query && !aiFirstEntrySearchText(entry).includes(query));
  }
  for (const group of root.querySelectorAll("[data-ai-first-table-group]")) {
    group.hidden = ![...group.querySelectorAll("tr[data-ai-first-row]")].some((row) => !row.hidden);
  }
  const visible = [...root.querySelectorAll("tr[data-ai-first-row]")].filter((row) => !row.hidden).length;
  const total = root.querySelectorAll("tr[data-ai-first-row]").length;
  const summary = root.querySelector("[data-ai-first-filter-summary]");
  if (summary) summary.textContent = `${visible} из ${total} строк на странице`;
  updateAIFirstBulkControls();
}

function renderAIFirstTables(entries) {
  const root = document.querySelector("#ai-first-table-scroll");
  if (!root) return;
  captureAIFirstPendingEdits();
  const view = captureAIFirstViewState();
  const pageEntries = entries.filter((entry) => Number(entry.page?.page_number || entry.row.page_number) === state.aiFirst.page);
  state.aiFirst.renderedEntries = new Map(pageEntries.map((entry) => [entry.row.id, entry]));
  state.aiFirst.openRowPanels ||= new Set();
  const byGroup = new Map();
  for (const entry of pageEntries) {
    const key = entry.manual ? `manual:${state.aiFirst.page}` : `table:${entry.table?.id || entry.row.table_id || "unknown"}`;
    const group = byGroup.get(key) || {key,manual:entry.manual,table:entry.table,entries:[]};
    group.entries.push(entry);
    byGroup.set(key, group);
  }
  const groupHtml = [...byGroup.values()].map((group) => {
    const columns = group.entries[0]?.columns || [];
    const headers = columns.map((column) => `<th>${escapeHtml(column.label)}</th>`).join("");
    const rows = group.entries.map((entry) => {
      const {row,page,columns:rowColumns,manual} = entry;
      const decision = aiFirstDecision(row.id);
      const reviewState = aiFirstRowReviewState(entry);
      const excluded = decision?.active && decision.disposition === "excluded";
      const pendingValues = state.aiFirst.pendingRowValues?.[row.id] || {};
      const cells = columns.map((column) => {
        const sourceColumn = rowColumns.find((item) => item.index === column.index);
        if (!sourceColumn) return "<td></td>";
        const key = `cell:${sourceColumn.index}`;
        const cell = manual ? null : (row.raw_cells || []).find((item) => item.column_index === sourceColumn.index);
        const source = manual ? row.values?.[key] || "" : cell?.raw_value ?? "";
        const retained = (decision?.active || decision?.invalidation_reason === "role_change")
          && Object.prototype.hasOwnProperty.call(decision.values || {}, key);
        const shown = Object.prototype.hasOwnProperty.call(pendingValues, key) ? pendingValues[key] : retained ? decision.values[key] : source;
        const sourceNote = retained && decision.values[key] !== source
          ? `<small class="ai-first-cell-source">${decision.invalidation_reason === "role_change" ? "Сохранённая правка · требует повторного подтверждения. Исходно:" : "Исходно:"} ${escapeHtml(source || "пусто")}</small>` : "";
        const uncertain = !manual && (cell?.unreadable || cell?.struck_through || cell?.handwritten || cell?.alternatives?.length || cell?.handwritten_replacements?.length);
        const needsAttention = reviewState.status === "attention" || (uncertain && !reviewState.decisionCurrent);
        return `<td class="${needsAttention ? "ai-first-cell--attention" : reviewState.status === "unsaved" ? "ai-first-cell--unsaved" : reviewState.status === "checked" ? "ai-first-cell--checked" : ""}"><input class="cell-input ai-first-cell-input" data-ai-first-cell="${escapeHtml(row.id)}" data-column-index="${sourceColumn.index}" aria-label="${escapeHtml(sourceColumn.label)}" maxlength="2000" value="${escapeHtml(shown)}">${sourceNote}${uncertain && !reviewState.decisionCurrent ? `<small class="critical-warning">Требует сверки${cell?.alternatives?.length ? ` · варианты: ${escapeHtml(cell.alternatives.join(" / "))}` : ""}</small>` : ""}</td>`;
      }).join("");
      const role = state.aiFirst.data.review.row_roles?.[row.id];
      const pendingRole = state.aiFirst.pendingRowRoles?.[row.id];
      const selectedRole = pendingRole?.role || (role?.active ? role.role : "unknown");
      const rowPage = Number(page?.page_number || row.page_number);
      const groupTableId = manual ? null : (group.table?.id || row.table_id);
      const parentCandidates = pageEntries.filter((candidate) => {
        if (candidate.row.id === row.id || Number(candidate.page?.page_number || candidate.row.page_number) !== rowPage) return false;
        if (groupTableId && candidate.row.table_id && candidate.row.table_id !== groupTableId) return false;
        const candidateRole = state.aiFirst.data.review.row_roles?.[candidate.row.id];
        return candidateRole?.active && candidateRole.role === "standalone_item";
      });
      const parentOptions = `<option value="">Выберите самостоятельную позицию</option>${parentCandidates.map((candidate) => `<option value="${escapeHtml(candidate.row.id)}" ${(pendingRole?.parent_record_id || role?.parent_record_id) === candidate.row.id ? "selected" : ""}>Строка ${escapeHtml(candidate.manual ? "ручная" : String((candidate.row.index ?? 0) + 1))}</option>`).join("")}`;
      const rowRoleOptions = [["unknown","Не определена"],["standalone_item","Самостоятельная позиция"],["component","Компонент комплекта"],["header","Заголовок"],["column_numbering","Нумерация колонок"],["section","Раздел"],["continuation","Продолжение строки"],["note","Примечание"]];
      const roleControls = `<div class="ai-first-role-controls"><label>Роль строки<select class="select" data-ai-first-row-role="${escapeHtml(row.id)}">${rowRoleOptions.map(([value,label]) => `<option value="${value}" ${selectedRole === value ? "selected" : ""}>${label}</option>`).join("")}</select></label><select class="select" data-ai-first-parent-row="${escapeHtml(row.id)}" ${selectedRole === "component" ? "" : "hidden"}>${parentOptions}</select><button class="button text" type="button" data-ai-first-save-role="${escapeHtml(row.id)}">Сохранить роль</button></div>`;
      const kindLabel = ({item:"Позиция",item_candidate:"Возможная позиция",header:"Заголовок",section:"Раздел",note:"Примечание"})[row.model_kind] || (manual ? "Ручная позиция" : "Строка документа");
      const rowLabel = manual ? "Ручная" : String((row.index ?? 0) + 1);
      const open = state.aiFirst.openRowPanels.has(row.id) ? "open" : "";
      const actionPanel = `<details class="ai-first-row-panel" data-ai-first-row-panel="${escapeHtml(row.id)}" ${open}><summary>Роль и действия</summary>${roleControls}<div class="ai-first-row-actions"><button class="button secondary" type="button" data-ai-first-save-row="${escapeHtml(row.id)}">Сохранить правки</button><button class="button ghost" type="button" data-ai-first-confirm-row="${escapeHtml(row.id)}">Подтвердить значения</button><button class="button text" type="button" data-ai-first-exclude-row="${escapeHtml(row.id)}">${excluded ? "Восстановить" : "Исключить"}</button></div></details>`;
      const searchable = aiFirstEntrySearchText(entry);
      return `<tr data-ai-first-row="${escapeHtml(row.id)}" data-page-number="${rowPage}" data-review-state="${reviewState.status}" data-role="${escapeHtml(selectedRole)}" data-disposition="${escapeHtml(decision?.disposition || "")}" data-search-text="${escapeHtml(searchable)}" class="ai-first-row--${reviewState.status} ${excluded ? "is-excluded" : ""} active"><td class="selector"><input type="checkbox" data-ai-first-select-row="${escapeHtml(row.id)}" aria-label="Выбрать строку" ${state.aiFirst.selectedRows.has(row.id) ? "checked" : ""}></td><td>${rowLabel}<small class="ai-first-cell-source">${escapeHtml(kindLabel)} · <span data-ai-first-row-status>${escapeHtml(aiFirstRowStatusLabel(reviewState))}</span></small></td><td><button class="button text ai-first-source-page" type="button" data-ai-first-show-page="${rowPage}">Стр. ${rowPage}</button></td>${cells}<td>${actionPanel}</td></tr>`;
    }).join("");
    const title = group.manual ? "Добавленные вручную" : `Таблица ${Number(group.table?.index ?? 0) + 1}`;
    const count = group.entries.length;
    return `<section class="ai-first-source-table-group" data-ai-first-table-group="${escapeHtml(group.key)}"><h4>${escapeHtml(title)} <small>${count} строк</small></h4><table class="data-table ai-first-table"><thead><tr><th class="selector">Выбор</th><th>Строка</th><th>Страница</th>${headers}<th>Проверка</th></tr></thead><tbody>${rows}</tbody></table></section>`;
  }).join("");
  const options = `<option value="all">Все строки</option><option value="attention">Требуют внимания</option><option value="unassigned">Роль не назначена</option><option value="checked">Проверены</option><option value="corrected">Исправлены</option><option value="excluded">Исключены</option><option value="service">Служебные строки</option>`;
  const bulkRoles = [["standalone_item","Самостоятельная позиция"],["header","Заголовок"],["column_numbering","Нумерация колонок"],["section","Раздел"],["continuation","Продолжение строки"],["note","Примечание"]];
  root.innerHTML = `<div class="ai-first-row-toolbar"><label class="ai-first-search-label">Поиск по строкам, наименованиям, маркам и артикулам<input class="input" id="ai-first-search" type="search" value="${escapeHtml(state.aiFirst.reviewSearch || "")}" placeholder="Например: артикул или марка"></label><label>Фильтр<select class="select" id="ai-first-review-filter">${options}</select></label><button class="button ghost" type="button" data-ai-first-next-attention>Следующая строка для проверки</button><span data-ai-first-filter-summary role="status" aria-live="polite"></span></div><div class="ai-first-bulk-toolbar"><label>Назначить выбранным<select class="select" id="ai-first-bulk-role">${bulkRoles.map(([value,label]) => `<option value="${value}">${label}</option>`).join("")}</select></label><button class="button secondary" id="ai-first-confirm-selected" type="button" disabled>Подтвердить выбранные</button><button class="button secondary" type="button" data-ai-first-bulk-role-submit disabled>Применить роль выбранным</button><span data-ai-first-selected-count>Выберите строки для группового действия</span></div>${groupHtml || `<div class="empty-table">На этой странице распознанных строк нет. Состояние и полноту страницы можно проверить в сводке.</div>`}`;
  const filter = root.querySelector("#ai-first-review-filter");
  if (filter) filter.value = state.aiFirst.reviewFilter || "all";
  root.querySelector("#ai-first-search")?.addEventListener("input", applyAIFirstReviewFilters);
  filter?.addEventListener("change", applyAIFirstReviewFilters);
  root.querySelectorAll("[data-ai-first-select-row]").forEach((input) => input.addEventListener("change", () => {
    const id = input.dataset.aiFirstSelectRow;
    if (input.checked) state.aiFirst.selectedRows.add(id); else state.aiFirst.selectedRows.delete(id);
    updateAIFirstBulkControls();
  }));
  root.querySelectorAll("[data-ai-first-save-row]").forEach((button) => button.addEventListener("click", () => {
    const id = button.dataset.aiFirstSaveRow, values = {};
    root.querySelectorAll("[data-ai-first-cell]").forEach((input) => { if (input.dataset.aiFirstCell === id) values[`cell:${input.dataset.columnIndex}`] = input.value; });
    void postAIFirstReview("row", {record_id:id,disposition:"corrected",confirmed:false,values});
  }));
  root.querySelectorAll("[data-ai-first-cell]").forEach((input) => input.addEventListener("input", () => updateAIFirstRenderedRowState(input.dataset.aiFirstCell)));
  root.querySelectorAll("[data-ai-first-row-role]").forEach((select) => select.addEventListener("change", () => {
    const parent = root.querySelector(`[data-ai-first-parent-row="${CSS.escape(select.dataset.aiFirstRowRole)}"]`);
    if (parent) parent.hidden = select.value !== "component";
    updateAIFirstRenderedRowState(select.dataset.aiFirstRowRole);
  }));
  root.querySelectorAll("[data-ai-first-row-panel]").forEach((panel) => panel.addEventListener("toggle", () => {
    if (panel.open) state.aiFirst.openRowPanels.add(panel.dataset.aiFirstRowPanel);
    else state.aiFirst.openRowPanels.delete(panel.dataset.aiFirstRowPanel);
  }));
  root.querySelectorAll("[data-ai-first-save-role]").forEach((button) => button.addEventListener("click", () => {
    const recordId = button.dataset.aiFirstSaveRole;
    const snapshot = aiFirstRowInputSnapshot(recordId);
    if (snapshot.some((item) => item.value !== item.baseline)) {
      toast("Сначала сохраните или отмените правки ячеек этой строки, затем измените её роль.", "error");
      return;
    }
    const role = root.querySelector(`[data-ai-first-row-role="${CSS.escape(recordId)}"]`)?.value;
    const parent = root.querySelector(`[data-ai-first-parent-row="${CSS.escape(recordId)}"]`)?.value || null;
    if (role === "component" && !parent) {
      toast("Для компонента выберите самостоятельную позицию-родителя на этой странице.", "error");
      return;
    }
    if (!role) return;
    void postAIFirstReview("row-role", {record_id:recordId,role,parent_record_id:role === "component" ? parent : null});
  }));
  root.querySelectorAll("[data-ai-first-confirm-row]").forEach((button) => button.addEventListener("click", () => {
    const id = button.dataset.aiFirstConfirmRow, snapshot = aiFirstRowInputSnapshot(id);
    const changed = snapshot.filter((item) => item.value !== item.baseline);
    const preview = snapshot.map((item) => `${item.label}: ${item.value || "пусто"}`).join("\n");
    const question = changed.length
      ? `Сохранить несохранённые изменения и подтвердить эту строку?\n\n${changed.map((item) => `${item.label}: ${item.baseline || "пусто"} → ${item.value || "пусто"}`).join("\n")}`
      : `Подтвердить показанные значения после сверки с PDF?\n\n${preview}`;
    if (!confirm(question)) return;
    void postAIFirstReview("row", {record_id:id,...aiFirstRowConfirmationPayload(snapshot)});
  }));
  root.querySelectorAll("[data-ai-first-exclude-row]").forEach((button) => button.addEventListener("click", () => {
    const id = button.dataset.aiFirstExcludeRow, current = aiFirstDecision(id);
    const disposition = current?.active && current.disposition === "excluded" ? "confirmed" : "excluded";
    const snapshot = aiFirstRowInputSnapshot(id);
    if (disposition === "excluded") {
      if (!confirm("Исключить строку из проверенных позиций? Сохранённые исправления останутся в истории, строка сохранится в распознанных данных.")) return;
      void postAIFirstReview("row", {record_id:id,disposition,values:{}});
      return;
    }
    const values = aiFirstRowValues(snapshot);
    const changedFromSource = Object.keys(current?.values || {}).length > 0;
    const preview = snapshot.map((item) => `${item.label}: ${item.value || "пусто"}`).join("\n");
    if (!confirm(`Вернуть строку в проверку со сохранёнными значениями? Сверьте их с PDF перед подтверждением.\n\n${preview}`)) return;
    void postAIFirstReview("row", {record_id:id,disposition:changedFromSource ? "corrected" : "confirmed",confirmed:true,values});
  }));
  root.querySelectorAll("[data-ai-first-resolve]").forEach((button) => button.addEventListener("click", () => {
    const id = button.dataset.aiFirstResolve;
    const detail = document.querySelector(`[data-ai-first-issue-note="${CSS.escape(id)}"]`)?.value || "";
    void postAIFirstReview("resolve-issue", {issue_id:id,detail});
  }));
  root.querySelectorAll("[data-ai-first-row]").forEach((row) => row.addEventListener("click", (event) => {
    if (event.target.closest("button, input, select, textarea, summary, details")) return;
    void locateAIFirstSource(row.dataset.aiFirstRow, null);
  }));
  root.querySelectorAll("[data-ai-first-cell]").forEach((input) => input.addEventListener("click", (event) => {
    event.stopPropagation();
    void locateAIFirstSource(input.dataset.aiFirstCell, Number(input.dataset.columnIndex));
  }));
  root.querySelectorAll("[data-ai-first-show-page]").forEach((button) => button.addEventListener("click", (event) => {
    event.stopPropagation();
    showAIFirstPage(Number(button.dataset.aiFirstShowPage));
  }));
  root.querySelector("[data-ai-first-next-attention]")?.addEventListener("click", () => {
    const rows = [...root.querySelectorAll("tr[data-ai-first-row]")].filter((row) => !row.hidden && ["attention", "unsaved"].includes(row.dataset.reviewState));
    if (!rows.length) { toast("В текущем фильтре нет строк, требующих внимания.", "warning"); return; }
    const current = rows.findIndex((row) => row.dataset.aiFirstRow === state.aiFirst.lastAttentionRow);
    const next = rows[(current + 1) % rows.length];
    state.aiFirst.lastAttentionRow = next.dataset.aiFirstRow;
    next.scrollIntoView({block:"center"});
    void locateAIFirstSource(next.dataset.aiFirstRow, null);
  });
  root.querySelector("[data-ai-first-bulk-role-submit]")?.addEventListener("click", () => void assignSelectedAIFirstRole());
  applyAIFirstReviewFilters();
  restoreAIFirstViewState(view);
}

async function postAIFirstReview(action, body) {
  const affected = new Set(body.record_id ? [body.record_id] : (body.record_ids || []));
  captureAIFirstPendingEdits();
  const pending = new Map();
  for (const input of document.querySelectorAll("#ai-first-table-scroll [data-ai-first-cell]")) {
    const id = input.dataset.aiFirstCell;
    const rowPending = pending.get(id) || {};
    rowPending[`cell:${input.dataset.columnIndex}`] = input.value;
    pending.set(id, rowPending);
  }
  for (const [id, values] of pending) {
    const snapshot = aiFirstRowInputSnapshot(id);
    if (!snapshot.some((item) => item.value !== item.baseline)) pending.delete(id);
    else pending.set(id, values);
  }
  try {
    await api(`/api/documents/${encodeURIComponent(state.document.document_id)}/ai-first/review/${action}`, {
      method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body),
    });
  } catch (error) {
    const bulkAction = action === "bulk-confirm" || action === "bulk-role";
    const message = error?.code === "bulk_selection_has_open_issues"
      ? "Групповое действие не выполнено: сначала разрешите замечания или проверьте неопределённые значения в выбранных строках."
      : bulkAction ? "Групповое действие не сохранено. Выбор строк оставлен; проверьте соединение и повторите." : (error.message || "Не удалось сохранить решение.");
    toast(message, "error");
    return {saved:false,refreshed:false};
  }
  try {
    state.aiFirst.data = await api(`/api/documents/${encodeURIComponent(state.document.document_id)}/ai-first`);
    for (const id of affected) {
      delete state.aiFirst.pendingRowValues?.[id];
      delete state.aiFirst.pendingRowRoles?.[id];
    }
    if (body.issue_id) delete state.aiFirst.pendingIssueNotes?.[body.issue_id];
    renderAIFirstReview();
    for (const [id, values] of pending) {
      if (affected.has(id)) continue;
      for (const input of document.querySelectorAll("#ai-first-table-scroll [data-ai-first-cell]")) {
        if (input.dataset.aiFirstCell === id) input.value = values[`cell:${input.dataset.columnIndex}`] ?? input.value;
      }
      updateAIFirstRenderedRowState(id);
    }
    toast("Изменение сохранено в черновике и добавлено в историю проверки.", "success");
    return {saved:true,refreshed:true};
  } catch (_) {
    toast("Изменение сохранено, но не удалось обновить данные. Перезагрузите документ перед продолжением.", "warning");
    return {saved:true,refreshed:false};
  }
}

async function confirmAIFirstPageCompleteness(pageNumber) {
  const unresolvedRows = aiFirstRowsWithoutReviewedRole(pageNumber);
  if (unresolvedRows.length && !confirm(
    `На странице ${pageNumber} осталось ${unresolvedRows.length} строк без подтверждённой человеком роли. Они сохранятся в исходных строках и не станут позициями автоматически. Подтвердить полноту проверки страницы?`
  )) return {saved:false,refreshed:false};
  return postAIFirstReview("complete-page", {page_number:Number(pageNumber)});
}

async function confirmSelectedAIFirstRows() {
  const recordIds = aiFirstVisibleSelectedIds();
  if (!recordIds.length) return;
  const changedRows = recordIds.filter((id) => aiFirstRowInputSnapshot(id).some((item) => item.value !== item.baseline));
  if (changedRows.length) {
    toast(`В ${changedRows.length} выбранных строках есть несохранённые правки. Сохраните каждую строку отдельно, затем подтвердите их вместе.`, "error");
    return;
  }
  if (!confirm(`Подтвердить ${recordIds.length} выбранных строк? Будут подтверждены значения, показанные в таблице; несохранённых правок нет. Сначала сверьте строки с PDF.`)) return;
  const result = await postAIFirstReview("bulk-confirm", {record_ids:recordIds});
  if (!result?.saved) return;
  for (const id of recordIds) state.aiFirst.selectedRows.delete(id);
  if (result.refreshed) renderAIFirstReview();
  else {
    document.querySelectorAll("#ai-first-table-scroll [data-ai-first-select-row]").forEach((input) => { input.checked = false; });
    const confirmButton = document.querySelector("#ai-first-table-scroll #ai-first-confirm-selected");
    if (confirmButton) confirmButton.disabled = true;
  }
}

async function assignSelectedAIFirstRole() {
  const root = document.querySelector("#ai-first-table-scroll");
  const recordIds = aiFirstVisibleSelectedIds();
  if (!root || !recordIds.length) return;
  const role = root.querySelector("#ai-first-bulk-role")?.value;
  if (!["standalone_item", "header", "column_numbering", "section", "continuation", "note"].includes(role)) return;
  const changedValues = recordIds.filter((id) => aiFirstRowInputSnapshot(id).some((item) => item.value !== item.baseline));
  const changedRoles = recordIds.filter((id) => state.aiFirst.pendingRowRoles?.[id]);
  if (changedValues.length || changedRoles.length) {
    toast("Сначала сохраните или отмените индивидуальные правки выбранных строк, затем применяйте общую роль.", "error");
    return;
  }
  const roleLabel = root.querySelector("#ai-first-bulk-role option:checked")?.textContent || role;
  if (!confirm(`Назначить роль «${roleLabel}» для ${recordIds.length} выбранных строк? Каждая строка останется исходной, а роль будет записана в историю проверки.`)) return;
  const result = await postAIFirstReview("bulk-role", {record_ids:recordIds,role});
  if (!result?.saved) return;
  for (const id of recordIds) state.aiFirst.selectedRows.delete(id);
  if (result.refreshed) renderAIFirstReview();
  else for (const input of root.querySelectorAll("[data-ai-first-select-row]")) {
    if (recordIds.includes(input.dataset.aiFirstSelectRow)) input.checked = false;
  }
  updateAIFirstBulkControls();
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
  if (aiFirstHasUnsavedReviewEdits()) {
    toast("Черновик не сохранён: сначала сохраните или отмените все введённые правки, роли, замечания и новую позицию. Изменения, оставшиеся только на экране, не попадут в файл.", "warning");
    return {saved:false,reason:"unsaved_dom_changes"};
  }
  try {
    await api(`/api/documents/${encodeURIComponent(state.document.document_id)}/ai-first/review/save`, {method:"POST"});
    toast("Проверенный результат и история исправлений сохранены локально.", "success");
  } catch (error) { toast(error.message || "Не удалось сохранить результат.", "error"); }
}

function aiFirstHasUnsavedReviewEdits() {
  if (Object.keys(state.aiFirst.pendingRowValues || {}).length
      || Object.keys(state.aiFirst.pendingRowRoles || {}).length
      || Object.keys(state.aiFirst.pendingIssueNotes || {}).length
      || Object.values(state.aiFirst.addRowValues || {}).some((value) => String(value || "").trim())) return true;
  const recordIds = new Set([...document.querySelectorAll("#ai-first-table-scroll [data-ai-first-cell]")].map((item) => item.dataset.aiFirstCell));
  for (const recordId of recordIds) {
    if (aiFirstRowInputSnapshot(recordId).some((item) => item.value !== item.baseline)) return true;
  }
  for (const select of document.querySelectorAll("#ai-first-table-scroll [data-ai-first-row-role]")) {
    const role = state.aiFirst.data?.review?.row_roles?.[select.dataset.aiFirstRowRole];
    if (select.value !== (role?.active ? role.role : "unknown")) return true;
    const parent = document.querySelector(`[data-ai-first-parent-row="${CSS.escape(select.dataset.aiFirstRowRole)}"]`);
    if (parent && !parent.hidden && parent.value !== (role?.active ? role.parent_record_id || "" : "")) return true;
  }
  if ([...document.querySelectorAll("#ai-first-page-issues [data-ai-first-issue-note]")].some((input) => input.value.trim())) return true;
  if ([...document.querySelectorAll("#ai-first-add-row-form [data-ai-first-new-cell]")].some((input) => input.value.trim())) return true;
  return false;
}

async function downloadAIFirstPilotXlsx() {
  const pages = [...(state.aiFirst.exportPages || [])].sort((a,b)=>a-b);
  if (!pages.length || pages.length > 3) {
    toast("Выберите от одной до трёх обработанных страниц в сводке.", "error");
    return;
  }
  if (aiFirstHasUnsavedReviewEdits()) {
    toast("Сначала сохраните или отмените несохранённые правки и изменения ролей.", "error");
    return;
  }
  const button = document.querySelector("#ai-first-export-pilot");
  button.disabled = true;
  try {
    const headers = new Headers({"Content-Type":"application/json"});
    const csrfToken = typeof readCsrfCookie === "function" ? readCsrfCookie() : "";
    if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
    const response = await fetch(`/api/documents/${encodeURIComponent(state.document.document_id)}/ai-first/pilot-export`, {
      method:"POST", headers, body:JSON.stringify({page_numbers:pages}),
    });
    if (!response.ok) {
      let message = "Не удалось подготовить тестовый XLSX.";
      try {
        const payload = await response.json();
        message = payload?.detail?.message || payload?.detail || message;
      } catch (_) {}
      throw new Error(typeof message === "string" ? message : "Не удалось подготовить тестовый XLSX.");
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `Averon_AI-first_TEST_${String(state.document.document_id).slice(0, 8)}.xlsx`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("Тестовый XLSX сформирован из сохранённых решений проверки.", "success");
  } catch (error) {
    toast(error.message || "Не удалось подготовить тестовый XLSX.", "error");
  } finally {
    button.disabled = pages.length === 0;
  }
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
  document.querySelector("#ai-first-export-pilot").addEventListener("click", () => void downloadAIFirstPilotXlsx());
  document.querySelector("#ai-first-confirm-page").addEventListener("click", async () => {
    await confirmAIFirstPageCompleteness(state.aiFirst.page);
  });
  document.querySelector("#ai-first-add-row").addEventListener("click", () => {
    state.aiFirst.addRowOpen = !state.aiFirst.addRowOpen;
    if (!state.aiFirst.addRowOpen) state.aiFirst.addRowValues = {};
    renderAIFirstAddRowForm();
    document.querySelector("#ai-first-add-row").textContent = state.aiFirst.addRowOpen ? "Отменить добавление" : "+ Добавить позицию";
  });
  document.querySelector("#ai-first-page-select").addEventListener("change", (event) => showAIFirstPage(Number(event.target.value)));
  document.querySelector("#ai-first-prev-page").addEventListener("click", () => showAIFirstPage(state.aiFirst.page - 1));
  document.querySelector("#ai-first-next-page").addEventListener("click", () => showAIFirstPage(state.aiFirst.page + 1));
  document.querySelector("#ai-first-open-saved").addEventListener("click", async () => {
    if (!state.document) return;
    try {
      const data = await api(`/api/documents/${encodeURIComponent(state.document.document_id)}/ai-first`);
      state.aiFirst.data = data;
      if (!(data.pages || []).some((page) => page.state === "completed")) {
        renderAIFirstRunFeedback(data, true);
        setView("pages");
        toast("В сохранённом запуске нет успешно обработанных страниц. Документ не открыт как готовая проверка.", "error");
        return;
      }
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
  if (next) showAIFirstPage(next);
}

document.addEventListener("DOMContentLoaded", installAIFirstUIEvents, {once:true});
