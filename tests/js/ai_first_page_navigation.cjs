const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const sourcePath = process.argv[2] || path.resolve(__dirname, "../../averon_import/static/ai_first_ui.js");
const source = fs.readFileSync(sourcePath, "utf8");
function sourceBetween(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + start.length);
  assert(first >= 0 && last > first, `source block not found: ${start}`);
  return source.slice(first, last);
}

class Element {
  constructor() {
    this.value = "";
    this.textContent = "";
    this.disabled = false;
    this.src = "";
    this.handlers = {};
    this.dataset = {};
    this.classes = new Set();
    this.submitButton = null;
    this.classList = {toggle:(name, active) => active ? this.classes.add(name) : this.classes.delete(name)};
  }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  set innerHTML(html) {
    this._html = html;
    this.newCells = [...html.matchAll(/<input class="input" data-ai-first-new-cell="(\d+)" data-ai-first-new-column-key="([^"]+)" maxlength="2000">/g)]
      .map(match => {
        const input = new Element();
        input.dataset.aiFirstNewCell = match[1];
        input.dataset.aiFirstNewColumnKey = match[2];
        return input;
      });
    this.submitButton = html.includes('id="ai-first-add-row-submit"') ? new Element() : null;
  }
  get innerHTML() { return this._html || ""; }
  querySelectorAll(selector) {
    if (selector === "[data-ai-first-new-cell]") return this.newCells || [];
    if (selector === "[data-ai-first-resolve]") return [];
    return [];
  }
  querySelector(selector) { return selector === "#ai-first-add-row-submit" ? this.submitButton : null; }
}

const elements = new Map([
  ["#ai-first-page-select", new Element()],
  ["#ai-first-pdf-page-label", new Element()],
  ["#ai-first-pdf-image", new Element()],
  ["#ai-first-prev-page", new Element()],
  ["#ai-first-next-page", new Element()],
  ["#ai-first-confirm-page", new Element()],
  ["#ai-first-confirm-selected", new Element()],
  ["#ai-first-page-issues", new Element()],
  ["#ai-first-add-row-form", new Element()],
]);
const rows = [1, 2].map(page => {
  const row = new Element();
  row.dataset.pageNumber = String(page);
  return row;
});
const tableCell = new Element();
tableCell.dataset.aiFirstCell = "row-1";
tableCell.dataset.columnIndex = "1";
tableCell.value = "Несохранённое исправление";

const columns = (headers) => headers.map((raw_header, index) => ({index,raw_header}));
const state = {
  document:{document_id:"synthetic-doc",page_count:2},
  aiFirst:{
    page:1,
    addRowOpen:true,
    addRowValues:{},
    data:{
      draft:{
        expected_page_numbers:[1,2],
        pages:[
          {page_number:1,status:"extracted",tables:[{columns:columns(["Поз.","Наименование","Количество"]),rows:[]}],issues:[]},
          {page_number:2,status:"extracted",tables:[{columns:columns(["Позиция","Наименование","Артикул","Количество","Единица"]),rows:[]}],issues:[]},
        ],
        issues:[
          {id:"issue-1",page_number:1,code:"duplicate_position_candidate",severity:"review"},
          {id:"issue-2",page_number:2,code:"position_gap_candidate",severity:"review"},
        ],
      },
      pages:[{page_number:1,state:"completed"},{page_number:2,state:"completed"}],
      review:{confirmed_complete_pages:[],resolved_issue_ids:[]},
    },
  },
};

