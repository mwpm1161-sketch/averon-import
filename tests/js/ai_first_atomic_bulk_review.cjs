const assert=require("node:assert/strict");
const fs=require("node:fs");
const path=require("node:path");
const vm=require("node:vm");
const source=fs.readFileSync(path.resolve(__dirname,"../../averon_import/static/ai_first_ui.js"),"utf8");
const start=source.indexOf("function aiFirstBulkDependencyPreview"),end=source.indexOf("async function confirmSelectedAIFirstRows",start);
assert(start>=0&&end>start);

function harness(options={}) {
  const page={page_number:1,extraction_fingerprint:"fp-1"},table={id:"table-1"};
  const state={document:{document_id:"doc-1"},aiFirst:{page:1,selectedRows:new Set(["hidden","visible","other-page"]),
    data:{review_token:"a".repeat(64),draft:{revision_id:"rev-1"},review:{row_roles:{dependent:{active:true,role:"component"}}},editor_draft:{revision:4}},
    pendingRowValues:{visible:{"cell:0":"edited"},"other-page":{"cell:0":"keep"}},pendingRowRoles:{},
    renderedEntries:new Map([["visible",{row:{id:"visible",page_number:1},page,table}]])}};
  let text="edited",role=options.role||"standalone_item",parent=options.parent||"",confirmAnswer=options.confirm!==false;
  const calls={preview:[],commit:[],toasts:[],prompts:[],renders:0};
  const controls={querySelector:selector=>selector==="#ai-first-bulk-role"?{value:role}:selector==="#ai-first-bulk-parent"?{value:parent}:null};
  const context={state,crypto:{randomUUID:()=>"00000000-0000-4000-8000-000000000001"},
    document:{querySelector:selector=>selector==="#ai-first-review-controls"?controls:selector.includes("option:checked")?{textContent:role}:null},
    aiFirstRequestContext:()=>({documentId:"doc-1"}),isCurrentAIFirstContext:()=>true,
    aiFirstDocumentEntries:()=>[...state.aiFirst.renderedEntries.values(),{row:{id:"dependent",index:6,page_number:2},page:{page_number:2},table:{id:"table-2",index:1}}],
    aiFirstVisibleSelectedIds:()=>state.aiFirst.selectedRows.has("visible")?["visible"]:[],
    captureAIFirstPendingEdits:()=>{},aiFirstRowInputSnapshot:()=>[{key:"cell:0",value:text,baseline:"raw"}],
    aiFirstRowValues:items=>Object.fromEntries(items.map(item=>[item.key,item.value])),
    aiFirstClone:value=>JSON.parse(JSON.stringify(value)),aiFirstSameEdits:(a,b)=>JSON.stringify(a)===JSON.stringify(b),
    updateAIFirstBulkControls:()=>{},renderAIFirstReview:()=>{calls.renders++;},renderAIFirstReviewSyncWarning:()=>{},syncAIFirstSelectionUI:()=>{},
    forgetAIFirstSelectedRow:id=>state.aiFirst.selectedRows.delete(id),
    confirm:message=>{calls.prompts.push(message);return confirmAnswer;},toast:message=>calls.toasts.push(message),
    api:async(url,init)=>{
      const body=JSON.parse(init.body);calls.preview.push({url,body});
      assert.equal(body.mode,"preview");
      if(options.previewError) throw options.previewError;
      if(options.editDuringPreview) text="newer edit";
      return options.preview||{can_commit:true,corrected_cell_count:1,invalidated_decision_ids:["dependent"],invalidated_role_ids:[]};
    },
    postAIFirstReview:async(action,body)=>{calls.commit.push({action,body});const result=options.result||{saved:true,refreshed:true};if(result.saved&&result.refreshed)context.renderAIFirstReview();return result;},
    refreshAIFirstReviewState:async()=>{calls.refreshed=true;},
  };
  vm.runInNewContext(source.slice(start,end),context);
  return {state,calls,context};
}

