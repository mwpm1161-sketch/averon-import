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
  ocrMode: "accurate",
  ocrHealth: null,
};

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
  const stepMap = {upload:"upload", pages:"pages", processing:"recognition", review:"review"};
  const step = stepMap[name] || name;
  $$(".step").forEach((button) => button.classList.toggle("active", button.dataset.step === step));
  const titles = {
    upload: ["Импорт спецификации", "Перенос таблиц из PDF в редактируемый Excel"],
    pages: ["Выбор страниц", "Укажите, какие таблицы необходимо распознать"],
    processing: ["Распознавание документа", "Определение строк, столбцов и текста"],
    review: ["Проверка данных", "Сравните результат с PDF и подготовьте экспорт"],
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
    state.config = await api("/api/config");
    const health = await api("/api/health");
    const ocr = health.ocr;
    state.ocrHealth = ocr;
    $("#ocr-dot").classList.add(ocr.available && ocr.russian ? "ok" : "bad");
    $("#ocr-title").textContent = ocr.available && ocr.russian ? "OCR готов" : "OCR требует настройки";
    $("#ocr-text").textContent = ocr.available ? `${ocr.version}${ocr.russian ? " · русский язык" : " · нет rus"}` : "Tesseract не найден";
    initializeOcrMode(ocr);
    populateFilters();
    initializeExportColumns();
    await resumeLastDocument();
  } catch (error) {
    toast(error.message, "error");
  }
}

