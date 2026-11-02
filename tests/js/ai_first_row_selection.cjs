const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const rootPath = path.resolve(__dirname, "../..");
const source = fs.readFileSync(path.join(rootPath, "averon_import/static/ai_first_ui.js"), "utf8");
const template = fs.readFileSync(path.join(rootPath, "averon_import/templates/index.html"), "utf8");
const css = fs.readFileSync(path.join(rootPath, "averon_import/static/styles.css"), "utf8");

function sourceBetween(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + start.length);
  assert(first >= 0 && last > first, `source block not found: ${start}`);
  return source.slice(first, last);
}

class MockRow {
  constructor(id, searchText = id, pageNumber = 1) {
    this.dataset = {aiFirstRow:id,pageNumber:String(pageNumber)};
    this.searchText = searchText;
    this.hidden = false;
    this.checkbox = {dataset:{aiFirstSelectRow:id},checked:false};
  }
  querySelectorAll(selector) {
    return selector === "[data-ai-first-select-row]" ? [this.checkbox] : [];
  }
}

class MockGroup {
  constructor(rows) { this.rows = rows; this.hidden = false; }
  querySelectorAll(selector) { return selector === "tr[data-ai-first-row]" ? this.rows : []; }
}

const rows = [new MockRow("r1"),new MockRow("r2"),new MockRow("r3"),new MockRow("r4")];
// Two table groups share one page. The root order is the visual reading order.
const groups = [new MockGroup(rows.slice(0,2)),new MockGroup(rows.slice(2))];
const page1 = {page_number:1,extraction_fingerprint:"revision-1",tables:[{id:"table-1"},{id:"table-2"}]};
const page2 = {page_number:2,extraction_fingerprint:"page-2",tables:[]};
const search = {value:""};
const filter = {value:"all"};
const filterSummary = {textContent:""};
const selectionCount = {textContent:""};
const confirmButton = {disabled:true};
const roleButton = {disabled:true};
const selectVisibleButton = {disabled:false};
const clearPageButton = {disabled:true};
const controls = {
  querySelector(selector) {
    return new Map([
      ["#ai-first-search",search],
      ["#ai-first-review-filter",filter],
      ["[data-ai-first-filter-summary]",filterSummary],
      ["[data-ai-first-selected-count]",selectionCount],
      ["#ai-first-confirm-selected",confirmButton],
      ["[data-ai-first-bulk-role-submit]",roleButton],
      ["[data-ai-first-select-visible]",selectVisibleButton],
      ["[data-ai-first-clear-page-selection]",clearPageButton],
    ]).get(selector) || null;
  },
};
const tableRoot = {
  scrollTop:143,
  scrollLeft:318,
  querySelectorAll(selector) {
    if (selector === "tr[data-ai-first-row]") return rows;
    if (selector === "[data-ai-first-select-row]") return rows.map(row=>row.checkbox);
    if (selector === "[data-ai-first-table-group]") return groups;
    return [];
  },
};
const entryMap = new Map(rows.map(row=>[row.dataset.aiFirstRow,{row:{id:row.dataset.aiFirstRow,page_number:1},page:page1,searchText:row.searchText}]));
const pendingRowValues = {r2:{"cell:0":"unsaved value"}};
const pendingRowRoles = {r2:{role:"section"}};
const state = {
  document:{document_id:"synthetic-doc",page_count:2},
  aiFirst:{page:1,selectedRows:new Set(),pendingRowValues,pendingRowRoles,
    data:{draft:{pages:[page1,page2]},review:{row_roles:{},decisions:{}}},renderedEntries:entryMap},
};
const context = {
  state,
  document:{querySelector(selector) {
    if (selector === "#ai-first-table-scroll") return tableRoot;
    if (selector === "#ai-first-review-controls") return controls;
    return null;
  }},
  aiFirstPageRecord:pageNumber=>state.aiFirst.data.draft.pages.find(page=>page.page_number===pageNumber) || null,
  aiFirstRowReviewState:()=>({status:"neutral",roleVerified:false}),
  aiFirstDecision:()=>null,
  aiFirstEntrySearchText:entry=>entry.searchText,
};
const helpers = sourceBetween("function aiFirstVisibleSelectedIds", "function applyAIFirstReviewFilters");
const filters = sourceBetween("function applyAIFirstReviewFilters", "function renderAIFirstInspectorValues");
vm.runInNewContext(`${helpers}\n${filters}\nglobalThis.visibleSelected=aiFirstVisibleSelectedIds;\nglobalThis.handleSelection=handleAIFirstRowSelection;\nglobalThis.selectAllVisible=selectAllVisibleAIFirstRows;\nglobalThis.clearPage=clearAIFirstPageSelection;\nglobalThis.applyFilters=applyAIFirstReviewFilters;\nglobalThis.anchorMatches=aiFirstSelectionAnchorMatches;\nglobalThis.reconcileSelection=reconcileAIFirstSelectedRows;\nglobalThis.updateBulkControls=updateAIFirstBulkControls;`,context);

