const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.resolve(__dirname,"../../averon_import/static/ai_first_ui.js"),"utf8");
const input = {dataset:{aiFirstCell:"row-1",columnIndex:"0"},value:"Edited synthetic name",getAttribute:()=>"Name"};
let inputs = [input];
const role = {dataset:{aiFirstRowRole:"row-1"},value:"standalone_item"};
const note = {dataset:{aiFirstIssueNote:"issue-1"},value:"Unfinished synthetic note"};
const added = {dataset:{aiFirstNewColumnKey:"name#1",aiFirstNewCell:"0"},value:"Unfinished manual row"};
const root = {querySelectorAll:()=>inputs,innerHTML:""};
const data = {review_token:"a".repeat(64),draft:{source_document_id:"synthetic",pages:[{page_number:1,tables:[{rows:[
  {id:"row-1",raw_cells:[{column_index:0,raw_value:"Original synthetic name"}]},
]}]}]},review:{decisions:{},row_roles:{}}};
const state = {document:{document_id:"document-1"},authGeneration:1,aiFirst:{data,page:1,
  pendingRowValues:{"row-2":{"cell:0":"Other page edit"}},pendingRowRoles:{},pendingIssueNotes:{},addRowValues:{}}};
let fail = false, held = null, sent = [], toasts = [];
const context = {state,console,setTimeout,Map,Set,CSS:{escape:v=>v},
  document:{addEventListener:()=>{},querySelector:selector=>selector==="#ai-first-table-scroll" ? root : null,
    querySelectorAll:selector=>selector==="#ai-first-table-scroll [data-ai-first-cell]" ? inputs
      : selector.includes("[data-ai-first-row-role]") ? [role]
      : selector.includes("[data-ai-first-issue-note]") ? [note]
      : selector.includes("[data-ai-first-new-cell]") ? [added] : []},
  toast:(message,kind)=>toasts.push({message,kind}),
  api:async(url,options={})=>{
    if (fail) throw new Error("Synthetic network failure");
    if (held) return new Promise(resolve=>{held.resolve=resolve;});
    const body=JSON.parse(options.body || "{}"); sent.push({url,body});
    return {saved:true,review_token:"b".repeat(64),editor_draft:{revision:1,stale:false,edits:body.edits,history:[]}};
  }};
