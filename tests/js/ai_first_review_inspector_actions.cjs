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

class Control {
  constructor(dataset={}) { this.dataset=dataset; this.value=""; this.hidden=false; this.handlers={}; this.className=""; this.textContent=""; }
  addEventListener(name,handler) { this.handlers[name]=handler; }
  fire(name="click") { return this.handlers[name]?.({target:this}); }
}

const row = {id:"row-1",index:0,page_number:1,table_id:"table-1",model_kind:"item",raw_cells:[{id:"cell-1",column_index:0,raw_value:"Исходное значение"}]};
const page = {page_number:1,status:"extracted",extraction_fingerprint:"fp-1",tables:[{id:"table-1",rows:[row]}],issues:[]};
const entry = {row,page,table:page.tables[0],columns:[{index:0,label:"Наименование"}],manual:false};
const tableInput = new Control({aiFirstCell:"row-1",columnIndex:"0"});
tableInput.value = "Исправленное значение";
const roleInput = new Control({aiFirstRowRole:"row-1"});
roleInput.value = "standalone_item";
const parentInput = new Control({aiFirstParentRow:"row-1"});
const parentWrap = new Control();
const controls = new Map([
  ["[data-ai-first-row-role=\"row-1\"]",roleInput],
  ["[data-ai-first-parent-row=\"row-1\"]",parentInput],
  ["[data-ai-first-parent-wrap]",parentWrap],
  ["[data-ai-first-close-inspector]",new Control()],
  ["[data-ai-first-save-row]",new Control()],
  ["[data-ai-first-save-role]",new Control()],
  ["[data-ai-first-confirm-row]",new Control()],
  ["[data-ai-first-exclude-row]",new Control()],
]);
const inspector = {
  hidden:true,innerHTML:"",
  querySelector:selector=>controls.get(selector) || null,
  querySelectorAll:()=>[],
};
const data = {
  draft:{source_document_id:"doc-1",pages:[page],issues:[]},
  review:{decisions:{},row_roles:{},resolved_issue_ids:[],confirmed_complete_pages:[]},
};
const state = {document:{document_id:"doc-1"},aiFirst:{data,inspectorRowId:"row-1",renderedEntries:new Map([["row-1",entry]]),pendingRowValues:{"row-1":{"cell:0":"Исправленное значение"}},pendingRowRoles:{},pendingIssueNotes:{}}};
const posted = [];
let toasts = [];
let failNextPost = false;
const context = {
  state,
  CSS:{escape:value=>String(value)},
  escapeHtml:value=>String(value ?? ""),
  aiFirstDecision:id=>state.aiFirst.data.review.decisions[id] || null,
  aiFirstRowReviewState:()=>({status:"unsaved",label:"Есть несохранённые изменения",role:null,excluded:false,openIssues:[]}),
  aiFirstRowStatusLabel:review=>review.label,
  aiFirstIssueMessage:()=>"Сверьте строку.",
  aiFirstRowInputSnapshot:id=>[
    {key:"cell:0",label:"Наименование",value:tableInput.value,baseline:state.aiFirst.data.review.decisions[id]?.values?.["cell:0"] || "Исходное значение"},
  ],
  aiFirstRowValues:snapshot=>Object.fromEntries(snapshot.map(item=>[item.key,item.value])),
  aiFirstRowConfirmationPayload:snapshot=>({disposition:"corrected",confirmed:true,values:Object.fromEntries(snapshot.map(item=>[item.key,item.value]))}),
  updateAIFirstRenderedRowState:()=>{},
  renderAIFirstReview:()=>{},
  captureAIFirstPendingEdits:()=>{},
  toast:(message)=>toasts.push(message),
  confirm:()=>true,
  document:{
    querySelector:selector=>selector === "#ai-first-row-inspector" ? inspector : selector === "#ai-first-inspector-values" ? {innerHTML:""} : null,
    querySelectorAll:selector=>selector === "#ai-first-table-scroll [data-ai-first-cell]" ? [tableInput] : [],
  },
  api:async (url,options={})=>{
    if (options.method === "POST") {
      const body=JSON.parse(options.body);
      posted.push({url,body});
      if (failNextPost) { failNextPost=false; throw new Error("offline mock failure"); }
      if (url.endsWith("/row")) data.review.decisions[body.record_id]={active:true,disposition:body.disposition,human_confirmed:false,values:body.values};
      if (url.endsWith("/row-role")) data.review.row_roles[body.record_id]={active:true,role:body.role,parent_record_id:body.parent_record_id,source_document_id:"doc-1",page_number:1,page_extraction_fingerprint:"fp-1",table_id:"table-1"};
      return {};
    }
    return data;
  },
};
vm.runInNewContext(`${sourceBetween("function renderAIFirstInspectorValues", "function selectAIFirstRow")}; ${sourceBetween("async function postAIFirstReview", "async function confirmAIFirstPageCompleteness")}; globalThis.renderInspector=renderAIFirstRowInspector; globalThis.saveReview=postAIFirstReview; renderAIFirstInspectorValues=()=>{};`,context);