const context = {
  state,
  document:null,
  escapeHtml:value => String(value ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;"),
  documentPageImageUrl:(id, page, scale) => `/synthetic/${id}/${page}/${scale}`,
  apiMode:"success",
  api:async (url, options = {}) => {
    if (options.method === "POST") {
      if (context.apiMode === "post-fail") throw new Error("synthetic server rejection");
      return {};
    }
    if (context.apiMode === "get-fail") throw new Error("synthetic refresh failure");
    return state.aiFirst.data;
  },
  aiFirstRowInputSnapshot:() => [],
  toast:() => {},
  confirm:() => true,
};
context.reviewRenderCount = 0;
context.renderAIFirstReview = () => { context.reviewRenderCount += 1; };
context.document = {
  querySelector:selector => elements.get(selector) || null,
  querySelectorAll:selector => selector === "[data-ai-first-row]" ? rows : selector === "#ai-first-table-scroll [data-ai-first-cell]" ? [tableCell] : [],
};

const snippet = [
  sourceBetween("function aiFirstPageRecord", "function renderAIFirstRunFeedback"),
  sourceBetween("function aiFirstColumnList", "function aiFirstDocumentEntries"),
].join("\n");
const addRowToggle = sourceBetween(
  'document.querySelector("#ai-first-add-row").addEventListener',
  'document.querySelector("#ai-first-page-select").addEventListener',
);
assert(addRowToggle.includes("renderAIFirstAddRowForm()"), "opening the add-row form updates only that form");
assert(!addRowToggle.includes("renderAIFirstReview()"), "opening the form must not rerender and discard unsaved table edits");
vm.runInNewContext(`${snippet}; globalThis.showPage=showAIFirstPage; globalThis.renderAddForm=renderAIFirstAddRowForm;`, context);

context.renderAddForm();
const pageOneFields = elements.get("#ai-first-add-row-form").newCells;
pageOneFields.find(input => input.dataset.aiFirstNewColumnKey === "наименование#1").value = "Синтетическая деталь";
pageOneFields.find(input => input.dataset.aiFirstNewColumnKey === "количество#1").value = "24";
pageOneFields.find(input => input.dataset.aiFirstNewColumnKey === "наименование#1").handlers.input();
pageOneFields.find(input => input.dataset.aiFirstNewColumnKey === "количество#1").handlers.input();

context.showPage(2);
assert.equal(state.aiFirst.page, 2, "page selection updates review state");
assert.equal(elements.get("#ai-first-page-select").value, "2", "page selector follows the selected page");
assert.equal(elements.get("#ai-first-pdf-page-label").textContent, "Страница 2");
assert.equal(elements.get("#ai-first-pdf-image").src, "/synthetic/synthetic-doc/2/200");
assert(elements.get("#ai-first-page-issues").innerHTML.includes("В нумерации есть пропуск"), "the selected page shows its own issue");
assert(!elements.get("#ai-first-page-issues").innerHTML.includes("Номер позиции встречается"), "issues from the previous page are removed");
assert.equal(tableCell.value, "Несохранённое исправление", "page changes preserve unsaved table edits");

const pageTwoForm = elements.get("#ai-first-add-row-form");
assert(pageTwoForm.innerHTML.includes("Новая позиция · страница 2"), "the add-row form follows the selected page");
assert(pageTwoForm.innerHTML.includes("Артикул") && pageTwoForm.innerHTML.includes("Единица"), "the add-row form exposes current-page columns");
assert.equal(pageTwoForm.newCells.find(input => input.dataset.aiFirstNewColumnKey === "наименование#1").value, "Синтетическая деталь", "matching unsaved add-row values survive page changes");
assert.equal(pageTwoForm.newCells.find(input => input.dataset.aiFirstNewColumnKey === "количество#1").dataset.aiFirstNewCell, "3", "the preserved quantity maps to the selected page's column index");
assert.equal(pageTwoForm.newCells.find(input => input.dataset.aiFirstNewColumnKey === "количество#1").value, "24");

context.showPage(1);
assert(elements.get("#ai-first-page-issues").innerHTML.includes("Номер позиции встречается"), "returning to a page restores its issue set");
assert.equal(elements.get("#ai-first-add-row-form").newCells.find(input => input.dataset.aiFirstNewColumnKey === "количество#1").value, "24", "add-row values remain available when returning");
console.log("PASS: AI-first page navigation refreshes page issues and add-row columns while preserving unsaved values");

const reviewSnippet = sourceBetween("async function postAIFirstReview", "async function cancelAIFirstRecognition");
vm.runInNewContext(`${reviewSnippet}; globalThis.aiFirstRowInputSnapshot=() => []; globalThis.submitAddRow=postAIFirstReview; globalThis.confirmSelected=confirmSelectedAIFirstRows;`, context);

async function verifySaveFailureBehavior() {
  const form = elements.get("#ai-first-add-row-form");
  const setField = (columnKey, value) => {
    const input = form.newCells.find(item => item.dataset.aiFirstNewColumnKey === columnKey);
    input.value = value;
    input.handlers.input();
  };

  setField("наименование#1", "Сохранённая позиция");
  setField("количество#1", "6");
  context.apiMode = "success";
  await form.submitButton.handlers.click();
  assert.deepEqual({...state.aiFirst.addRowValues}, {}, "successful add clears stored values");
  assert(form.newCells.every(input => input.value === ""), "successful add clears the visible form");

  context.renderAddForm();
  const retryForm = elements.get("#ai-first-add-row-form");
  const setRetryField = (columnKey, value) => {
    const input = retryForm.newCells.find(item => item.dataset.aiFirstNewColumnKey === columnKey);
    input.value = value;
    input.handlers.input();
  };
  setRetryField("наименование#1", "Позиция для повтора");
  setRetryField("количество#1", "13");
  context.apiMode = "post-fail";
  await retryForm.submitButton.handlers.click();
  assert.equal(retryForm.newCells.find(input => input.dataset.aiFirstNewColumnKey === "наименование#1").value, "Позиция для повтора", "rejected add keeps visible name for retry");
  assert.equal(state.aiFirst.addRowValues["наименование#1"], "Позиция для повтора", "rejected add keeps the name in form state");
  assert.equal(state.aiFirst.addRowValues["количество#1"], "13", "rejected add keeps the quantity in form state");

  state.aiFirst.selectedRows = new Set(["row-1", "row-2"]);
  context.apiMode = "post-fail";
  await context.confirmSelected();
  assert.deepEqual([...state.aiFirst.selectedRows], ["row-1", "row-2"], "failed bulk confirmation keeps selected rows");

  context.apiMode = "success";
  await context.confirmSelected();
  assert.equal(state.aiFirst.selectedRows.size, 0, "successful bulk confirmation clears selection");
  console.log("PASS: add-row values clear only after save; API rejection preserves retry values and bulk selection");
}

void verifySaveFailureBehavior();
