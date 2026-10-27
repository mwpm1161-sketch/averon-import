const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const rootPath = path.resolve(__dirname, "../..");
const uiPath = path.join(rootPath, "averon_import/static/ai_first_ui.js");
const templatePath = path.join(rootPath, "averon_import/templates/index.html");
const cssPath = path.join(rootPath, "averon_import/static/styles.css");
const source = fs.readFileSync(uiPath, "utf8");
const template = fs.readFileSync(templatePath, "utf8");
const css = fs.readFileSync(cssPath, "utf8");

function sourceBetween(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + start.length);
  assert(first >= 0 && last > first, `source block not found: ${start}`);
  return source.slice(first, last);
}

const tableInput = {
  dataset:{aiFirstCell:"row-1",columnIndex:"8"},
  value:"current value",
  selectionStart:4,
  selectionEnd:9,
  focused:null,
  getAttribute:()=>"Проверка",
  focus(options) { this.focused = options; },
  setSelectionRange(start,end) { this.selection = [start,end]; },
};
const tableRoot = {
  scrollTop:183,
  scrollLeft:526,
  querySelector(selector) {
    if (selector.includes("data-ai-first-cell")) return tableInput;
    return null;
  },
  querySelectorAll(selector) { return selector.includes("data-ai-first-cell") ? [tableInput] : []; },
};
let activeElement = tableInput;
const context = {
  CSS:{escape:value=>String(value)},
  document:{
    activeElement,
    querySelector:selector=>selector === "#ai-first-table-scroll" ? tableRoot : null,
    getElementById:()=>null,
  },
};
const viewCode = sourceBetween("function captureAIFirstViewState", "function aiFirstEntrySearchText");
vm.runInNewContext(`${viewCode}; globalThis.captureView=captureAIFirstViewState; globalThis.restoreView=restoreAIFirstViewState;`, context);
const captured = context.captureView();
assert.equal(captured.scrollLeft,526,"horizontal position is captured with vertical position");
assert.equal(captured.scrollTop,183,"vertical position is captured");
assert.equal(captured.focus.type,"cell","focused editable cell is identified across redraw");
tableRoot.scrollLeft = 0;
tableRoot.scrollTop = 0;
activeElement = {};
context.document.activeElement = activeElement;
context.restoreView(captured);
assert.equal(tableRoot.scrollLeft,526,"horizontal position is restored after redraw");
assert.equal(tableRoot.scrollTop,183,"vertical position is restored after redraw");
assert.deepEqual(tableInput.selection,[4,9],"caret selection is preserved");
assert.equal(tableInput.focused.preventScroll,true,"focus restoration does not jump the table scroll position");

const inspectorInput = {
  dataset:{aiFirstInspectorCell:"row-1",columnIndex:"8"},value:"corrected",selectionStart:3,selectionEnd:7,focused:null,
  focus(options) { this.focused=options; },setSelectionRange(start,end) { this.selection=[start,end]; },
};
const inspectorRoot = {scrollTop:97,querySelector:selector=>selector.includes("data-ai-first-inspector-cell") ? inspectorInput : null};
context.document.querySelector = selector => selector === "#ai-first-table-scroll" ? tableRoot : selector === "#ai-first-row-inspector" ? inspectorRoot : null;
context.document.activeElement = inspectorInput;
const editorView = context.captureView();
assert.equal(editorView.focus.type,"inspector-cell","the active row editor field is captured before redraw");
assert.equal(editorView.focus.column,"8","the editor column identity is preserved");
assert.equal(editorView.inspectorScrollTop,97,"the editor vertical position is captured");
inspectorRoot.scrollTop=0;
context.restoreView(editorView);
assert.equal(inspectorRoot.scrollTop,97,"the editor vertical position is restored");
assert.deepEqual(inspectorInput.selection,[3,7],"the editor caret is restored after redraw");
assert.equal(inspectorInput.focused.preventScroll,true,"editor focus restoration does not jump the table scroll position");