async function flush() { await new Promise(resolve=>setTimeout(resolve,0)); }
async function run() {
  context.renderInspector();
  roleInput.fire("change");
  assert.equal(state.aiFirst.pendingRowRoles["row-1"].role,"standalone_item","the simultaneous role edit is retained before saving cell values");
  await controls.get("[data-ai-first-save-row]").fire();
  await flush();
  assert.equal(posted.length,1,"the real row-save handler issues exactly one API write");
  assert.equal(posted[0].body.values["cell:0"],"Исправленное значение","the corrected value is sent by the row handler");
  assert.equal(state.aiFirst.pendingRowValues["row-1"],undefined,"successful value save clears the saved cell edits");
  assert.equal(state.aiFirst.pendingRowRoles["row-1"].role,"standalone_item","successful value save does not discard the still-unsaved role");

  await controls.get("[data-ai-first-save-role]").fire();
  await flush();
  assert.equal(posted.length,2,"after the cell save, the real role handler can save the pending role");
  assert.equal(posted[1].url.endsWith("/row-role"),true);
  assert.equal(posted[1].body.role,"standalone_item");
  assert.equal(state.aiFirst.pendingRowRoles["row-1"],undefined,"successful role save clears only the role edit");

  state.aiFirst.pendingRowValues["row-1"]={"cell:0":"a separate pending correction"};
  state.aiFirst.pendingRowRoles["row-1"]={role:"section",parent_record_id:null};
  await context.saveReview("row-role",{record_id:"row-1",role:"section",parent_record_id:null});
  assert.equal(state.aiFirst.pendingRowRoles["row-1"],undefined,"a successful role action clears the role payload");
  assert.equal(state.aiFirst.pendingRowValues["row-1"]["cell:0"],"a separate pending correction","a role action cannot clear pending cell values");

  tableInput.value="retry correction";
  state.aiFirst.pendingRowValues["row-1"]={"cell:0":"retry correction"};
  state.aiFirst.pendingRowRoles["row-1"]={role:"standalone_item",parent_record_id:null};
  failNextPost=true;
  const failedSave=await context.saveReview("row-role",{record_id:"row-1",role:"standalone_item",parent_record_id:null});
  assert.equal(failedSave.saved,false,"a rejected API call remains a failed save");
  assert.equal(tableInput.value,"retry correction","the editor's table-backed value survives API rejection");
  assert.equal(state.aiFirst.pendingRowValues["row-1"]["cell:0"],"retry correction","pending value remains available for retry");
  assert.equal(state.aiFirst.pendingRowRoles["row-1"].role,"standalone_item","pending role remains available for retry");

  const pill = new Control(), rowLabel = new Control(), heading = new Control();
  const cellClassSet = new Set();
  const cell = {closest:()=>({classList:{remove:(...names)=>names.forEach(name=>cellClassSet.delete(name)),add:name=>cellClassSet.add(name)}})};
  const rowElement = {dataset:{pageNumber:"1"},className:"",querySelector:selector=>selector === "[data-ai-first-row-status]" ? rowLabel : selector === ".ai-first-status-pill" ? pill : null,querySelectorAll:()=>[cell]};
  context.document.querySelector = selector=>selector === "#ai-first-table-scroll" ? {querySelector:()=>rowElement} : selector.includes("ai-first-inspector-heading") ? heading : null;
  context.document.querySelectorAll = ()=>[];
  context.aiFirstDocumentEntries = ()=>[entry];
  context.aiFirstRowReviewState = ()=>context.currentReviewState;
  context.currentReviewState={status:"unsaved",label:"Есть несохранённые изменения"};
  context.aiFirstRowStatusLabel=review=>review.label;
  context.aiFirstDecision=()=>null;
  context.renderAIFirstReviewFooter=()=>{};
  vm.runInNewContext(`${sourceBetween("function aiFirstStatusPillLabel", "function updateAIFirstRenderedRowState")}; ${sourceBetween("function updateAIFirstRenderedRowState", "function aiFirstProcessingMessage")}; globalThis.updateRendered=updateAIFirstRenderedRowState;`,context);
  for (const [status,label] of [["unsaved","Не сохранено"],["attention","Внимание"],["checked","Проверено"]]) {
    context.currentReviewState={status,label:`Состояние ${label}`};
    context.updateRendered("row-1");
    assert.equal(pill.textContent,label,`status pill refreshes to ${label}`);
    assert(pill.className.includes(`--${status}`),`status pill class refreshes to ${status}`);
    assert(rowElement.className.includes(`ai-first-row--${status}`),`row class refreshes to ${status}`);
  }
  const rolePendingReview={status:"attention",label:"Роль не подтверждена",decisionCurrent:true,roleVerified:false,pageReady:true,evidenceIssueExists:true,openIssues:[]};
  const labelSource=sourceBetween("function aiFirstRowStatusLabel", "function updateAIFirstRenderedRowState");
  vm.runInNewContext(`${labelSource}; globalThis.rowStatus=aiFirstRowStatusLabel; globalThis.pillStatus=aiFirstStatusPillLabel;`,context);
  assert.equal(context.rowStatus(rolePendingReview),"Значения проверены · тип строки не подтверждён","confirmed values remain explicit when the role is unconfirmed");
  assert.equal(context.pillStatus(rolePendingReview),"Тип не подтверждён","the warning pill names its reason instead of obscuring confirmed values");
  assert.equal(rolePendingReview.status,"attention","the display wording does not change the existing business status");
  console.log("PASS: real inspector save handlers preserve pending role/value edits across sequential API writes; row status pill tracks edits, role state and confirmation");
}
run().catch(error=>{console.error(error);process.exitCode=1;});