function initializeOcrMode(ocr) {
  const accurate = document.querySelector('input[name="ocr-mode"][value="accurate"]');
  const standard = document.querySelector('input[name="ocr-mode"][value="standard"]');
  const status = $("#accurate-mode-status");
  const card = $("#accurate-mode-card");
  const saved = localStorage.getItem("averonOcrMode");
  const available = Boolean(ocr?.accurate_models);
  accurate.disabled = !available;
  card.classList.toggle("disabled", !available);
  if (available) {
    status.textContent = "Точная модель установлена и готова к работе.";
    status.className = "mode-status ok";
    state.ocrMode = saved === "standard" ? "standard" : "accurate";
  } else {
    status.textContent = "Точная модель не установлена. Запустите install_accurate_ocr_models.bat и перезапустите программу.";
    status.className = "mode-status warning";
    state.ocrMode = "standard";
  }
  (state.ocrMode === "accurate" ? accurate : standard).checked = true;
  document.querySelectorAll('input[name="ocr-mode"]').forEach((input) => {
    input.addEventListener("change", () => {
      state.ocrMode = input.value;
      localStorage.setItem("averonOcrMode", state.ocrMode);
    });
  });
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

async function resumeLastDocument() {
  const documentId = localStorage.getItem("averonCurrentDocument");
  if (!documentId) return;
  try {
    const documentData = await api(`/api/documents/${documentId}`);
    state.document = documentData;
    $("#document-name").textContent = documentData.filename;
    $("#document-meta").textContent = `${documentData.page_count} стр. · ${bytes(documentData.size)}`;
    $("#new-document-button").hidden = false;
    if (documentData.has_result) {
      const result = await api(`/api/documents/${documentId}/results`);
      loadResult(result);
      toast("Последний документ восстановлен", "success");
    } else {
      setView("pages");
      renderThumbnails();
    }
  } catch (_) {
    localStorage.removeItem("averonCurrentDocument");
  }
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
  $("#recognize-button").disabled = state.selectedPages.size === 0;
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
  $("#processing-title").textContent = state.ocrMode === "accurate" ? "Точное инженерное распознавание" : "Распознаём спецификацию";
  $("#processing-message").textContent = state.ocrMode === "accurate" ? "Читаем столбцы раздельно и проверяем сомнительные ячейки…" : "Подготовка страниц…";
  try {
    const job = await api(`/api/documents/${state.document.document_id}/recognize`, {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        pages,
        crop:state.crop,
        dpi:state.ocrMode === "accurate" ? 350 : 300,
        ocr_mode:state.ocrMode,
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

function loadResult(result) {
  state.result = result;
  state.rows = result.rows.map((row) => ({
    ...row,
    selected: row.selected ?? ["item","component"].includes(row.row_type),
  }));
  state.dirty = false;
  buildResultHeader();
  renderRows();
  updateSummary();
  setView("review");
  const first = state.rows.find((row) => row.selected) || state.rows[0];
  if (first) selectRow(first.id);
  if (result.errors?.length) {
    const pages = result.errors.map((error) => error.page).join(", ");
    const details = [...new Set(result.errors.map((error) => error.error).filter(Boolean))]
      .slice(0, 2)
      .join("; ");
    toast(`Не удалось обработать страницы: ${pages}${details ? `. Причина: ${details}` : ""}`, "error");
  } else toast("Распознавание завершено", "success");
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
  }).join("")}</tr>`;
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
  return state.rows.filter((row) => {
    if (type && row.row_type !== type) return false;
    if (status && row.status !== status) return false;
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
      if (row.status !== "verified") row.status = "edited";
      row.edited = true;
      autoHeight(input); markDirty(); updateSummary();
    });
    input.addEventListener("focus", () => selectRow(input.dataset.id));
  });
  body.querySelectorAll(".cell-select").forEach((select) => select.addEventListener("change", () => {
    const row = rowById(select.dataset.id); row[select.dataset.key] = select.value; row.edited = true; markDirty(); updateSummary(); renderRows();
  }));
}

function rowHtml(row) {
  const active = row.id === state.activeRowId ? "active" : "";
  const review = ["review","unrecognized"].includes(row.status) ? "review" : "";
  return `<tr data-id="${row.id}" class="${active} ${review}">
    <td class="selector"><input class="row-select" data-id="${row.id}" type="checkbox" ${row.selected ? "checked" : ""}></td>
    ${displayColumns.map((key) => cellHtml(row,key)).join("")}
  </tr>`;
}

function cellHtml(row, key) {
  if (key === "row_type") return `<td><select class="cell-select" data-id="${row.id}" data-key="row_type">${options(state.config.row_types,row.row_type)}</select></td>`;
  if (key === "status") return `<td><select class="cell-select" data-id="${row.id}" data-key="status">${options(state.config.statuses,row.status)}</select></td>`;
  if (key === "confidence") {
    const value = Number(row.confidence || 0);
    return `<td><div class="confidence"><span><i style="width:${Math.max(0,Math.min(100,value))}%"></i></span>${value.toFixed(0)}%</div></td>`;
  }
  if (key === "page") return `<td><span class="status-pill">${escapeHtml(String(row.page ?? ""))}</span></td>`;
  return `<td><textarea rows="1" class="cell-input" data-id="${row.id}" data-key="${key}">${escapeHtml(String(row[key] ?? ""))}</textarea></td>`;
}

function options(map, selected) {
  return Object.entries(map).map(([value,label]) => `<option value="${value}" ${value===selected?"selected":""}>${escapeHtml(label)}</option>`).join("");
}

function escapeHtml(value) {
  return value.replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
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
  if (state.previewPage !== row.page || !$("#pdf-preview").src) {
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
  requestAnimationFrame(() => highlight.scrollIntoView({block:"center",inline:"center",behavior:"smooth"}));
}

function updateSummary() {
  const ready = state.rows.filter((r) => ["recognized","verified","edited"].includes(r.status)).length;
  const review = state.rows.filter((r) => ["review","unrecognized"].includes(r.status)).length;
  const selected = state.rows.filter((r) => r.selected).length;
  $("#summary-total").textContent = state.rows.length;
  $("#summary-ready").textContent = ready;
  $("#summary-review").textContent = review;
  $("#summary-selected").textContent = selected;
}

function markDirty() { state.dirty = true; $("#save-button").textContent = "Сохранить правки •"; }

async function saveRows(showToast = true) {
  if (!state.document || !state.rows.length) return;
  await api(`/api/documents/${state.document.document_id}/results`, {
    method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({rows:state.rows}),
  });
  state.dirty = false; $("#save-button").textContent = "Сохранить правки";
  if (showToast) toast("Правки сохранены", "success");
}

function copyRows(rows, columns, includeHeader = true) {
  const selected = rows.filter((row) => row.selected && !["section","system","note","skip"].includes(row.row_type));
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
  await saveRows(false);
  const payload = {
    columns, rows:state.rows,
    include_headers:$("#export-headers").checked,
    only_exportable:$("#export-items-only").checked,
    filename:$("#export-filename").value,
    sheet_name:$("#export-sheet").value,
  };
  try {
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

function resetApp() {
  if (state.dirty && !confirm("Несохранённые правки будут потеряны. Продолжить?")) return;
  Object.assign(state,{document:null,selectedPages:new Set(),previewPage:null,crop:null,rows:[],result:null,activeRowId:null,zoom:1,dirty:false});
  localStorage.removeItem("averonCurrentDocument");
  $("#thumbnail-grid").innerHTML=""; $("#new-document-button").hidden=true; setView("upload");
}

function setupEvents() {
  $("#pdf-file").addEventListener("change", (event) => uploadFile(event.target.files[0]));
  const drop=$("#drop-zone");
  ["dragenter","dragover"].forEach((name)=>drop.addEventListener(name,(e)=>{e.preventDefault();drop.classList.add("drag");}));
  ["dragleave","drop"].forEach((name)=>drop.addEventListener(name,(e)=>{e.preventDefault();drop.classList.remove("drag");}));
  drop.addEventListener("drop",(e)=>uploadFile(e.dataTransfer.files[0]));
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
  $("#table-search").addEventListener("input",renderRows); $("#type-filter").addEventListener("change",renderRows); $("#status-filter").addEventListener("change",renderRows);
  $("#save-button").addEventListener("click",()=>saveRows().catch((e)=>toast(e.message,"error")));
  $("#copy-selected").addEventListener("click",()=>copyRows(state.rows,state.config.default_export_columns));
  $("#open-export").addEventListener("click",()=>{renderExportColumns();$("#export-modal").showModal();});
  $("#copy-export").addEventListener("click",()=>copyRows(state.rows,selectedExportColumns(),$("#export-headers").checked));
  $("#download-excel").addEventListener("click",downloadExcel);
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