const pendingRoot = {
  querySelectorAll:selector=>selector === "[data-ai-first-cell]" ? [{dataset:{aiFirstCell:"row-1"},value:"edited"}] : [],
  querySelector:selector=>selector.includes("data-ai-first-parent-row") ? {value:"row-parent"} : null,
};
const roleSelect = {dataset:{aiFirstRowRole:"row-1"},value:"component"};
const pendingState = {aiFirst:{data:{review:{row_roles:{}}},pendingRowValues:{},pendingRowRoles:{},pendingIssueNotes:{}}};
context.state = pendingState;
context.document.querySelector = selector => {
  if (selector === "#ai-first-table-scroll") return pendingRoot;
  if (selector.includes("data-ai-first-parent-row")) return {value:"row-parent"};
  return null;
};
context.document.querySelectorAll = selector => {
  if (selector === "#ai-first-row-inspector [data-ai-first-row-role]") return [roleSelect];
  return [];
};
context.aiFirstRowInputSnapshot = () => [{key:"cell:8",label:"Проверка",value:"edited",baseline:"original"}];
context.aiFirstRowValues = snapshot => Object.fromEntries(snapshot.map(item=>[item.key,item.value]));
vm.runInNewContext(`${sourceBetween("function captureAIFirstPendingEdits", "function captureAIFirstViewState")}; globalThis.capturePending=captureAIFirstPendingEdits;`, context);
context.capturePending();
assert.equal(pendingState.aiFirst.pendingRowValues["row-1"]["cell:8"],"edited","unsaved cell values survive a page change");
assert.equal(pendingState.aiFirst.pendingRowRoles["row-1"].role,"component","unsaved role survives a page change");
assert.equal(pendingState.aiFirst.pendingRowRoles["row-1"].parent_record_id,"row-parent","component parent survives a page change");

assert(template.indexOf("id=\"ai-first-review-controls\"") < template.indexOf("id=\"ai-first-table-scroll\""),"filters and bulk controls are outside the horizontal scroller");
assert(template.indexOf("id=\"ai-first-row-inspector\"") < template.indexOf("id=\"ai-first-table-scroll\""),"the selected-row inspector remains visible above the table");
assert(source.includes("const headers = columns.map"),"every source column is still rendered from the source table definition");
assert(source.includes("const cells = columns.map"),"each source cell remains available in the table");
assert.match(css,/\.ai-first-table-scroll\s*\{[^}]*min-width:0[^}]*overflow:auto/s,"horizontal scroll container may shrink inside the split pane");
assert.match(css,/\.ai-first-table\s*\{[^}]*width:max-content[^}]*min-width:100%/s,"wide source columns remain intact and scrollable");
assert.match(css,/\.ai-first-row-inspector:not\(\[hidden\]\)\s*\{[^}]*position:absolute/s,"the selected-row editor overlays the table pane instead of shrinking it");
assert.match(css,/\.ai-first-inspector-values\s*\{[^}]*max-height:none[^}]*overflow:visible/s,"row fields share the editor scroll instead of a nested micro-scroll");
assert.match(css,/@media \(max-width:760px\)[\s\S]*\.ai-first-review-workspace/,"layout adapts to a narrow screen");

const inspector = sourceBetween("function renderAIFirstRowInspector", "function selectAIFirstRow");
for (const action of ["data-ai-first-save-row", "data-ai-first-save-role", "data-ai-first-confirm-row", "data-ai-first-exclude-row"]) {
  assert(inspector.includes(action),`selected-row inspector exposes ${action}`);
}
assert(inspector.includes("if (rolePending)"),"confirmation requires saving a changed role first");
assert(inspector.includes("if (aiFirstRowInputSnapshot(recordId).some"),"exclusion cannot discard unsaved cell edits");
assert(inspector.includes("Несохранённые правки останутся в строке таблицы"),"closing warns that pending edits remain visible but unsaved");
assert(source.includes("data-ai-first-inspector-cell"),"the selected-row editor exposes editable source-column fields");
assert(source.includes("installAIFirstUnsavedNavigationGuard"),"page refresh warns before discarding pending edits");

console.log("PASS: AI-first horizontal/vertical scroll and focus restoration, pending edits across page changes, source-column retention and compact inspector safeguards");
