const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.resolve(__dirname, "../../averon_import/static/ai_first_ui.js"), "utf8");
function sourceBetween(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + start.length);
  assert(first >= 0 && last > first, `source block not found: ${start}`);
  return source.slice(first, last);
}

const row = {id:"row-1",table_id:"table-1",page_number:1,index:0,model_kind:"item",raw_cells:[
  {id:"cell-1",column_index:0,raw_value:"1",unreadable:false,struck_through:false,handwritten:false,alternatives:[]},
  {id:"cell-2",column_index:1,raw_value:"Original",unreadable:false,struck_through:false,handwritten:false,alternatives:[]},
]};
const page = {page_number:1,status:"extracted",extraction_fingerprint:"fingerprint-1",tables:[{id:"table-1",rows:[row]}],issues:[]};
const role = (value="standalone_item") => ({record_id:row.id,source_document_id:"synthetic-doc",page_number:1,page_extraction_fingerprint:"fingerprint-1",table_id:"table-1",role:value,active:true});
const decision = (values={}) => ({record_id:row.id,source_document_id:"synthetic-doc",page_number:1,disposition:"confirmed",active:true,values});
const data = {
  draft:{source_document_id:"synthetic-doc",pages:[page],issues:[]},
  pages:[{page_number:1,state:"completed"}],
  review:{decisions:{[row.id]:decision()},row_roles:{[row.id]:role()},resolved_issue_ids:[],confirmed_complete_pages:[1]},
};
const input = {dataset:{aiFirstCell:row.id,columnIndex:"1"},value:"Original",getAttribute:() => "Наименование"};
const roleInput = {value:"standalone_item"};
const parentInput = {value:"",hidden:true};
const context = {
  state:{document:{document_id:"synthetic-doc"},aiFirst:{data}},
  aiFirstReportPages:value=>new Map((value?.pages || []).map(item=>[item.page_number,item])),
  CSS:{escape:value=>String(value)},
  document:{
    querySelectorAll:selector=>selector === "#ai-first-table-scroll [data-ai-first-cell]" ? [input] : [],
    querySelector:selector=>selector.includes("data-ai-first-row-role") ? roleInput : selector.includes("data-ai-first-parent-row") ? parentInput : null,
  },
};
vm.runInNewContext(`${sourceBetween("function aiFirstDecision", "function aiFirstProcessingMessage")}; globalThis.evaluateRow=aiFirstRowReviewState; globalThis.confirmationPayload=aiFirstRowConfirmationPayload; globalThis.statusLabel=aiFirstRowStatusLabel;`, context);
const entry = {row,page,manual:false};
const evaluate = () => context.evaluateRow(entry);

let result = evaluate();
assert.equal(result.status,"checked");
assert.equal(result.exportReady,true);
assert(context.statusLabel(result).includes("допущена в тестовый XLSX"));

page.issues = [{id:"table-issue",page_number:1,record_ids:["table-1"],severity:"review",code:"continuation_unverified"}];
result = evaluate();
assert.equal(result.status,"attention","an open table issue blocks the green state");
assert.equal(result.exportReady,false);

page.issues = [{id:"page-issue",page_number:1,record_ids:[],severity:"blocking",code:"unreadable_page_fragment"}];
assert.equal(evaluate().status,"attention","an open page issue applies to its rows");

page.issues = [];
data.draft.issues = [{id:"document-issue",page_number:null,record_ids:[],severity:"review",code:"unreadable_page_fragment"}];
assert.equal(evaluate().status,"attention","a document-wide issue applies to every page");
data.draft.issues = [];

data.review.row_roles[row.id] = { ...role("unknown") };
roleInput.value = "unknown";
assert.equal(evaluate().status,"attention","an unverified role is never green");
data.review.row_roles[row.id] = role();
roleInput.value = "standalone_item";
data.review.decisions[row.id] = { ...decision(),active:false,invalidation_reason:"page_reprocessed" };
assert.equal(evaluate().status,"attention","a stale decision is never green");
data.review.decisions[row.id] = decision();

row.raw_cells[1].handwritten = true;
data.review.decisions[row.id] = decision();
assert.equal(evaluate().status,"attention","uncertain evidence without a resolving remark cannot appear checked");
data.review.decisions[row.id] = null;
assert.equal(evaluate().status,"attention","uncertain source values require an explicit decision");
page.issues = [{id:"handwriting-note",page_number:1,record_ids:["cell-2"],severity:"review",code:"row_has_uncertain_visual_evidence"}];
data.review.resolved_issue_ids = ["handwriting-note"];
data.review.decisions[row.id] = decision({"cell:1":"Human reading"});
input.value = "Human reading";
assert.equal(evaluate().status,"checked","an explicit decision may clear the row state after its remarks are resolved");
row.raw_cells[1].handwritten = false;
page.issues = [];
data.review.resolved_issue_ids = [];

data.review.confirmed_complete_pages = [];
result = evaluate();
assert.equal(result.status,"checked","value review is distinct from page completeness");
assert.equal(result.exportReady,false,"pilot export readiness still requires page completeness");
assert(context.statusLabel(result).includes("полнота страницы не подтверждена"));
data.review.confirmed_complete_pages = [1];

data.review.row_roles[row.id] = role("header");
roleInput.value = "header";
assert.equal(evaluate().status,"neutral","service rows use the neutral state");
data.review.row_roles[row.id] = role();
roleInput.value = "standalone_item";
assert(context.statusLabel({status:"checked",role:{role:"component"},pageComplete:true,exportReady:false}).includes("компонент комплекта"));
data.review.decisions[row.id] = {...decision({"cell:1":"Human reading"}),disposition:"excluded"};
assert.equal(evaluate().status,"neutral","excluded rows use the neutral state");
data.review.decisions[row.id] = decision({"cell:1":"Saved correction"});
input.value = "Saved correction";
input.value = "Unsaved correction";
assert.equal(evaluate().status,"unsaved","unsaved DOM edits are visibly distinct");

const payload = context.confirmationPayload([{key:"cell:1",value:"Edited",baseline:"Original"}]);
assert.equal(payload.disposition,"corrected","editing and confirming records a corrected decision");
assert.equal(payload.values["cell:1"],"Edited");
assert.equal(context.confirmationPayload([{key:"cell:1",value:"Original",baseline:"Original"}]).disposition,"confirmed");

let saveCalls = 0;
const saveWarnings = [];
context.api = async () => { saveCalls += 1; return {}; };
context.toast = (message,kind) => saveWarnings.push({message,kind});
vm.runInNewContext(`${sourceBetween("async function saveAIFirstReview", "async function downloadAIFirstPilotXlsx")}; globalThis.saveDraft=saveAIFirstReview;`, context);
async function verifySaveGuard() {
  input.value = "DOM-only correction";
  await context.saveDraft();
  assert.equal(saveCalls,0,"draft save is blocked while an edit exists only in the DOM");
  assert.equal(saveWarnings.at(-1).kind,"warning");
  assert(saveWarnings.at(-1).message.includes("не попадут в файл"));
  input.value = "Saved correction";
  await context.saveDraft();
  assert.equal(saveCalls,1,"draft save proceeds after unsaved edits are cleared");
}
verifySaveGuard().then(() => {
  console.log("PASS: AI-first review state, open issue scope, stale decisions, service/excluded rows, export readiness, corrected confirmation and explicit draft-save guard");
}).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
