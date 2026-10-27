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
  const roleInput = document.querySelector(`#ai-first-row-inspector [data-ai-first-row-role="${CSS.escape(row.id)}"]`);
  const parentInput = document.querySelector(`#ai-first-row-inspector [data-ai-first-parent-row="${CSS.escape(row.id)}"]`);
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
  if (reviewState.status === "attention" && reviewState.decisionCurrent && !reviewState.roleVerified
      && reviewState.pageReady && reviewState.evidenceIssueExists !== false && !(reviewState.openIssues || []).length) {
    return "Значения проверены · тип строки не подтверждён";
  }
  if (reviewState.status !== "checked") return reviewState.label;
  if (reviewState.exportReady) return "Значения проверены · допущена в тестовый XLSX";
  if (!reviewState.pageComplete) return "Значения проверены · полнота страницы не подтверждена";
  if (reviewState.role?.role === "component") return "Значения проверены · компонент комплекта";
  if (reviewState.role?.role !== "standalone_item") return "Значения проверены · не самостоятельная позиция";
  return "Значения проверены · допуск к тестовому XLSX не подтверждён";
}

function aiFirstStatusPillLabel(reviewState, excluded = false) {
  if (reviewState.status === "checked") return "Проверено";
  if (reviewState.status === "unsaved") return "Не сохранено";
  if (excluded) return "Исключена";
  if (reviewState.status === "neutral") return "Роль назначена";
  if (reviewState.status === "attention" && reviewState.decisionCurrent && !reviewState.roleVerified
      && reviewState.pageReady && reviewState.evidenceIssueExists !== false && !(reviewState.openIssues || []).length) {
    return "Тип не подтверждён";
  }
  return "Внимание";
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
  rowElement.className = `ai-first-row--${reviewState.status} ${excluded ? "is-excluded" : ""} ${recordId === state.aiFirst.inspectorRowId ? "active" : ""}`;
  rowElement.dataset.reviewState = reviewState.status;
  const label = rowElement.querySelector("[data-ai-first-row-status]");
  if (label) label.textContent = aiFirstRowStatusLabel(reviewState);
  const statusPill = rowElement.querySelector(".ai-first-status-pill");
  if (statusPill) {
    const excluded = Boolean(aiFirstDecision(recordId)?.active && aiFirstDecision(recordId)?.disposition === "excluded");
    statusPill.className = `ai-first-status-pill ai-first-status-pill--${reviewState.status}`;
    statusPill.textContent = aiFirstStatusPillLabel(reviewState, excluded);
  }
  if (recordId === state.aiFirst.inspectorRowId) {
    const inspectorStatus = document.querySelector("#ai-first-row-inspector .ai-first-inspector-heading h4");
    if (inspectorStatus) inspectorStatus.textContent = aiFirstRowStatusLabel(reviewState);
  }
  for (const input of rowElement.querySelectorAll("[data-ai-first-cell]")) {
    const cell = input.closest("td");
    if (!cell) continue;
    cell.classList.remove("ai-first-cell--attention", "ai-first-cell--checked", "ai-first-cell--unsaved");
    if (reviewState.status === "attention") cell.classList.add("ai-first-cell--attention");
    else if (reviewState.status === "checked") cell.classList.add("ai-first-cell--checked");
    else if (reviewState.status === "unsaved") cell.classList.add("ai-first-cell--unsaved");
  }
  renderAIFirstReviewFooter(aiFirstDocumentEntries(state.aiFirst.data.draft, state.aiFirst.data.review));
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

const AI_FIRST_PDF_MIN_ZOOM = 0.2;
const AI_FIRST_PDF_MAX_ZOOM = 2.4;
const AI_FIRST_PDF_ZOOM_STEP = 0.2;

function setAIFirstPdfZoom(value, {fitWidth = false} = {}) {
  state.aiFirst.pdfFitWidth = fitWidth;
  state.aiFirst.pdfZoom = Math.max(AI_FIRST_PDF_MIN_ZOOM, Math.min(AI_FIRST_PDF_MAX_ZOOM, Number(value) || 1));
  renderAIFirstPdfZoom();
}

function renderAIFirstPdfZoom() {
  const image = document.querySelector("#ai-first-pdf-image");
  const stage = document.querySelector(".ai-first-pdf-stage");
  const wrap = document.querySelector(".ai-first-pdf-image-wrap");
  const label = document.querySelector("#ai-first-zoom-label");
  if (!image?.naturalWidth || !stage || !wrap || !label) return;
  state.aiFirst.pdfFitWidth ??= true;
  state.aiFirst.pdfZoom ??= 1;
  let zoom = state.aiFirst.pdfZoom;
  if (state.aiFirst.pdfFitWidth) {
    const style = window.getComputedStyle(stage);
    const padding = (Number.parseFloat(style.paddingLeft) || 0) + (Number.parseFloat(style.paddingRight) || 0);
    const available = Math.max(1, stage.clientWidth - padding);
    zoom = Math.max(AI_FIRST_PDF_MIN_ZOOM, Math.min(AI_FIRST_PDF_MAX_ZOOM, available / image.naturalWidth));
  }
  image.style.width = `${Math.max(1, Math.round(image.naturalWidth * zoom))}px`;
  image.style.height = "auto";
  wrap.style.width = image.style.width;
  label.textContent = state.aiFirst.pdfFitWidth ? "По ширине" : `${Math.round(zoom * 100)}%`;
}

function nudgeAIFirstPdfZoom(direction) {
  const image = document.querySelector("#ai-first-pdf-image");
  let current = state.aiFirst.pdfZoom || 1;
  if (state.aiFirst.pdfFitWidth && image?.naturalWidth) {
    const stage = document.querySelector(".ai-first-pdf-stage");
    if (stage) {
      const style = window.getComputedStyle(stage);
      const padding = (Number.parseFloat(style.paddingLeft) || 0) + (Number.parseFloat(style.paddingRight) || 0);
      current = Math.max(AI_FIRST_PDF_MIN_ZOOM, Math.min(AI_FIRST_PDF_MAX_ZOOM, (stage.clientWidth - padding) / image.naturalWidth));
    }
  }
  setAIFirstPdfZoom(current + direction * AI_FIRST_PDF_ZOOM_STEP);
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
  const tablePage = document.querySelector("#ai-first-table-page-number");
  if (tablePage) tablePage.textContent = String(pageNumber);
  document.querySelector("#ai-first-pdf-image").src = documentPageImageUrl(state.document.document_id, pageNumber, 200);
  document.querySelector("#ai-first-prev-page").disabled = pageNumber <= 1;
  document.querySelector("#ai-first-next-page").disabled = pageNumber >= pageNumbers.length;
  const page = aiFirstPageRecord(pageNumber), report = aiFirstReportPages(state.aiFirst.data).get(pageNumber);
  const ready = report?.state === "completed" && ["extracted", "no_specification"].includes(page?.status);
  document.querySelector("#ai-first-confirm-page").disabled = !ready || (state.aiFirst.data.review.confirmed_complete_pages || []).includes(pageNumber);
  document.querySelectorAll("[data-ai-first-row]").forEach((row) => row.classList.toggle("active", row.dataset.aiFirstRow === state.aiFirst.inspectorRowId));
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

function installAIFirstUnsavedNavigationGuard() {
  if (typeof window === "undefined" || window.__averonAIFirstUnsavedGuardInstalled) return;
  window.__averonAIFirstUnsavedGuardInstalled = true;
  window.addEventListener("beforeunload", (event) => {
    if (!state.aiFirst?.data || !aiFirstHasUnsavedReviewEdits()) return;
    event.preventDefault();
    event.returnValue = "";
  });
}

function renderAIFirstReview() {
  const data = state.aiFirst.data;
  if (!data?.draft || !state.document) return;
  installAIFirstUnsavedNavigationGuard();
  state.aiFirst.reviewGeneration = (state.aiFirst.reviewGeneration || 0) + 1;
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
  const confirmedPages = (review.confirmed_complete_pages || []).length;
  const processedPages = pageNumbers.filter((number) => pageReports.get(number)?.state === "completed" && ["extracted","no_specification"].includes(aiFirstPageRecord(number)?.status)).length;
  const failedPages = (data.pages || []).filter((item) => item.state !== "completed" && (item.state !== "pending" || item.safe_error_code)).length;
  document.querySelector("#ai-first-review-status").textContent = `${processedPages} из ${state.document.page_count} страниц обработано · ${totalRows} строк распознано`;
  const summaries = [
    ["Строк распознано", totalRows, ""],
    ["Предметных строк", subjectRows, ""],
    ["Подтверждённые самостоятельные позиции", confirmedItems, ""],
    ["Страницы · полнота", `${processedPages}/${state.document.page_count} · ${confirmedPages} подтверждено`, processedPages < state.document.page_count || confirmedPages < state.document.page_count ? "warning" : ""],
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
    const exportReason = complete ? "" : report?.state === "pending" && !report.safe_error_code
      ? "Сначала обработайте страницу."
      : "Страница недоступна для выгрузки до успешного завершения обработки.";
    const exportControl = `<label class="ai-first-export-page"><input type="checkbox" data-ai-first-export-page="${number}" aria-describedby="ai-first-export-reason-${number}" ${exportChecked} ${complete ? "" : "disabled"}> В тестовый XLSX</label><small class="ai-first-export-disabled-reason" id="ai-first-export-reason-${number}" ${complete ? "hidden" : ""}>${escapeHtml(exportReason)}</small>`;
    return `<div class="ai-first-page-status"><button class="button ghost" type="button" data-ai-first-show-page="${number}">Страница ${number} · ${label}</button><span>${escapeHtml(detail)}${confirmed ? " · полнота подтверждена" : ""}</span>${complete && !confirmed ? `<button class="button text" type="button" data-ai-first-complete-page="${number}">Подтвердить полноту</button>` : ""}${retryable ? `<button class="button text" type="button" data-ai-first-retry-one="${number}">Повторить</button>` : ""}${exportControl}</div>`;
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
  renderAIFirstReviewFooter(entries);
  updateAIFirstExportControls();
  showAIFirstPage(state.aiFirst.page);
}

function renderAIFirstReviewFooter(entries) {
  const root = document.querySelector("#ai-first-review-footer");
  if (!root) return;
  const pageEntries = entries.filter((entry) => Number(entry.page?.page_number || entry.row.page_number) === state.aiFirst.page);
  const renderedStates = [...document.querySelectorAll("#ai-first-table-scroll tr[data-ai-first-row]")].map((row) => row.dataset.reviewState);
  const states = renderedStates.length ? renderedStates : pageEntries.map((entry) => aiFirstRowReviewState(entry).status);
  const checked = states.filter((status) => status === "checked").length;
  const attention = states.filter((status) => status === "attention").length;
  const unsaved = states.filter((status) => status === "unsaved").length;
  const page = aiFirstPageRecord(state.aiFirst.page);
  const report = aiFirstReportPages(state.aiFirst.data).get(state.aiFirst.page);
  const complete = (state.aiFirst.data?.review?.confirmed_complete_pages || []).includes(state.aiFirst.page);
  let next = "Выберите строку и сверьте её с PDF.";
  if (unsaved) next = "Сохраните правки в выбранных строках.";
  else if (attention) next = "Откройте строку с замечанием и выполните проверку.";
  else if (page?.status === "extracted" && !complete) next = "Подтвердите полноту этой страницы.";
  else if (page?.status === "no_specification" && !complete) next = "Сверьте страницу с PDF и подтвердите, что позиций нет.";
  else if (report && report.state !== "completed") next = "Обработка страницы не завершена; изучите причину в сводке страниц.";
  else if (state.aiFirst.exportPages?.size) next = "Проверьте сводку тестового XLSX и скачайте файл.";
  root.innerHTML = `<span>Проверено <b>${checked}</b></span><span>Внимание <b>${attention}</b></span><span>Несохранено <b>${unsaved}</b></span><span class="ai-first-next-action">Далее: ${escapeHtml(next)}</span>`;
}

function updateAIFirstExportControls() {
  const button = document.querySelector("#ai-first-export-pilot");
  const label = document.querySelector("#ai-first-export-note");
  const feedback = document.querySelector("#ai-first-export-feedback");
  const retryButton = document.querySelector("#ai-first-retry-export-preview");
  if (!button || !label) return;
  state.aiFirst.exportPages ||= new Set();
  const selected = [...state.aiFirst.exportPages].sort((a,b)=>a-b);
  const scope = selected.join(",");
  const generation = state.aiFirst.reviewGeneration || 0;
  const key = selected.length ? `${scope}|${generation}` : "";
  if (state.aiFirst.exportPreviewSelectionKey !== key) {
    state.aiFirst.exportPreviewSelectionKey = key;
    state.aiFirst.exportPreviewRequestId = (state.aiFirst.exportPreviewRequestId || 0) + 1;
    state.aiFirst.exportPreview = null;
    state.aiFirst.exportPreviewLoadingKey = null;
    state.aiFirst.exportPreviewErrorKey = null;
    state.aiFirst.exportDownloadError = null;
  }
  if (retryButton) retryButton.hidden = true;
  if (!selected.length) {
    button.disabled = true;
    if (feedback) feedback.hidden = true;
    label.textContent = "";
    return;
  }
  if (feedback) feedback.hidden = false;
  if (selected.length > 3) {
    button.disabled = true;
    label.textContent = "Выберите не более трёх страниц.";
    return;
  }
  if (state.aiFirst.exportPreview?.key === key) {
    button.disabled = false;
    const preview = state.aiFirst.exportPreview.data;
    const lastFailure = state.aiFirst.exportDownloadError ? ` Последняя попытка экспорта: ${state.aiFirst.exportDownloadError}` : "";
    label.textContent = `К проверенным позициям допущено ${preview.verified_position_count}; строк в файле — ${preview.total_row_count} (OCR ${preview.ocr_row_count}, вручную ${preview.manual_row_count}). Предварительная проверка допуска не гарантирует создание файла: XLSX дополнительно проверяется на ограничения Excel.${lastFailure} Выбранные страницы будут отмечены как частичный охват; файл помечен «ТЕСТ» и не предназначен для коммерческого использования.`;
    return;
  }
  if (state.aiFirst.exportPreviewErrorKey === key) {
    button.disabled = true;
    label.textContent = "Не удалось проверить строки. Скачивание заблокировано до успешной повторной проверки.";
    if (retryButton) retryButton.hidden = false;
    return;
  }
  if (state.aiFirst.exportPreviewLoadingKey === key) {
    button.disabled = true;
    label.textContent = "Проверяем выбранные строки перед выгрузкой…";
    return;
  }
  void loadAIFirstPilotExportPreview(selected, key);
}

function retryAIFirstPilotExportPreview() {
  const selected = [...(state.aiFirst.exportPages || [])].sort((a,b)=>a-b);
  if (!selected.length || selected.length > 3) return;
  const key = `${selected.join(",")}|${state.aiFirst.reviewGeneration || 0}`;
  state.aiFirst.exportPreviewRequestId = (state.aiFirst.exportPreviewRequestId || 0) + 1;
  state.aiFirst.exportPreview = null;
  state.aiFirst.exportPreviewErrorKey = null;
  state.aiFirst.exportPreviewLoadingKey = null;
  state.aiFirst.exportDownloadError = null;
  state.aiFirst.exportPreviewSelectionKey = key;
  return loadAIFirstPilotExportPreview(selected, key);
}

async function loadAIFirstPilotExportPreview(pageNumbers, key) {
  const button = document.querySelector("#ai-first-export-pilot");
  const label = document.querySelector("#ai-first-export-note");
  if (!button || !label) return;
  const selection = [...(state.aiFirst.exportPages || [])].sort((a,b)=>a-b);
  const currentKey = selection.length ? `${selection.join(",")}|${state.aiFirst.reviewGeneration || 0}` : "";
  if (key !== currentKey || selection.length > 3) return;
  const requestId = (state.aiFirst.exportPreviewRequestId || 0) + 1;
  state.aiFirst.exportPreviewRequestId = requestId;
  state.aiFirst.exportPreviewLoadingKey = key;
  const retryButton = document.querySelector("#ai-first-retry-export-preview");
  if (retryButton) retryButton.hidden = true;
  button.disabled = true;
  label.textContent = "Проверяем выбранные строки…";
  try {
    const data = await api(`/api/documents/${encodeURIComponent(state.document.document_id)}/ai-first/pilot-export/preview`, {
      method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({page_numbers:pageNumbers}),
    });
    const currentPages = [...(state.aiFirst.exportPages || [])].sort((a,b)=>a-b);
    const currentSelectionKey = currentPages.length ? `${currentPages.join(",")}|${state.aiFirst.reviewGeneration || 0}` : "";
    if (requestId !== state.aiFirst.exportPreviewRequestId || key !== currentSelectionKey) return;
    state.aiFirst.exportPreview = {key,data};
    state.aiFirst.exportPreviewErrorKey = null;
    state.aiFirst.exportPreviewLoadingKey = null;
    updateAIFirstExportControls();
  } catch (_) {
    const currentPages = [...(state.aiFirst.exportPages || [])].sort((a,b)=>a-b);
    const currentSelectionKey = currentPages.length ? `${currentPages.join(",")}|${state.aiFirst.reviewGeneration || 0}` : "";
    if (requestId !== state.aiFirst.exportPreviewRequestId || key !== currentSelectionKey) return;
    state.aiFirst.exportPreviewErrorKey = key;
    state.aiFirst.exportPreviewLoadingKey = null;
    updateAIFirstExportControls();
  }
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
  for (const select of document.querySelectorAll("#ai-first-row-inspector [data-ai-first-row-role]")) {
    const id = select.dataset.aiFirstRowRole, role = state.aiFirst.data?.review?.row_roles?.[id];
    const roleValue = select.value, parentValue = document.querySelector(`#ai-first-row-inspector [data-ai-first-parent-row="${CSS.escape(id)}"]`)?.value || "";
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
}

function captureAIFirstViewState() {
  const root = document.querySelector("#ai-first-table-scroll");
  const inspector = document.querySelector("#ai-first-row-inspector");
  const active = document.activeElement, dataset = active?.dataset || {};
  let focus = null;
  if (dataset.aiFirstInspectorCell) focus = {type:"inspector-cell",id:dataset.aiFirstInspectorCell,column:dataset.columnIndex};
  else if (dataset.aiFirstCell) focus = {type:"cell",id:dataset.aiFirstCell,column:dataset.columnIndex};
  else if (dataset.aiFirstOpenRow) focus = {type:"open-row",id:dataset.aiFirstOpenRow};
  else if (dataset.aiFirstRowRole) focus = {type:"role",id:dataset.aiFirstRowRole};
  else if (dataset.aiFirstParentRow) focus = {type:"parent",id:dataset.aiFirstParentRow};
  else if (dataset.aiFirstSelectRow) focus = {type:"select-row",id:dataset.aiFirstSelectRow};
  else if (dataset.aiFirstSaveRow) focus = {type:"row-action",attribute:"save-row",id:dataset.aiFirstSaveRow};
  else if (dataset.aiFirstConfirmRow) focus = {type:"row-action",attribute:"confirm-row",id:dataset.aiFirstConfirmRow};
  else if (dataset.aiFirstExcludeRow) focus = {type:"row-action",attribute:"exclude-row",id:dataset.aiFirstExcludeRow};
  else if (dataset.aiFirstSaveRole) focus = {type:"row-action",attribute:"save-role",id:dataset.aiFirstSaveRole};
  else if (["ai-first-search", "ai-first-review-filter", "ai-first-bulk-role", "ai-first-page-select"].includes(active?.id)) focus = {type:"id",id:active.id};
  return {scrollTop:root?.scrollTop || 0,scrollLeft:root?.scrollLeft || 0,inspectorScrollTop:inspector?.scrollTop || 0,focus,selectionStart:active?.selectionStart,selectionEnd:active?.selectionEnd};
}

function restoreAIFirstViewState(view) {
  const root = document.querySelector("#ai-first-table-scroll");
  const inspector = document.querySelector("#ai-first-row-inspector");
  if (root) root.scrollTop = view?.scrollTop || 0;
  if (root) root.scrollLeft = view?.scrollLeft || 0;
  if (inspector) inspector.scrollTop = view?.inspectorScrollTop || 0;
  if (!view?.focus) return;
  let target = null;
  if (view.focus.type === "inspector-cell") target = inspector?.querySelector(`[data-ai-first-inspector-cell="${CSS.escape(view.focus.id)}"][data-column-index="${CSS.escape(view.focus.column)}"]`);
  else if (view.focus.type === "cell") target = root?.querySelector(`[data-ai-first-cell="${CSS.escape(view.focus.id)}"][data-column-index="${CSS.escape(view.focus.column)}"]`);
  else if (view.focus.type === "open-row") target = root?.querySelector(`[data-ai-first-open-row="${CSS.escape(view.focus.id)}"]`);
  else if (view.focus.type === "role") target = document.querySelector(`#ai-first-row-inspector [data-ai-first-row-role="${CSS.escape(view.focus.id)}"]`);
  else if (view.focus.type === "parent") target = document.querySelector(`#ai-first-row-inspector [data-ai-first-parent-row="${CSS.escape(view.focus.id)}"]`);
  else if (view.focus.type === "select-row") target = root?.querySelector(`[data-ai-first-select-row="${CSS.escape(view.focus.id)}"]`);
  else if (view.focus.type === "row-action") target = document.querySelector(`#ai-first-row-inspector [data-ai-first-${view.focus.attribute}="${CSS.escape(view.focus.id)}"]`);
  else if (view.focus.type === "id") target = document.getElementById(view.focus.id);
  target?.focus({preventScroll:true});
  if (target && Number.isInteger(view.selectionStart) && typeof target.setSelectionRange === "function") {
    target.setSelectionRange(view.selectionStart, view.selectionEnd);
  }
  if (root) { root.scrollTop = view?.scrollTop || 0; root.scrollLeft = view?.scrollLeft || 0; }
  if (inspector) inspector.scrollTop = view?.inspectorScrollTop || 0;
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
  const root = document.querySelector("#ai-first-review-controls");
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
  const controls = document.querySelector("#ai-first-review-controls");
  if (!root || !controls) return;
  const query = (controls.querySelector("#ai-first-search")?.value || "").trim().toLocaleLowerCase("ru-RU");
  const filter = controls.querySelector("#ai-first-review-filter")?.value || "all";
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
  const summary = controls.querySelector("[data-ai-first-filter-summary]");
  if (summary) summary.textContent = `${visible} из ${total} строк на странице`;
  updateAIFirstBulkControls();
}

function renderAIFirstInspectorValues(entry) {
  const target = document.querySelector("#ai-first-inspector-values");
  if (!target || !entry) return;
  const {row,columns,manual} = entry;
  const snapshot = aiFirstRowInputSnapshot(row.id);
  const sourceByIndex = new Map((row.raw_cells || []).map((cell) => [Number(cell.column_index), cell.raw_value || ""]));
  if (manual) for (const [key,value] of Object.entries(row.values || {})) {
    const match = /^cell:(\d+)$/.exec(key);
    if (match) sourceByIndex.set(Number(match[1]), value || "");
  }
  target.innerHTML = columns.map((column) => {
    const source = sourceByIndex.get(Number(column.index)) || "";
    const current = snapshot.find((item) => item.key === `cell:${column.index}`)?.value ?? source;
    const label = column.label || `Колонка ${column.index + 1}`;
    return `<article class="ai-first-inspector-value"><h5>${escapeHtml(label)}</h5><div class="ai-first-inspector-source"><small>Исходное распознавание</small><p>${escapeHtml(source || "—")}</p></div><label class="ai-first-inspector-editor-field"><span>Текущий текст</span><textarea class="input" data-ai-first-inspector-cell="${escapeHtml(row.id)}" data-column-index="${column.index}" aria-label="Текущий текст: ${escapeHtml(label)}" maxlength="2000" rows="3">${escapeHtml(current)}</textarea></label></article>`;
  }).join("");
}

function bindAIFirstInspectorValueEditors(recordId) {
  const inspector = document.querySelector("#ai-first-row-inspector");
  for (const editor of inspector?.querySelectorAll("[data-ai-first-inspector-cell]") || []) {
    editor.addEventListener("input", () => {
      const selector = `#ai-first-table-scroll [data-ai-first-cell="${CSS.escape(recordId)}"][data-column-index="${CSS.escape(editor.dataset.columnIndex)}"]`;
      const tableInput = document.querySelector(selector);
      if (tableInput) tableInput.value = editor.value;
      captureAIFirstPendingEdits();
      updateAIFirstRenderedRowState(recordId);
    });
  }
}

function renderAIFirstRowInspector() {
  const root = document.querySelector("#ai-first-row-inspector");
  if (!root) return;
  const recordId = state.aiFirst.inspectorRowId;
  const entry = recordId && state.aiFirst.renderedEntries?.get(recordId);
  if (!entry) { root.hidden = true; root.innerHTML = ""; return; }
  const {row,page,table,manual} = entry;
  const decision = aiFirstDecision(recordId);
  const reviewState = aiFirstRowReviewState(entry);
  const role = state.aiFirst.data?.review?.row_roles?.[recordId];
  const pendingRole = state.aiFirst.pendingRowRoles?.[recordId];
  const selectedRole = pendingRole?.role || (role?.active ? role.role : "unknown");
  const pageNumber = Number(page?.page_number || row.page_number);
  const rowNumber = manual ? "Ручная строка" : `Строка ${Number(row.index ?? 0) + 1}`;
  const parentCandidates = [...(state.aiFirst.renderedEntries?.values() || [])].filter((candidate) => {
    if (candidate.row.id === recordId || Number(candidate.page?.page_number || candidate.row.page_number) !== pageNumber) return false;
    const tableId = table?.id || row.table_id;
    if (tableId && candidate.table?.id && candidate.table.id !== tableId) return false;
    const candidateRole = state.aiFirst.data?.review?.row_roles?.[candidate.row.id];
    return candidateRole?.active && candidateRole.role === "standalone_item";
  });
  const parentValue = pendingRole?.parent_record_id || role?.parent_record_id || "";
  const parentOptions = `<option value="">Выберите самостоятельную позицию</option>${parentCandidates.map((candidate) => `<option value="${escapeHtml(candidate.row.id)}" ${parentValue === candidate.row.id ? "selected" : ""}>Строка ${escapeHtml(candidate.manual ? "ручная" : String(Number(candidate.row.index ?? 0) + 1))}</option>`).join("")}`;
  const roleOptions = [["unknown","Не определена"],["standalone_item","Самостоятельная позиция"],["component","Компонент комплекта"],["header","Заголовок"],["column_numbering","Нумерация колонок"],["section","Раздел"],["continuation","Продолжение строки"],["note","Примечание"]];
  const openIssues = reviewState.openIssues || [];
  const issueHtml = openIssues.length ? `<ul class="ai-first-inspector-issues">${openIssues.map((issue) => `<li><span>${escapeHtml(aiFirstIssueMessage(issue.code))}</span><button class="button text" type="button" data-ai-first-inspector-issue="${escapeHtml(issue.id)}">К замечанию</button></li>`).join("")}</ul>` : `<p class="ai-first-inspector-clear">Открытых замечаний по этой строке нет.</p>`;
  const status = aiFirstRowStatusLabel(reviewState);
  const excluded = decision?.active && decision.disposition === "excluded";
  root.hidden = false;
  root.innerHTML = `<header class="ai-first-inspector-heading"><div><small>Страница ${pageNumber} · ${escapeHtml(rowNumber)}</small><h4>${escapeHtml(status)}</h4></div><button class="button ghost" type="button" data-ai-first-close-inspector aria-label="Закрыть проверку строки">Закрыть</button></header><div id="ai-first-inspector-values" class="ai-first-inspector-values"></div><div class="ai-first-inspector-review"><div class="ai-first-inspector-role"><label>Роль строки<select class="select" data-ai-first-row-role="${escapeHtml(recordId)}">${roleOptions.map(([value,label]) => `<option value="${value}" ${selectedRole === value ? "selected" : ""}>${label}</option>`).join("")}</select></label><label data-ai-first-parent-wrap ${selectedRole === "component" ? "" : "hidden"}>Родительская позиция<select class="select" data-ai-first-parent-row="${escapeHtml(recordId)}">${parentOptions}</select></label></div><div><b>Замечания</b>${issueHtml}</div></div><div class="ai-first-inspector-actions"><button class="button secondary" type="button" data-ai-first-save-row="${escapeHtml(recordId)}">Сохранить правки</button><button class="button secondary" type="button" data-ai-first-save-role="${escapeHtml(recordId)}">Сохранить роль</button><button class="button primary" type="button" data-ai-first-confirm-row="${escapeHtml(recordId)}">Подтвердить значения</button><button class="button text" type="button" data-ai-first-exclude-row="${escapeHtml(recordId)}">${excluded ? "Вернуть в проверку" : "Исключить"}</button></div>`;
  renderAIFirstInspectorValues(entry);
  bindAIFirstInspectorValueEditors(recordId);
  const roleInput = root.querySelector(`[data-ai-first-row-role="${CSS.escape(recordId)}"]`);
  const parentInput = root.querySelector(`[data-ai-first-parent-row="${CSS.escape(recordId)}"]`);
  roleInput?.addEventListener("change", () => {
    if (parentInput) root.querySelector("[data-ai-first-parent-wrap]").hidden = roleInput.value !== "component";
    state.aiFirst.pendingRowRoles ||= {};
    const baselineRole = role?.active ? role.role : "unknown";
    const baselineParent = role?.active ? role.parent_record_id || "" : "";
    const nextParent = roleInput.value === "component" ? (parentInput?.value || "") : "";
    if (roleInput.value === baselineRole && nextParent === baselineParent) delete state.aiFirst.pendingRowRoles[recordId];
    else state.aiFirst.pendingRowRoles[recordId] = {role:roleInput.value,parent_record_id:roleInput.value === "component" ? nextParent : null};
    updateAIFirstRenderedRowState(recordId);
  });
  parentInput?.addEventListener("change", () => {
    state.aiFirst.pendingRowRoles ||= {};
    const baselineRole = role?.active ? role.role : "unknown";
    const baselineParent = role?.active ? role.parent_record_id || "" : "";
    const nextRole = roleInput?.value || selectedRole;
    if (nextRole === baselineRole && parentInput.value === baselineParent) delete state.aiFirst.pendingRowRoles[recordId];
    else state.aiFirst.pendingRowRoles[recordId] = {role:nextRole,parent_record_id:parentInput.value};
    updateAIFirstRenderedRowState(recordId);
  });
  root.querySelector("[data-ai-first-close-inspector]")?.addEventListener("click", () => {
    captureAIFirstPendingEdits();
    if ((aiFirstRowInputSnapshot(recordId).some((item) => item.value !== item.baseline)
      || state.aiFirst.pendingRowRoles?.[recordId])
      && !confirm("Несохранённые правки останутся в строке таблицы и будут отмечены как несохранённые. Закрыть редактор?")) return;
    state.aiFirst.inspectorRowId = null;
    renderAIFirstRowInspector();
    document.querySelectorAll("#ai-first-table-scroll tr[data-ai-first-row]").forEach((item) => item.classList.remove("active"));
  });
  root.querySelectorAll("[data-ai-first-inspector-issue]").forEach((button) => button.addEventListener("click", () => {
    const note = document.querySelector(`#ai-first-page-issues [data-ai-first-issue-note="${CSS.escape(button.dataset.aiFirstInspectorIssue)}"]`);
    note?.scrollIntoView({block:"nearest"});
    note?.focus({preventScroll:true});
  }));
  root.querySelector("[data-ai-first-save-row]")?.addEventListener("click", () => {
    const values = Object.fromEntries(aiFirstRowInputSnapshot(recordId).map((item) => [item.key,item.value]));
    void postAIFirstReview("row", {record_id:recordId,disposition:"corrected",confirmed:false,values});
  });
  root.querySelector("[data-ai-first-save-role]")?.addEventListener("click", () => {
    const snapshot = aiFirstRowInputSnapshot(recordId);
    if (snapshot.some((item) => item.value !== item.baseline)) {
      toast("Сначала сохраните или отмените правки ячеек этой строки.", "error");
      return;
    }
    const roleValue = roleInput?.value;
    const parent = parentInput?.value || null;
    if (roleValue === "component" && !parent) {
      toast("Для компонента выберите самостоятельную позицию-родителя на этой странице.", "error");
      return;
    }
    void postAIFirstReview("row-role", {record_id:recordId,role:roleValue,parent_record_id:roleValue === "component" ? parent : null});
  });
  root.querySelector("[data-ai-first-confirm-row]")?.addEventListener("click", () => {
    const snapshot = aiFirstRowInputSnapshot(recordId);
    const changed = snapshot.filter((item) => item.value !== item.baseline);
    const rolePending = Boolean(state.aiFirst.pendingRowRoles?.[recordId]);
    if (rolePending) { toast("Сначала сохраните выбранную роль строки.", "error"); return; }
    const values = snapshot.map((item) => `${item.label}: ${item.value || "пусто"}`).join("\n");
    const question = changed.length
      ? `Сохранить несохранённые изменения и подтвердить эту строку?\n\n${changed.map((item) => `${item.label}: ${item.baseline || "пусто"} → ${item.value || "пусто"}`).join("\n")}`
      : `Подтвердить показанные значения после сверки с PDF?\n\n${values}`;
    if (!confirm(question)) return;
    void postAIFirstReview("row", {record_id:recordId,...aiFirstRowConfirmationPayload(snapshot)});
  });
  root.querySelector("[data-ai-first-exclude-row]")?.addEventListener("click", () => {
    if (aiFirstRowInputSnapshot(recordId).some((item) => item.value !== item.baseline) || state.aiFirst.pendingRowRoles?.[recordId]) {
      toast("Сначала сохраните или отмените несохранённые правки и роль.", "error");
      return;
    }
    const current = aiFirstDecision(recordId);
    const disposition = current?.active && current.disposition === "excluded" ? "confirmed" : "excluded";
    const snapshot = aiFirstRowInputSnapshot(recordId);
    if (disposition === "excluded") {
      if (!confirm("Исключить строку из проверенных позиций? Она останется в исходных распознанных данных и истории.")) return;
      void postAIFirstReview("row", {record_id:recordId,disposition,values:{}});
      return;
    }
    const values = aiFirstRowValues(snapshot), changed = Object.keys(current?.values || {}).length > 0;
    if (!confirm("Вернуть строку в проверку со сохранёнными значениями? Сверьте их с PDF.")) return;
    void postAIFirstReview("row", {record_id:recordId,disposition:changed ? "corrected" : "confirmed",confirmed:true,values});
  });
}

function selectAIFirstRow(recordId, columnIndex = null) {
  captureAIFirstPendingEdits();
  const previousId = state.aiFirst.inspectorRowId;
  if (previousId && previousId !== recordId
      && (aiFirstRowInputSnapshot(previousId).some((item) => item.value !== item.baseline)
        || state.aiFirst.pendingRowRoles?.[previousId])) {
    toast("Несохранённые правки предыдущей строки сохранены в текущем черновике на экране.", "warning");
  }
  state.aiFirst.inspectorRowId = recordId;
  document.querySelectorAll("#ai-first-table-scroll tr[data-ai-first-row]").forEach((row) => row.classList.toggle("active", row.dataset.aiFirstRow === recordId));
  renderAIFirstRowInspector();
  void locateAIFirstSource(recordId, columnIndex);
}

function renderAIFirstTables(entries) {
  const root = document.querySelector("#ai-first-table-scroll");
  const controls = document.querySelector("#ai-first-review-controls");
  if (!root || !controls) return;
  captureAIFirstPendingEdits();
  const view = captureAIFirstViewState();
  const pageEntries = entries.filter((entry) => Number(entry.page?.page_number || entry.row.page_number) === state.aiFirst.page);
  state.aiFirst.renderedEntries = new Map(pageEntries.map((entry) => [entry.row.id, entry]));
  state.aiFirst.inspectorRowId ||= null;
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
      const rowPage = Number(page?.page_number || row.page_number);
      const kindLabel = ({item:"Позиция",item_candidate:"Возможная позиция",header:"Заголовок",section:"Раздел",note:"Примечание"})[row.model_kind] || (manual ? "Ручная позиция" : "Строка документа");
      const rowLabel = manual ? "Ручная" : String((row.index ?? 0) + 1);
      const searchable = aiFirstEntrySearchText(entry);
      const selectedRole = state.aiFirst.pendingRowRoles?.[row.id]?.role || state.aiFirst.data.review.row_roles?.[row.id]?.role || "unknown";
      const statusPillLabel = aiFirstStatusPillLabel(reviewState, excluded);
      return `<tr data-ai-first-row="${escapeHtml(row.id)}" data-page-number="${rowPage}" data-review-state="${reviewState.status}" data-role="${escapeHtml(selectedRole)}" data-disposition="${escapeHtml(decision?.disposition || "")}" data-search-text="${escapeHtml(searchable)}" class="ai-first-row--${reviewState.status} ${excluded ? "is-excluded" : ""} ${state.aiFirst.inspectorRowId === row.id ? "active" : ""}"><td class="selector"><input type="checkbox" data-ai-first-select-row="${escapeHtml(row.id)}" aria-label="Выбрать строку" ${state.aiFirst.selectedRows.has(row.id) ? "checked" : ""}></td><td><button class="button text ai-first-open-row" type="button" data-ai-first-open-row="${escapeHtml(row.id)}" aria-expanded="${state.aiFirst.inspectorRowId === row.id ? "true" : "false"}">${escapeHtml(rowLabel)} · Проверить</button><small class="ai-first-cell-source">${escapeHtml(kindLabel)} · <span data-ai-first-row-status>${escapeHtml(aiFirstRowStatusLabel(reviewState))}</span></small></td><td><button class="button text ai-first-source-page" type="button" data-ai-first-show-page="${rowPage}">Стр. ${rowPage}</button></td>${cells}<td><span class="ai-first-status-pill ai-first-status-pill--${reviewState.status}">${statusPillLabel}</span></td></tr>`;
    }).join("");
    const title = group.manual ? "Добавленные вручную" : `Таблица ${Number(group.table?.index ?? 0) + 1}`;
    const count = group.entries.length;
    return `<section class="ai-first-source-table-group" data-ai-first-table-group="${escapeHtml(group.key)}"><h4>${escapeHtml(title)} <small>${count} строк</small></h4><table class="data-table ai-first-table"><thead><tr><th class="selector">Выбор</th><th>Строка</th><th>Страница</th>${headers}<th>Проверка</th></tr></thead><tbody>${rows}</tbody></table></section>`;
  }).join("");
  const options = `<option value="all">Все строки</option><option value="attention">Требуют внимания</option><option value="unassigned">Роль не назначена</option><option value="checked">Проверены</option><option value="corrected">Исправлены</option><option value="excluded">Исключены</option><option value="service">Служебные строки</option>`;
  const bulkRoles = [["standalone_item","Самостоятельная позиция"],["header","Заголовок"],["column_numbering","Нумерация колонок"],["section","Раздел"],["continuation","Продолжение строки"],["note","Примечание"]];
  root.innerHTML = groupHtml || `<div class="empty-table">На этой странице распознанных строк нет.</div>`;
  controls.innerHTML = `<div class="ai-first-row-toolbar"><label class="ai-first-search-label">Поиск по строкам<input class="input" id="ai-first-search" type="search" value="${escapeHtml(state.aiFirst.reviewSearch || "")}" placeholder="Наименование, марка или артикул"></label><label>Показать<select class="select" id="ai-first-review-filter">${options}</select></label><button class="button ghost" type="button" data-ai-first-next-attention>Следующая для проверки</button><span data-ai-first-filter-summary role="status" aria-live="polite"></span></div><div class="ai-first-bulk-toolbar"><label>Роль выбранных<select class="select" id="ai-first-bulk-role">${bulkRoles.map(([value,label]) => `<option value="${value}">${label}</option>`).join("")}</select></label><button class="button secondary" id="ai-first-confirm-selected" type="button" disabled>Подтвердить выбранные</button><button class="button secondary" type="button" data-ai-first-bulk-role-submit disabled>Применить роль</button><span data-ai-first-selected-count>Выберите строки для группового действия</span></div>`;
  const filter = controls.querySelector("#ai-first-review-filter");
  if (filter) filter.value = state.aiFirst.reviewFilter || "all";
  controls.querySelector("#ai-first-search")?.addEventListener("input", applyAIFirstReviewFilters);
  filter?.addEventListener("change", applyAIFirstReviewFilters);
  root.querySelectorAll("[data-ai-first-select-row]").forEach((input) => input.addEventListener("change", () => {
    const id = input.dataset.aiFirstSelectRow;
    if (input.checked) state.aiFirst.selectedRows.add(id); else state.aiFirst.selectedRows.delete(id);
    updateAIFirstBulkControls();
  }));
  root.querySelectorAll("[data-ai-first-cell]").forEach((input) => {
    input.addEventListener("input", () => {
      updateAIFirstRenderedRowState(input.dataset.aiFirstCell);
      if (input.dataset.aiFirstCell === state.aiFirst.inspectorRowId) {
        const selector = `#ai-first-row-inspector [data-ai-first-inspector-cell="${CSS.escape(input.dataset.aiFirstCell)}"][data-column-index="${CSS.escape(input.dataset.columnIndex)}"]`;
        const editor = document.querySelector(selector);
        if (editor) editor.value = input.value;
      }
    });
  });
  root.querySelectorAll("[data-ai-first-row]").forEach((row) => row.addEventListener("click", (event) => {
    if (event.target.closest("button, input, select, textarea, summary, details")) return;
    selectAIFirstRow(row.dataset.aiFirstRow, null);
  }));
  root.querySelectorAll("[data-ai-first-open-row]").forEach((button) => button.addEventListener("click", () => selectAIFirstRow(button.dataset.aiFirstOpenRow, null)));
  root.querySelectorAll("[data-ai-first-cell]").forEach((input) => input.addEventListener("click", (event) => {
    event.stopPropagation();
    selectAIFirstRow(input.dataset.aiFirstCell, Number(input.dataset.columnIndex));
  }));
  root.querySelectorAll("[data-ai-first-show-page]").forEach((button) => button.addEventListener("click", (event) => {
    event.stopPropagation();
    showAIFirstPage(Number(button.dataset.aiFirstShowPage));
  }));
  controls.querySelector("[data-ai-first-next-attention]")?.addEventListener("click", () => {
    const rows = [...root.querySelectorAll("tr[data-ai-first-row]")].filter((row) => !row.hidden && ["attention", "unsaved"].includes(row.dataset.reviewState));
    if (!rows.length) { toast("В текущем фильтре нет строк, требующих внимания.", "warning"); return; }
    const current = rows.findIndex((row) => row.dataset.aiFirstRow === state.aiFirst.lastAttentionRow);
    const next = rows[(current + 1) % rows.length];
    state.aiFirst.lastAttentionRow = next.dataset.aiFirstRow;
    next.scrollIntoView({block:"center"});
    selectAIFirstRow(next.dataset.aiFirstRow, null);
  });
  controls.querySelector("#ai-first-confirm-selected")?.addEventListener("click", () => void confirmSelectedAIFirstRows());
  controls.querySelector("[data-ai-first-bulk-role-submit]")?.addEventListener("click", () => void assignSelectedAIFirstRole());
  applyAIFirstReviewFilters();
  renderAIFirstRowInspector();
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
      if (action === "row" || action === "bulk-confirm") delete state.aiFirst.pendingRowValues?.[id];
      if (action === "row-role" || action === "bulk-role") delete state.aiFirst.pendingRowRoles?.[id];
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
    const confirmButton = document.querySelector("#ai-first-review-controls #ai-first-confirm-selected");
    if (confirmButton) confirmButton.disabled = true;
  }
}