function click(id, {shiftKey=false,checked=true}={}) {
  const row = rows.find(item=>item.dataset.aiFirstRow===id);
  row.checkbox.checked = checked;
  context.handleSelection(row.checkbox,{shiftKey});
}
function selectedIds() { return [...state.aiFirst.selectedRows].sort(); }
function visibleSelectedIds() { return Array.from(context.visibleSelected()); }

// 1. A normal checkbox click toggles one row and sets an anchor.
click("r2");
assert.deepEqual(selectedIds(),["r2"]);
click("r2",{checked:false});
assert.deepEqual(selectedIds(),[]);

// 2, 3, 4. Shift ranges work in both directions, across table groups, and are idempotent.
click("r2");
click("r4",{shiftKey:true});
assert.deepEqual(selectedIds(),["r2","r3","r4"],"forward range follows DOM order across table groups");
click("r3",{shiftKey:true});
assert.deepEqual(selectedIds(),["r2","r3","r4"],"repeating a Shift range adds no duplicate IDs");
state.aiFirst.selectedRows.clear();
click("r4");
click("r2",{shiftKey:true});
assert.deepEqual(selectedIds(),["r2","r3","r4"],"backward range includes both endpoints");

// 5, 6. A filter change invalidates the anchor; hidden selections never enter bulk IDs.
state.aiFirst.selectedRows.clear();
click("r1");
search.value = "r3";
context.applyFilters();
assert.equal(state.aiFirst.selectionAnchor,null,"changing the search filter clears the previous anchor");
assert.deepEqual(visibleSelectedIds(),[],"a selected row hidden by the filter is excluded from bulk IDs");
assert.match(selectionCount.textContent,/ещё 1 строка скрыта фильтром и не участвует/);
click("r3",{shiftKey:true});
assert.deepEqual(visibleSelectedIds(),["r3"],"Shift-click after filtering cannot bridge a hidden row");
assert.deepEqual(selectedIds(),["r1","r3"],"the filter does not silently discard hidden selection");

// 7. Select all visible adds only filtered-visible rows; clear affects the current page only.
context.selectAllVisible();
assert.deepEqual(selectedIds(),["r1","r3"],"select-all adds only the visible row without selecting filtered rows");
assert.deepEqual(visibleSelectedIds(),["r3"]);
state.aiFirst.selectedRows.add("other-page-row");
context.clearPage();
assert.deepEqual(selectedIds(),["other-page-row"],"clear selection removes current-page rows, including hidden ones, but preserves other pages");
assert.equal(visibleSelectedIds().length,0);

// 8. Page identity and extraction fingerprint invalidate a stale anchor.
state.aiFirst.selectedRows.clear();
search.value = "";
filter.value = "all";
context.applyFilters();
click("r1");
const originalAnchor = state.aiFirst.selectionAnchor;
assert.equal(context.anchorMatches(originalAnchor,{documentId:"synthetic-doc",pageNumber:1,pageRecord:page1,extractionFingerprint:"revision-1",filterKey:'["","all"]'},rows.map(row=>row.dataset.aiFirstRow)),true);
assert.equal(context.anchorMatches(originalAnchor,{documentId:"synthetic-doc",pageNumber:1,pageRecord:{...page1},extractionFingerprint:"revision-1",filterKey:'["","all"]'},rows.map(row=>row.dataset.aiFirstRow)),true,"a review refresh with the same extraction fingerprint preserves the anchor");
page1.extraction_fingerprint = "revision-2";
assert.equal(context.anchorMatches(originalAnchor,{documentId:"synthetic-doc",pageNumber:1,pageRecord:page1,extractionFingerprint:"revision-2",filterKey:'["","all"]'},rows.map(row=>row.dataset.aiFirstRow)),false,"a changed extraction fingerprint invalidates the anchor");
click("r3",{shiftKey:true});
assert.deepEqual(selectedIds(),["r1","r3"],"a new extraction revision prevents reuse of the old range anchor");
state.aiFirst.page = 2;
assert.equal(context.anchorMatches(state.aiFirst.selectionAnchor,{documentId:"synthetic-doc",pageNumber:2,pageRecord:page2,extractionFingerprint:"page-2",filterKey:'["","all"]'},rows.map(row=>row.dataset.aiFirstRow)),false,"an anchor cannot cross pages");

