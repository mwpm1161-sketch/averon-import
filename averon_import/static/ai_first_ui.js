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

function aiFirstRowIssues(row, page) {
  const issues = [...(page?.issues || []), ...(state.aiFirst.data?.draft?.issues || [])];
  const ids = new Set([row.id, ...(row.raw_cells || []).map((cell) => cell.id)]);
  return issues.filter((issue, index) => issue.code !== "page_extraction_failed" && issue.record_ids?.some((id) => ids.has(id))
    && issues.findIndex((candidate) => candidate.id === issue.id) === index);
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
    .filter((issue, index, all) => issue.page_number === pageNumber && issue.code !== "page_extraction_failed"
      && ["review", "blocking"].includes(issue.severity) && !(review?.resolved_issue_ids || []).includes(issue.id)
      && all.findIndex((item) => item.id === issue.id) === index);
  root.innerHTML = pageIssues.map((issue) => `<div class="ai-first-issue ${issue.severity === "blocking" ? "blocking" : ""}"><div>${escapeHtml(aiFirstIssueMessage(issue.code))}<div class="ai-first-resolve-row"><input class="input" data-ai-first-issue-note="${escapeHtml(issue.id)}" maxlength="2000" placeholder="Что проверили по PDF?"><button class="button secondary" type="button" data-ai-first-resolve="${escapeHtml(issue.id)}">Отметить проверенным</button></div></div></div>`).join("");
  root.querySelectorAll("[data-ai-first-resolve]").forEach((button) => button.addEventListener("click", () => {
    const id = button.dataset.aiFirstResolve;
    const detail = root.querySelector(`[data-ai-first-issue-note="${CSS.escape(id)}"]`)?.value || "";
    void postAIFirstReview("resolve-issue", {issue_id:id,detail});
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
      && ["confirmed", "corrected"].includes(decision.disposition);
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
  document.querySelectorAll("[data-ai-first-complete-page]").forEach((button) => button.addEventListener("click", () => void postAIFirstReview("complete-page", {page_number:Number(button.dataset.aiFirstCompletePage)})));
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
  document.querySelector("#ai-first-confirm-selected").disabled = state.aiFirst.selectedRows.size === 0;
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

function renderAIFirstTables(entries) {
  const root = document.querySelector("#ai-first-table-scroll");
  const columns = new Map();
  for (const entry of entries) for (const column of entry.columns) if (!columns.has(column.key)) columns.set(column.key, column);
  const allColumns = [...columns.values()];
  const body = entries.map(({row,page,columns:rowColumns,manual}) => {
    const decision = aiFirstDecision(row.id);
    const rowIssues = manual ? [] : aiFirstRowIssues(row, page);
    const flagged = rowIssues.some((issue) => ["review", "blocking"].includes(issue.severity) && !(state.aiFirst.data.review.resolved_issue_ids || []).includes(issue.id));
    const excluded = decision?.active && decision.disposition === "excluded";
    const cells = allColumns.map((column) => {
      const sourceColumn = rowColumns.find((item) => item.key === column.key);
      if (!sourceColumn) return "<td></td>";
      const key = `cell:${sourceColumn.index}`;
      const cell = manual ? null : (row.raw_cells || []).find((item) => item.column_index === sourceColumn.index);
      const source = manual ? row.values?.[key] || "" : cell?.raw_value ?? "";
      const retainedAfterRoleChange = decision?.invalidation_reason === "role_change";
      const edited = (decision?.active || retainedAfterRoleChange) && Object.prototype.hasOwnProperty.call(decision.values || {}, key);
      const shown = edited ? decision.values[key] : source;
      const sourceNote = edited && decision.values[key] !== source
        ? `<small class="ai-first-cell-source">${retainedAfterRoleChange ? "Сохранённая правка требует повторного подтверждения. Исходно:" : "Исходно:"} ${escapeHtml(source || "пусто")}</small>` : "";
      const uncertain = !manual && (cell?.unreadable || cell?.struck_through || cell?.handwritten || cell?.alternatives?.length);
      return `<td class="${flagged || uncertain ? "critical-review" : ""}"><input class="cell-input ai-first-cell-input" data-ai-first-cell="${escapeHtml(row.id)}" data-column-index="${sourceColumn.index}" aria-label="${escapeHtml(sourceColumn.label)}" maxlength="2000" value="${escapeHtml(shown)}">${sourceNote}${uncertain ? `<small class="critical-warning">Требует сверки${cell?.alternatives?.length ? ` · варианты: ${escapeHtml(cell.alternatives.join(" / "))}` : ""}</small>` : ""}</td>`;
    }).join("");
    const decisionLabel = excluded ? "Исключена" : decision?.active ? (decision.disposition === "corrected" ? "Исправлена" : Object.keys(decision.values || {}).length ? "Подтверждена с правками" : "Подтверждена") : decision?.invalidation_reason === "role_change" ? "Роль изменена · подтвердите значения повторно" : "Нужно проверить";
    const kindLabel = ({item:"Позиция",item_candidate:"Возможная позиция",header:"Заголовок",section:"Раздел",note:"Примечание"})[row.model_kind] || (manual ? "Ручная позиция" : "Строка документа");
    const rowLabel = manual ? "Ручная" : String((row.index ?? 0) + 1);
    const role = state.aiFirst.data.review.row_roles?.[row.id];
    const selectedRole = role?.active ? role.role : "unknown";
    const rowPage = Number(page?.page_number || row.page_number);
    const rowTableId = manual ? null : row.table_id;
    const parentCandidates = entries.filter((candidate) => {
      if (candidate.row.id === row.id || Number(candidate.page?.page_number || candidate.row.page_number) !== rowPage) return false;
      if (rowTableId && candidate.row.table_id && candidate.row.table_id !== rowTableId) return false;
      const candidateRole = state.aiFirst.data.review.row_roles?.[candidate.row.id];
      return candidateRole?.active && candidateRole.role === "standalone_item";
    });
    const parentOptions = `<option value="">Выберите самостоятельную позицию</option>${parentCandidates.map((candidate) => `<option value="${escapeHtml(candidate.row.id)}" ${role?.parent_record_id === candidate.row.id ? "selected" : ""}>Строка ${escapeHtml(candidate.manual ? "ручная" : String((candidate.row.index ?? 0) + 1))}</option>`).join("")}`;
    const roleOptions = [["unknown","Не определена"],["standalone_item","Самостоятельная позиция"],["component","Компонент комплекта"],["header","Заголовок"],["column_numbering","Нумерация колонок"],["section","Раздел"],["continuation","Продолжение строки"],["note","Примечание"]];
    const roleControls = `<div class="ai-first-role-controls"><label>Роль строки<select class="select" data-ai-first-row-role="${escapeHtml(row.id)}">${roleOptions.map(([value,label]) => `<option value="${value}" ${selectedRole === value ? "selected" : ""}>${label}</option>`).join("")}</select></label><select class="select" data-ai-first-parent-row="${escapeHtml(row.id)}" ${selectedRole === "component" ? "" : "hidden"}>${parentOptions}</select><button class="button text" type="button" data-ai-first-save-role="${escapeHtml(row.id)}">Сохранить роль</button></div>`;
    return `<tr data-ai-first-row="${escapeHtml(row.id)}" data-page-number="${rowPage}" class="${flagged ? "critical-review" : ""} ${excluded ? "is-excluded" : ""} ${rowPage === state.aiFirst.page ? "active" : ""}"><td class="selector"><input type="checkbox" data-ai-first-select-row="${escapeHtml(row.id)}" aria-label="Выбрать строку" ${state.aiFirst.selectedRows.has(row.id) ? "checked" : ""}></td><td>${rowLabel}<small class="ai-first-cell-source">${escapeHtml(kindLabel)} · ${decisionLabel}</small></td><td><button class="button text ai-first-source-page" type="button" data-ai-first-show-page="${rowPage}">Стр. ${rowPage}</button></td>${cells}<td>${roleControls}<div class="ai-first-row-actions"><button class="button secondary" type="button" data-ai-first-save-row="${escapeHtml(row.id)}">Сохранить правки</button><button class="button ghost" type="button" data-ai-first-confirm-row="${escapeHtml(row.id)}">Подтвердить значения</button><button class="button text" type="button" data-ai-first-exclude-row="${escapeHtml(row.id)}">${excluded ? "Восстановить" : "Исключить"}</button></div></td></tr>`;
  }).join("");
  const header = allColumns.map((column) => `<th>${escapeHtml(column.label)}</th>`).join("");
  root.innerHTML = `<table class="data-table ai-first-table"><thead><tr><th class="selector">Выбор</th><th>Строка</th><th>Страница</th>${header}<th>Действия</th></tr></thead><tbody>${body || `<tr><td colspan="${allColumns.length + 4}" class="empty-table">Для обработанных страниц позиции не найдены. Проверьте сводку страниц.</td></tr>`}</tbody></table>`;
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
  root.querySelectorAll("[data-ai-first-row-role]").forEach((select) => select.addEventListener("change", () => {
    const parent = root.querySelector(`[data-ai-first-parent-row="${CSS.escape(select.dataset.aiFirstRowRole)}"]`);
    if (parent) parent.hidden = select.value !== "component";
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
    void postAIFirstReview("row", {record_id:id,disposition:"confirmed",values:changed.length ? aiFirstRowValues(snapshot) : {}});
  }));
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
  root.querySelectorAll("[data-ai-first-row]").forEach((row) => row.addEventListener("click", (event) => {
    if (event.target.closest("button, input, select, textarea")) return;
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
}

async function postAIFirstReview(action, body) {
  const affected = new Set(body.record_id ? [body.record_id] : (body.record_ids || []));
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
    toast(error.message || "Не удалось сохранить решение.", "error");
    return {saved:false,refreshed:false};
  }
  try {
    state.aiFirst.data = await api(`/api/documents/${encodeURIComponent(state.document.document_id)}/ai-first`);
    renderAIFirstReview();
    for (const [id, values] of pending) {
      if (affected.has(id)) continue;
      for (const input of document.querySelectorAll("#ai-first-table-scroll [data-ai-first-cell]")) {
        if (input.dataset.aiFirstCell === id) input.value = values[`cell:${input.dataset.columnIndex}`] ?? input.value;
      }
    }
    toast("Изменение сохранено в черновике и добавлено в историю проверки.", "success");
    return {saved:true,refreshed:true};
  } catch (_) {
    toast("Изменение сохранено, но не удалось обновить данные. Перезагрузите документ перед продолжением.", "warning");
    return {saved:true,refreshed:false};
  }
}

async function confirmSelectedAIFirstRows() {
  const recordIds = [...state.aiFirst.selectedRows];
  if (!recordIds.length) return;
  const changedRows = recordIds.filter((id) => aiFirstRowInputSnapshot(id).some((item) => item.value !== item.baseline));
  if (changedRows.length) {
    toast(`В ${changedRows.length} выбранных строках есть несохранённые правки. Сохраните каждую строку отдельно, затем подтвердите их вместе.`, "error");
    return;
  }
  if (!confirm(`Подтвердить ${recordIds.length} выбранных строк? Будут подтверждены значения, показанные в таблице; несохранённых правок нет. Сначала сверьте строки с PDF.`)) return;
  const result = await postAIFirstReview("bulk-confirm", {record_ids:recordIds});
  if (!result?.saved) return;
  state.aiFirst.selectedRows.clear();
  if (result.refreshed) renderAIFirstReview();
  else {
    document.querySelectorAll("#ai-first-table-scroll [data-ai-first-select-row]").forEach((input) => { input.checked = false; });
    const confirmButton = document.querySelector("#ai-first-confirm-selected");
    if (confirmButton) confirmButton.disabled = true;
  }
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

function aiFirstHasUnsavedReviewEdits() {
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
  document.querySelector("#ai-first-confirm-selected").addEventListener("click", () => void confirmSelectedAIFirstRows());
  document.querySelector("#ai-first-export-pilot").addEventListener("click", () => void downloadAIFirstPilotXlsx());
  document.querySelector("#ai-first-confirm-page").addEventListener("click", async () => {
    await postAIFirstReview("complete-page", {page_number:state.aiFirst.page});
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
