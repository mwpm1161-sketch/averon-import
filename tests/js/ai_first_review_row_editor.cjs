const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.resolve(__dirname, "../../averon_import/static/ai_first_ui.js"), "utf8");
function sourceBetween(start, end) {
  const first = source.indexOf(start), last = source.indexOf(end, first + start.length);
  assert(first >= 0 && last > first, `source block not found: ${start}`);
  return source.slice(first, last);
}
function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}
class Editor {
  constructor(recordId, columnIndex, value) {
    this.dataset = {aiFirstInspectorCell:recordId,columnIndex:String(columnIndex)};
    this.value = value;
    this.handlers = {};
  }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  fire(name) { this.handlers[name]?.({target:this}); }
}

const columns = Array.from({length:9},(_,index)=>({index,label:`Исходная колонка ${index + 1}`}));
const row = {id:"row-1",raw_cells:columns.map(column=>({column_index:column.index,raw_value:`OCR ${column.index + 1}`}))};
const entry = {row,columns,manual:false};
const tableInputs = columns.map(column=>({
  dataset:{aiFirstCell:"row-1",columnIndex:String(column.index)},
  value:`OCR ${column.index + 1}`,
}));
const tableByColumn = new Map(tableInputs.map(input=>[input.dataset.columnIndex,input]));
let editors = [];
const inspectorRoot = {
  scrollTop:0,
  querySelectorAll(selector) { return selector === "[data-ai-first-inspector-cell]" ? editors : []; },
};
const target = {
  html:"",
  set innerHTML(html) {
    this.html = html;
    const pattern = /<textarea\b([^>]*)>([\s\S]*?)<\/textarea>/g;
    editors = [...html.matchAll(pattern)].map(match=>{
      const id = /data-ai-first-inspector-cell="([^"]+)"/.exec(match[1])?.[1];
      const index = /data-column-index="([^"]+)"/.exec(match[1])?.[1];
      return new Editor(id,index,match[2].replaceAll("&quot;",'"').replaceAll("&lt;","<").replaceAll("&gt;",">"));
    });
  },
};
const state = {aiFirst:{pendingRowValues:{}}};
let captured = 0, updated = [];
const context = {
  state,
  CSS:{escape:value=>String(value)},
  escapeHtml,
  aiFirstRowInputSnapshot:()=>columns.map(column=>({key:`cell:${column.index}`,label:column.label,value:tableByColumn.get(String(column.index)).value,baseline:`OCR ${column.index + 1}`})),
  document:{
    querySelector(selector) {
      if (selector === "#ai-first-inspector-values") return target;
      if (selector === "#ai-first-row-inspector") return inspectorRoot;
      const match = /data-column-index="(\d+)"/.exec(selector);
      if (match) return tableByColumn.get(match[1]) || null;
      return null;
    },
  },
  captureAIFirstPendingEdits() {
    captured++;
    state.aiFirst.pendingRowValues["row-1"] = Object.fromEntries(tableInputs.map(input=>[`cell:${input.dataset.columnIndex}`,input.value]));
  },
  updateAIFirstRenderedRowState:id=>updated.push(id),
};
vm.runInNewContext(`${sourceBetween("function renderAIFirstInspectorValues", "function renderAIFirstRowInspector")}; globalThis.renderValues=renderAIFirstInspectorValues; globalThis.bindEditors=bindAIFirstInspectorValueEditors;`,context);

context.renderValues(entry);
assert.equal(editors.length,9,"the selected-row editor renders editable fields for all nine source columns");
for (let index=0; index<9; index++) {
  assert.equal(editors[index].dataset.columnIndex,String(index),"source-column order is preserved in the editor");
  assert(target.html.includes(`Исходная колонка ${index + 1}`),"every original column label is visible");
  assert(target.html.includes(`OCR ${index + 1}`),"the immutable OCR value remains visible beside the editor");
}
assert(target.html.includes("Исходное распознавание"),"the source value is explicitly identified as original OCR");
assert(target.html.includes('maxlength="2000"'),"editor fields retain the existing safe value limit");
context.bindEditors("row-1");
editors[2].value="Исправленное наименование [ТЕСТ]";
editors[2].fire("input");
assert.equal(tableInputs[2].value,"Исправленное наименование [ТЕСТ]","editor changes are mirrored to the existing review input model");
assert.equal(state.aiFirst.pendingRowValues["row-1"]["cell:2"],"Исправленное наименование [ТЕСТ]","the edit is captured as pending review state");
assert.equal(captured,1,"pending edits are captured on input");
assert.deepEqual(updated,["row-1"],"the visible review status updates for the edited row");
assert.equal(row.raw_cells[2].raw_value,"OCR 3","editing never mutates the original OCR observation");

let beforeUnloadHandler = null;
const guardContext = {
  state:{aiFirst:{data:{draft:{}}}},
  window:{__averonAIFirstUnsavedGuardInstalled:false,addEventListener:(name,handler)=>{ if (name === "beforeunload") beforeUnloadHandler=handler; }},
  aiFirstHasUnsavedReviewEdits:()=>true,
};
vm.runInNewContext(`${sourceBetween("function installAIFirstUnsavedNavigationGuard", "function renderAIFirstReview")}; globalThis.installGuard=installAIFirstUnsavedNavigationGuard;`,guardContext);
guardContext.installGuard();
let prevented=false;
const unloadEvent={preventDefault(){prevented=true;},returnValue:undefined};
beforeUnloadHandler(unloadEvent);
assert.equal(prevented,true,"navigating away with pending review edits triggers the browser's native warning");
assert.equal(unloadEvent.returnValue,"","the browser receives the standard beforeunload cancellation signal");
console.log("PASS: editable row inspector preserves all nine source columns, raw OCR and pending corrections");