async function assignSelectedAIFirstRole() {
  const root = document.querySelector("#ai-first-table-scroll");
  const controls = document.querySelector("#ai-first-review-controls");
  const recordIds = aiFirstVisibleSelectedIds();
  if (!root || !controls || !recordIds.length) return;
  const role = controls.querySelector("#ai-first-bulk-role")?.value;
  if (!["standalone_item", "header", "column_numbering", "section", "continuation", "note"].includes(role)) return;
  const changedValues = recordIds.filter((id) => aiFirstRowInputSnapshot(id).some((item) => item.value !== item.baseline));
  const changedRoles = recordIds.filter((id) => state.aiFirst.pendingRowRoles?.[id]);
  if (changedValues.length || changedRoles.length) {
    toast("Сначала сохраните или отмените индивидуальные правки выбранных строк, затем применяйте общую роль.", "error");
    return;
  }
  const roleLabel = controls.querySelector("#ai-first-bulk-role option:checked")?.textContent || role;
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
  for (const select of document.querySelectorAll("#ai-first-row-inspector [data-ai-first-row-role]")) {
    const role = state.aiFirst.data?.review?.row_roles?.[select.dataset.aiFirstRowRole];
    if (select.value !== (role?.active ? role.role : "unknown")) return true;
    const parent = document.querySelector(`#ai-first-row-inspector [data-ai-first-parent-row="${CSS.escape(select.dataset.aiFirstRowRole)}"]`);
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
  state.aiFirst.exportDownloadError = null;
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
    const message = error.message || "Не удалось подготовить тестовый XLSX.";
    state.aiFirst.exportDownloadError = message;
    toast(message, "error");
  } finally {
    updateAIFirstExportControls();
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
  document.querySelector("#ai-first-pdf-image").addEventListener("load", renderAIFirstPdfZoom);
  document.querySelector("#ai-first-zoom-out").addEventListener("click", () => nudgeAIFirstPdfZoom(-1));
  document.querySelector("#ai-first-zoom-in").addEventListener("click", () => nudgeAIFirstPdfZoom(1));
  document.querySelector("#ai-first-zoom-fit").addEventListener("click", () => setAIFirstPdfZoom(state.aiFirst.pdfZoom || 1, {fitWidth:true}));
  document.querySelector("#ai-first-zoom-100").addEventListener("click", () => setAIFirstPdfZoom(1));
  window.addEventListener("resize", renderAIFirstPdfZoom);
  document.querySelector("#cancel-ai-first-button").addEventListener("click", () => void cancelAIFirstRecognition());
  document.querySelector("#ai-first-back-to-pages").addEventListener("click", () => {
    captureAIFirstPendingEdits();
    setView("pages");
    renderAIFirstResumeControls();
  });
  document.querySelector("#ai-first-save-review").addEventListener("click", () => void saveAIFirstReview());
  document.querySelector("#ai-first-export-pilot").addEventListener("click", () => void downloadAIFirstPilotXlsx());
  document.querySelector("#ai-first-retry-export-preview").addEventListener("click", () => void retryAIFirstPilotExportPreview());
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