// 9. Refresh removes stale/deleted IDs and rows selected under an older extraction,
// while preserving an actual selected record on another unchanged page.
const updatedPage1 = {...page1,extraction_fingerprint:"revision-3"};
state.aiFirst.data.draft.pages = [updatedPage1,page2];
state.aiFirst.page = 1;
state.aiFirst.selectedRows = new Set(["r1","page-2-real","deleted-row"]);
state.aiFirst.selectionRevisions = new Map([
  ["r1",{pageNumber:1,fingerprint:"revision-2",pageRecord:page1}],
  ["page-2-real",{pageNumber:2,fingerprint:"page-2",pageRecord:page2}],
  ["deleted-row",{pageNumber:1,fingerprint:"revision-2",pageRecord:page1}],
]);
context.reconcileSelection([
  {row:{id:"r1",page_number:1},page:updatedPage1},
  {row:{id:"page-2-real",page_number:2},page:page2},
]);
assert.deepEqual(selectedIds(),["page-2-real"],"a reused ID from an old extraction and a deleted ID are both cleared");
context.updateBulkControls();
assert.equal(selectionCount.textContent,"Выбрано 1; видно 0; 1 на других страницах и не участвуют","the count retains a real second-page selection without counting removed IDs");

// 9, 10. Selection is UI-only and leaves editor drafts, focus/caret, and scroll untouched.
const activeEditor = {value:"draft edit",selectionStart:3,selectionEnd:7,focused:true};
context.document.activeElement = activeEditor;
const initialView = {value:activeEditor.value,start:activeEditor.selectionStart,end:activeEditor.selectionEnd,
  scrollTop:tableRoot.scrollTop,scrollLeft:tableRoot.scrollLeft};
click("r2",{checked:false});
assert.deepEqual(state.aiFirst.pendingRowValues,pendingRowValues,"selection leaves unsaved cell edits untouched");
assert.deepEqual(state.aiFirst.pendingRowRoles,pendingRowRoles,"selection leaves unsaved role edits untouched");
assert.deepEqual({value:activeEditor.value,start:activeEditor.selectionStart,end:activeEditor.selectionEnd,
  scrollTop:tableRoot.scrollTop,scrollLeft:tableRoot.scrollLeft},initialView,"selection does not redraw, move focus/caret, or change either scroll position");

assert(source.includes('root.querySelectorAll("[data-ai-first-select-row]").forEach((input) => input.addEventListener("click", (event) => handleAIFirstRowSelection(input,event)))'),
  "row selection uses native checkbox clicks so browser Shift and keyboard modifiers remain intact");
assert(source.includes('event.target?.closest("button, input, select, textarea, summary, details, td.selector")'),
  "the row inspector ignores clicks anywhere in the selector cell, including the label hit target");
assert(source.includes("data-ai-first-select-visible") && source.includes("data-ai-first-clear-page-selection"),"bulk selection buttons are wired from the existing review toolbar");
assert.match(css,/--ai-first-selector-column-width:48px/,"the selector width is defined once for both sticky columns");
assert.match(css,/\.ai-first-row-select-hit\s*\{[^}]*width:44px;\s*height:44px/s,"the row checkbox has a 44 by 44 pixel hit target");
assert.match(css,/\.ai-first-row-select-hit > span\s*\{[^}]*width:21px;\s*height:21px/s,"the visible checkbox is approximately 20 to 22 pixels");
assert.match(css,/\.ai-first-row-select-hit input\s*\{[^}]*width:44px;\s*height:44px;[^}]*opacity:0/s,"the native checkbox occupies the full 44px hit target");
assert.match(template,/id="ai-first-review-controls"[^>]*><\/div>/,"selection controls remain in the existing review controls outside the scrolling table");

console.log("PASS: AI-first row selection toggles, Shift ranges, table-group order, filter/revision anchors, visible-only bulk, scope clearing, exact counts, pending edits, keyboard target and scroll/focus preservation");