async function run(){
  const ok=harness();await ok.context.reviewSelectedAIFirstRowsAtomically();
  assert.equal(ok.calls.preview.length,1);assert.equal(ok.calls.commit.length,1,"one mutating endpoint call follows read-only preview");
  const request=ok.calls.commit[0];
  assert.equal(request.action,"bulk-role-decision");assert.equal(request.body.mode,"commit");
  assert.equal(JSON.stringify(request.body.record_ids),JSON.stringify(["visible"]),"filtered and other-page selection never enters the batch");
  assert.equal(request.body.values.visible["cell:0"],"edited");assert.equal(request.body.editor_revision,4);
  assert.equal(request.body.operation_id,ok.calls.preview[0].body.operation_id,"preview and commit use the same idempotency key and snapshot");
  assert.equal(request.body.provenance.visible.extraction_fingerprint,"fp-1");
  assert.deepEqual([...ok.state.aiFirst.selectedRows],["hidden","other-page"]);
  assert.equal(ok.state.aiFirst.pendingRowValues["other-page"]["cell:0"],"keep");
  assert.match(ok.calls.prompts[0],/Затронутых строк к пересмотру: 1/);
  assert.match(ok.calls.prompts[0],/Стр\. 2, таблица 2, строка 7 — В составе комплекта: подтверждение значений/);
  assert.equal(ok.calls.renders,1,"successful POST+GET must perform one full render, not a second render in the bulk handler");
  assert.equal(ok.state.aiFirst.pendingBulkOperation,null);

  for(const config of [{confirm:false},{preview:{can_commit:false,errors:[{code:"bulk_selection_has_open_issues"}]}},{editDuringPreview:true},{preview:{can_commit:true,invalidated_decision_ids:["missing-row"]}}]){
    const rejected=harness(config);await rejected.context.reviewSelectedAIFirstRowsAtomically();
    assert.equal(rejected.calls.commit.length,0,"cancel, invalid preview and changed in-flight values cannot mutate review");
    assert(rejected.state.aiFirst.selectedRows.has("visible"));
    assert.equal(rejected.state.aiFirst.pendingRowValues.visible["cell:0"],"edited");
  }
  const service=harness({role:"section"});await service.context.reviewSelectedAIFirstRowsAtomically();
  assert.equal(service.calls.commit[0].body.intent,"exclude_from_positions");assert.match(service.calls.prompts[0],/сохранив исходные строки/);
  const component=harness({role:"component",parent:"checked-parent"});await component.context.reviewSelectedAIFirstRowsAtomically();
  assert.equal(component.calls.commit[0].body.parents.visible,"checked-parent");
  const dependent=harness({preview:{can_commit:true,invalidated_decision_ids:["dependent"],invalidated_role_ids:["dependent"]}});await dependent.context.reviewSelectedAIFirstRowsAtomically();
  assert.match(dependent.calls.prompts[0],/Затронутых строк к пересмотру: 1/);
  assert.match(dependent.calls.prompts[0],/В составе комплекта: роль и подтверждение значений/);
  const unknown=harness({result:{saved:null,outcome:"unknown",refreshed:false}});await unknown.context.reviewSelectedAIFirstRowsAtomically();
  assert(unknown.state.aiFirst.selectedRows.has("visible"));
  assert.equal(unknown.calls.commit.length,1);assert(unknown.state.aiFirst.pendingBulkOperation);
  assert.equal(Object.hasOwn(unknown.state.aiFirst.pendingBulkOperation.values,"other-page"),false,"recovery metadata must not consume unrelated edits");
  const locked=harness();locked.state.aiFirst.reviewSyncRequired=true;await locked.context.reviewSelectedAIFirstRowsAtomically();
  assert.equal(locked.calls.preview.length,0);assert.equal(locked.calls.commit.length,0);
  const conflict=harness({previewError:{code:"review_revision_conflict"}});await conflict.context.reviewSelectedAIFirstRowsAtomically();
  assert.equal(conflict.calls.refreshed,true,"stale preview uses GET recovery instead of leaving the user with an unusable token");
  assert.equal(conflict.calls.commit.length,0);
  // The old inspector DOM must not recreate an obsolete role after fresh state arrives.
  const oldData={review_token:"old"},newData={review_token:"new"};
  const realState={aiFirst:{data:oldData,pendingRowValues:{},pendingRowRoles:{other:{role:"section"}},pendingIssueNotes:{}}};
  let realPosts=0,realRenders=0,restoredView=null;
  const currentView={scrollLeft:160,scrollTop:77,focus:{type:"cell",id:"other",column:"1"},selectionStart:2,selectionEnd:4};
  const real={state:realState,aiFirstRequestContext:()=>({documentId:"doc"}),isCurrentAIFirstContext:()=>true,
    aiFirstClone:value=>JSON.parse(JSON.stringify(value)),aiFirstSameEdits:(a,b)=>JSON.stringify(a)===JSON.stringify(b),
    updateAIFirstBulkControls:()=>{},toast:()=>{},
    captureAIFirstViewState:()=>currentView,restoreAIFirstViewState:view=>{assert.equal(realState.aiFirst.mutationInFlight,false);restoredView=view;},
    captureAIFirstPendingEdits:()=>{if(realState.aiFirst.data===newData)realState.aiFirst.pendingRowRoles.visible={role:"unknown"};},
    renderAIFirstReview:()=>{realRenders++;if(realState.aiFirst.skipPendingCaptureOnce)realState.aiFirst.skipPendingCaptureOnce=false;else real.captureAIFirstPendingEdits();},
    api:async(url,options={})=>{if(options.method==="POST"){realPosts++;return {};}return newData;},
  };
  const postStart=source.indexOf("async function postAIFirstReview"),postEnd=source.indexOf("async function confirmAIFirstPageCompleteness",postStart);
  vm.runInNewContext(source.slice(postStart,postEnd),real);
  const saved=await real.postAIFirstReview("bulk-role-decision",{record_ids:["visible"]});
  assert.equal(saved.refreshed,true);assert.equal(realState.aiFirst.pendingRowRoles.visible,undefined);
  assert.equal(realState.aiFirst.pendingRowRoles.other.role,"section","unrelated role buffers survive the state refresh");
  assert.equal(realRenders,1,"the real POST+GET path renders exactly once");
  assert.equal(restoredView,currentView,"focus/caret and scroll are restored after controls unlock without another render");
  realState.aiFirst.bulkPreviewInFlight=true;
  assert.equal((await real.postAIFirstReview("bulk-confirm",{record_ids:["visible"]})).blocked,true);
  const oldStart=source.indexOf("async function confirmSelectedAIFirstRows"),oldEnd=source.indexOf("async function cancelAIFirstRecognition",oldStart);
  vm.runInNewContext(source.slice(oldStart,oldEnd),real);
  await real.confirmSelectedAIFirstRows();await real.assignSelectedAIFirstRole();
  assert.equal(realPosts,1,"legacy bulk handlers and direct review POST cannot mutate during preview");
  const confirmButton={},roleButton={},combinedButton={};
  const ui={state:{aiFirst:{selectedRows:new Set(["visible"]),bulkPreviewInFlight:true}},aiFirstVisibleSelectedIds:()=>["visible"],
    document:{querySelector:selector=>selector==="#ai-first-review-controls"?{querySelector:q=>({"#ai-first-confirm-selected":confirmButton,"[data-ai-first-bulk-role-submit]":roleButton,"[data-ai-first-bulk-review-submit]":combinedButton,"#ai-first-bulk-role":{value:"standalone_item"}}[q]||null)}:selector==="#ai-first-table-scroll"?{querySelectorAll:()=>[{dataset:{aiFirstRow:"visible"}}]}:null}};
  const updateStart=source.indexOf("function updateAIFirstBulkControls"),updateEnd=source.indexOf("function applyAIFirstReviewFilters",updateStart);
  vm.runInNewContext(source.slice(updateStart,updateEnd),ui);ui.updateAIFirstBulkControls();
  assert.equal(confirmButton.disabled,true);assert.equal(roleButton.disabled,true);assert.equal(combinedButton.disabled,true);
  ui.state.aiFirst.bulkPreviewInFlight=false;ui.updateAIFirstBulkControls();
  assert.equal(confirmButton.disabled,false);assert.equal(roleButton.disabled,false);
  console.log("PASS: atomic role+decision preview, one mutation, exact visible snapshot, changed-input rejection, role intent, operation identity and recovery isolation");
}
run().catch(error=>{console.error(error);process.exitCode=1;});