vm.runInNewContext(source,context);
context.renderAIFirstReview=()=>{};
context.clearAIFirstRenderedReview=()=>{inputs=[];};
async function run() {
  const before=JSON.stringify(data.review);
  assert.equal(context.aiFirstHasUnsavedReviewEdits(),true);
  const saved=await context.saveAIFirstReview();
  assert.equal(saved.saved,true);
  assert.equal(sent.length,1);
  const payload=sent[0].body;
  assert.equal(payload.edits.row_values["row-1"]["cell:0"],input.value);
  assert.equal(payload.edits.row_values["row-2"]["cell:0"],"Other page edit");
  assert.equal(payload.edits.row_roles["row-1"].role,"standalone_item");
  assert.equal(payload.edits.issue_notes["issue-1"],note.value);
  assert.equal(payload.edits.add_row_values["name#1"],added.value);
  assert.equal(context.aiFirstHasUnsavedReviewEdits(),false,"server-backed drafts are saved, while confirmations stay separate");
  assert.equal(JSON.stringify(data.review),before,"save never invents review confirmations");
  const savedData=state.aiFirst.data;
  context.restoreAIFirstEditorDraft(savedData);
  assert.equal(state.aiFirst.pendingRowValues["row-1"]["cell:0"],input.value);
  assert.equal(state.aiFirst.addRowOpen,true);
  fail=true; input.value="Newer local value";
  assert.equal((await context.saveAIFirstReview()).saved,false);
  assert.equal(input.value,"Newer local value");
  assert.equal(context.aiFirstHasUnsavedReviewEdits(),true);
  fail=false; held={};
  const saving=context.saveAIFirstReview();
  input.value="Typed while save was pending";
  held.resolve({saved:true,review_token:"c".repeat(64),editor_draft:{revision:2,stale:false,edits:{...payload.edits,row_values:{...payload.edits.row_values,"row-1":{"cell:0":"Newer local value"}}}}});
  await saving;
  assert.equal(input.value,"Typed while save was pending");
  assert.equal(context.aiFirstHasUnsavedReviewEdits(),true,"typing during a save remains unsaved, never silently acknowledged");
  held=null;
  assert.equal((await context.saveAIFirstReview()).saved,true);
  assert.equal(context.aiFirstHasUnsavedReviewEdits(),false);
  // Old-document mutation responses must not refresh, overwrite or clear a new document.
  held={};
  const mutation=context.postAIFirstReview("row",{record_id:"row-1",disposition:"corrected",confirmed:false,values:{"cell:0":input.value}});
  state.document={document_id:"document-2"}; context.resetAIFirstDocumentState("document-2");
  held.resolve({}); await mutation;
  assert.equal(state.aiFirst.data,null);
  assert.deepEqual(Object.keys(state.aiFirst.pendingRowValues),[]);
  assert.equal(state.aiFirst.jobId,null);
  assert.equal(state.aiFirst.exportPages.size,0);
  // The same stale-job barrier applies to failed/expired/cancelled polling results.
  for (const status of ["failed","expired","completed"]) {
    state.document={document_id:"document-1"}; context.resetAIFirstDocumentState("document-1");
    let release;context.getJobForPolling=()=>new Promise(resolve=>{release=resolve;});
    const polling=context.pollAIFirstJob("old-job");
    state.document={document_id:"document-2"};context.resetAIFirstDocumentState("document-2");
    release({status,error:"checkpoint_identity_mismatch",result:{status:"cancelled"}});
    await polling;
    assert.equal(state.aiFirst.data,null);
    assert.equal(state.aiFirst.checkpointMismatch,undefined);
  }
  context.aiFirstVisibleSelectedIds=()=>["row-1"];
  context.aiFirstRowInputSnapshot=()=>[];
  context.confirm=()=>true;
  state.aiFirst.selectedRows=new Set(["row-1"]);
  const realPost=context.postAIFirstReview;
  context.postAIFirstReview=async()=>({saved:true,refreshed:false,stale:true});
  await context.confirmSelectedAIFirstRows();
  assert.equal(state.aiFirst.selectedRows.has("row-1"),true,"a late bulk response cannot clear a new document's selection");
  context.postAIFirstReview=realPost;
  const button={disabled:false},label={textContent:""};
  context.document.querySelector=selector=>selector==="#ai-first-export-pilot" ? button : selector==="#ai-first-export-note" ? label : null;
  state.aiFirst.exportPages=new Set([1]);state.aiFirst.reviewGeneration=0;
  held={};const preview=context.loadAIFirstPilotExportPreview([1],"1|0");
  const oldRequestId=state.aiFirst.exportPreviewRequestId;
  state.document={document_id:"document-3"};context.resetAIFirstDocumentState("document-3");
  state.aiFirst.exportPages=new Set([1]);state.aiFirst.reviewGeneration=0;
  state.aiFirst.exportPreviewRequestId=oldRequestId; // Deliberate selection/id collision.
  held.resolve({verified_position_count:999,total_row_count:999});await preview;
  assert.equal(state.aiFirst.exportPreview,undefined,"document identity, not selection alone, fences preview responses");
  held=null;
  state.selectedPages=new Set([1]);state.crop={x:0,y:0,width:0.2,height:0.2};
  state.aiFirst.cropDocumentId="document-2";state.aiFirst.cropPage=1;
  const callsBefore=sent.length;
  await context.startAIFirstRecognition();
  assert.equal(sent.length,callsBefore,"a stale crop never falls back silently to sending a whole page");
  assert(toasts.at(-1).message.includes("область относится"));
  assert(!source.includes("не предназначен для коммерческого использования"));
  const app = fs.readFileSync(path.resolve(__dirname,"../../averon_import/static/app.js"),"utf8");
  const resetCode = app.slice(app.indexOf("async function resetApp()"), app.indexOf("function setupEvents()"));
  let navigationAllowed=false, resets=0;
  const resetContext={state:{document:{document_id:"doc-before-reset"},aiFirst:{data:{}},dirty:false},
    prepareAIFirstDocumentNavigation:async()=>navigationAllowed,
    cancelDocumentNavigation:()=>{resets++;},clearExportError:()=>{},updatePageInspectionOpenButton:()=>{},rebuildResultIndexes:()=>{},
    localStorage:{removeItem:()=>{}},$ :()=>({innerHTML:"",hidden:false}),setView:()=>{},clearTimeout:()=>{},confirm:()=>true};
  vm.runInNewContext(resetCode,resetContext);
  await resetContext.resetApp();
  assert.equal(resets,0,"new-document navigation stops if the draft cannot be saved");
  assert.equal(resetContext.state.document.document_id,"doc-before-reset");
  navigationAllowed=true;await resetContext.resetApp();
  assert.equal(resets,1);assert.equal(resetContext.state.document,null);
  console.log("PASS: full draft save/restore, unconfirmed buffers, network failure, in-flight typing and cross-document mutation/job isolation");
}
run().catch(error=>{console.error(error);process.exitCode=1;});
