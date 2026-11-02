const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.resolve(__dirname, "../../averon_import/static/ai_first_ui.js"), "utf8");
function sourceBetween(start, end) {
  const first = source.indexOf(start), last = source.indexOf(end, first + start.length);
  assert(first >= 0 && last > first, "source block not found: " + start);
  return source.slice(first, last);
}

class Control {
  constructor(dataset={},value="") { this.dataset=dataset; this.value=value; this.hidden=false; this.checked=false; this.disabled=false; this.textContent=""; this.handlers={}; }
  addEventListener(name,handler) { this.handlers[name]=handler; }
  fire(name="click") { return this.handlers[name]?.({target:this}); }
}

function createHarness({getFails=false,postFails=false,losePostResponse=false}={}) {
  const page1={page_number:1,extraction_fingerprint:"fp-1"};
  const page2={page_number:2,extraction_fingerprint:"fp-2"};
  const oldEntries=[
    {row:{id:"a",page_number:1},page:page1},
    {row:{id:"b",page_number:1},page:page1},
    {row:{id:"c",page_number:2},page:page2},
    {row:{id:"d",page_number:1},page:{page_number:1,extraction_fingerprint:"fp-d-old"}},
  ];
  const data={review_token:"token-1",draft:{entries:oldEntries,pages:[page1,page2],issues:[{id:"issue-1"}]},review:{row_roles:{},decisions:{}}};
  const state={document:{document_id:"doc-1",page_count:2},authGeneration:0,aiFirst:{documentId:"doc-1",epoch:1,data,
    selectedRows:new Set(["a","b","c"]),selectionRevisions:new Map([
      ["a",{pageNumber:1,fingerprint:"fp-1",pageRecord:page1}],
      ["b",{pageNumber:1,fingerprint:"fp-1",pageRecord:page1}],
      ["c",{pageNumber:2,fingerprint:"fp-2",pageRecord:page2}],
    ]),pendingRowValues:{a:{"cell:0":"local pending"},d:{"cell:0":"stale pending"}},
    pendingRowRoles:{a:{role:"section",parent_record_id:null}},pendingIssueNotes:{"issue-1":"local note","issue-old":"stale note"},
    addRowValues:{"col:0":"manual row"},addRowOpen:true,page:1,reviewSyncRequired:false,reviewRefreshInFlight:false,mutationInFlight:false}};
  const checkboxes=[new Control({aiFirstSelectRow:"a"}),new Control({aiFirstSelectRow:"b"})];
  checkboxes[0].checked=true; checkboxes[1].checked=true;
  const rows=[{dataset:{aiFirstRow:"a"},hidden:true},{dataset:{aiFirstRow:"b"},hidden:false}];
  const tableRoot={querySelectorAll(selector){
    if(selector==="tr[data-ai-first-row]") return rows;
    if(selector==="[data-ai-first-select-row]") return checkboxes;
    return [];
  }};
  const confirmButton=new Control(),roleButton=new Control(),selectVisible=new Control(),clearPage=new Control(),count=new Control();
  const roleSelect=new Control({},"section"),roleOption=new Control(); roleOption.textContent="Раздел";
  const controlsRoot={querySelector(selector){return ({
    "#ai-first-confirm-selected":confirmButton,
    "[data-ai-first-bulk-role-submit]":roleButton,
    "[data-ai-first-select-visible]":selectVisible,
    "[data-ai-first-clear-page-selection]":clearPage,
    "[data-ai-first-selected-count]":count,
    "#ai-first-bulk-role":roleSelect,
    "#ai-first-bulk-role option:checked":roleOption,
  })[selector] || null;}};
  const refreshButton=new Control();
  const warning={hidden:true,innerHTML:"",querySelector:selector=>selector==="[data-ai-first-refresh-review]"?refreshButton:null};
  let refreshedData=null,postError=postFails?Object.assign(new Error("synthetic validation rejection"),{name:"ApiError",status:422}):null,renderFailures=0;
  const calls={posts:[],gets:0,toasts:[],renders:0,serverApplied:0};
  const document={querySelector(selector){return ({
    "#ai-first-table-scroll":tableRoot,
    "#ai-first-review-controls":controlsRoot,
    "#ai-first-review-sync-warning":warning,
  })[selector] || null;},querySelectorAll(){return [];}};
  const context={state,document,
    confirm:()=>true,
    toast:(message)=>calls.toasts.push(message),
    aiFirstClone:value=>JSON.parse(JSON.stringify(value)),
    aiFirstDocumentEntries:(draft)=>draft?.entries || [],
    aiFirstPageRecord:number=>state.aiFirst.data?.draft?.pages?.find(page=>page.page_number===Number(number)) || null,
    aiFirstEditorSnapshot:()=>({row_values:JSON.parse(JSON.stringify(state.aiFirst.pendingRowValues)),
      row_roles:JSON.parse(JSON.stringify(state.aiFirst.pendingRowRoles)),issue_notes:JSON.parse(JSON.stringify(state.aiFirst.pendingIssueNotes)),
      add_row_values:JSON.parse(JSON.stringify(state.aiFirst.addRowValues)),add_row_page:state.aiFirst.page}),
    captureAIFirstPendingEdits:()=>{},
    aiFirstRowInputSnapshot:()=>[],
    aiFirstRowValues:()=>({}),
    restoreAIFirstEditorDraft: data=>{
      const edits=data.editor_draft?.edits;
      if(!edits || data.editor_draft.stale) return;
      state.aiFirst.pendingRowValues=JSON.parse(JSON.stringify(edits.row_values || {}));
      state.aiFirst.pendingRowRoles=JSON.parse(JSON.stringify(edits.row_roles || {}));
      state.aiFirst.pendingIssueNotes=JSON.parse(JSON.stringify(edits.issue_notes || {}));
      state.aiFirst.addRowValues=JSON.parse(JSON.stringify(edits.add_row_values || {}));
      state.aiFirst.addRowOpen=Object.keys(state.aiFirst.addRowValues).length>0;
    },
    renderAIFirstReview:()=>{
      calls.renders++;
      if(renderFailures){renderFailures--;throw new Error("synthetic render failure");}
      context.reconcileAIFirstSelectedRows(context.aiFirstDocumentEntries(state.aiFirst.data.draft,state.aiFirst.data.review));
      context.syncAIFirstSelectionUI();
      state.aiFirst.skipPendingCaptureOnce=false;
    },
    api:async (url,options={})=>{
      if(options.method==="POST"){
        const body=JSON.parse(options.body); calls.posts.push({url,body});
        if(postError) throw postError;
        if(losePostResponse){calls.serverApplied++;throw new TypeError("fetch connection lost after server applied mutation");}
        return {};
      }
      calls.gets++;
      if(getFails) throw new Error("refresh connection lost");
      return refreshedData || {review_token:"token-refreshed",draft:{entries:oldEntries,pages:[page1,page2],issues:[{id:"issue-1"}]},review:{row_roles:{},decisions:{}}};
    },
  };
  vm.createContext(context);
  vm.runInContext([
    sourceBetween("function aiFirstRequestContext", "function renderAIFirstResumeControls"),
    sourceBetween("function aiFirstVisibleSelectedIds", "function applyAIFirstReviewFilters"),
    sourceBetween("async function postAIFirstReview", "async function confirmAIFirstPageCompleteness"),
    sourceBetween("async function confirmSelectedAIFirstRows", "async function cancelAIFirstRecognition"),
    sourceBetween("async function saveAIFirstReview", "function aiFirstHasUnsavedReviewEdits"),
    "globalThis.confirmRows=confirmSelectedAIFirstRows; globalThis.assignRole=assignSelectedAIFirstRole; globalThis.postReview=postAIFirstReview; globalThis.saveDraft=saveAIFirstReview; globalThis.refreshReview=refreshAIFirstReviewState;",
  ].join("\n"),context);
  return {context,state,rows,checkboxes,confirmButton,roleButton,count,warning,refreshButton,roleSelect,roleOption,controlsRoot,tableRoot,calls,
    setGetFails(value){getFails=value;},setPostFails(value){postFails=value;postError=value?Object.assign(new Error("synthetic validation rejection"),{name:"ApiError",status:422}):null;},
    setPostError(value){postError=value;},failNextRender(){renderFailures++;},setRefreshedData(value){refreshedData=value;}};
}

async function run() {
  const refreshed=createHarness();
  await refreshed.context.confirmRows();
  assert.deepEqual(refreshed.calls.posts[0].body.record_ids,["b"],"only the currently visible row is submitted");
  assert.equal(refreshed.calls.posts.length,1); assert.equal(refreshed.calls.gets,1);
  assert.deepEqual([...refreshed.state.aiFirst.selectedRows],["a","c"],"confirmed ID is removed while hidden and other-page selections survive");
  assert.equal(refreshed.checkboxes[0].checked,true); assert.equal(refreshed.checkboxes[1].checked,false);
  assert.equal(refreshed.count.textContent,"Выбрано 2; видно 0; ещё 1 строка скрыта фильтром и не участвует; 1 на других страницах и не участвуют");
  assert.equal(refreshed.state.aiFirst.reviewSyncRequired,false,"a successful GET keeps mutations enabled");
  assert.deepEqual(refreshed.state.aiFirst.pendingRowValues.a,{"cell:0":"local pending"},"unrelated pending edits survive a successful review refresh");

  const stale=createHarness({getFails:true});
  await stale.context.confirmRows();
  assert.deepEqual(stale.calls.posts[0].body.record_ids,["b"]);
  assert.equal(stale.calls.posts.length,1,"the successful POST is not repeated after GET failure");
  assert.equal(stale.calls.gets,1);
  assert.deepEqual([...stale.state.aiFirst.selectedRows],["a","c"]);
  assert.equal(stale.checkboxes[0].checked,true); assert.equal(stale.checkboxes[1].checked,false);
  assert.equal(stale.count.textContent,"Выбрано 2; видно 0; ещё 1 строка скрыта фильтром и не участвует; 1 на других страницах и не участвуют");
  assert.equal(stale.state.aiFirst.reviewSyncRequired,true);
  assert.equal(stale.confirmButton.disabled,true); assert.equal(stale.roleButton.disabled,true);
  assert.equal(stale.warning.hidden,false); assert.match(stale.warning.innerHTML,/состояние проверки не обновлено/);
  assert.deepEqual(stale.state.aiFirst.pendingRowValues.a,{"cell:0":"local pending"},"pending edits survive the failed GET");
  const blocked=await stale.context.postReview("row",{record_id:"a",disposition:"confirmed"});
  const blockedDraft=await stale.context.saveDraft();
  assert.equal(blocked.blocked,true); assert.equal(blockedDraft.blocked,true);
  assert.equal(stale.calls.posts.length,1,"bulk, row and draft mutations do not POST while review state is stale");

  const page1={page_number:1,extraction_fingerprint:"fp-1"}, page2={page_number:2,extraction_fingerprint:"fp-2"};
  stale.setGetFails(false);
  stale.setRefreshedData({review_token:"token-2",draft:{entries:[
    {row:{id:"a",page_number:1},page:page1},{row:{id:"b",page_number:1},page:page1},
    {row:{id:"c",page_number:2},page:page2},{row:{id:"d",page_number:1},page:{page_number:1,extraction_fingerprint:"fp-d-new"}},
  ],pages:[page1,page2],issues:[{id:"issue-1"}]},review:{row_roles:{},decisions:{}},editor_draft:{edits:{
    row_values:{a:{"cell:0":"server draft"}},row_roles:{},issue_notes:{},add_row_values:{},add_row_page:null,
  }}});
  assert.equal(typeof stale.refreshButton.handlers.click,"function","the warning exposes an explicit state-refresh action");
  await stale.context.refreshReview();
  assert.equal(stale.calls.posts.length,1,"explicit refresh is GET-only"); assert.equal(stale.calls.gets,2);
  assert.equal(stale.state.aiFirst.reviewSyncRequired,false); assert.equal(stale.state.aiFirst.data.review_token,"token-2");
  assert.equal(JSON.stringify(stale.state.aiFirst.pendingRowValues.a),JSON.stringify({"cell:0":"local pending"}),"compatible local edits take precedence over a saved draft snapshot");
  assert.equal(stale.state.aiFirst.pendingRowValues.d,undefined,"edits from a changed extraction revision are not reattached");
  assert.equal(JSON.stringify(stale.state.aiFirst.pendingRowRoles.a),JSON.stringify({role:"section",parent_record_id:null}));
  assert.equal(JSON.stringify(stale.state.aiFirst.pendingIssueNotes),JSON.stringify({"issue-1":"local note"}));
  assert.equal(JSON.stringify(stale.state.aiFirst.addRowValues),JSON.stringify({"col:0":"manual row"}));
  assert.deepEqual([...stale.state.aiFirst.selectedRows],["a","c"],"valid selections survive explicit state refresh");
  assert.equal(stale.warning.hidden,true);

  const roleFailure=createHarness({getFails:true});
  roleFailure.roleSelect.value="section";
  await roleFailure.context.assignRole();
  assert.deepEqual(roleFailure.calls.posts[0].body.record_ids,["b"]);
  assert.equal(roleFailure.calls.posts[0].body.role,"section","bulk-role meaning is unchanged");
  assert.equal(roleFailure.state.aiFirst.reviewSyncRequired,true);
  assert.deepEqual([...roleFailure.state.aiFirst.selectedRows],["a","c"]);
  assert.equal(roleFailure.checkboxes[0].checked,true); assert.equal(roleFailure.checkboxes[1].checked,false);
  assert.equal(roleFailure.calls.posts.length,1);

  const rejected=createHarness({postFails:true});
  const result=await rejected.context.confirmRows();
  assert.deepEqual([...rejected.state.aiFirst.selectedRows],["a","b","c"],"a rejected POST leaves all selection intact");
  assert.equal(rejected.checkboxes[0].checked,true); assert.equal(rejected.checkboxes[1].checked,true);
  assert.equal(rejected.calls.posts.length,1); assert.equal(rejected.calls.gets,0);
  assert.equal(rejected.state.aiFirst.reviewSyncRequired,false);
  assert.equal(rejected.confirmButton.disabled,false,"a definitive HTTP 422 re-enables bulk confirmation for visible selected rows");
  assert.equal(rejected.roleButton.disabled,false,"a definitive HTTP 422 re-enables bulk role assignment for visible selected rows");

  const lost=createHarness({losePostResponse:true});
  await lost.context.confirmRows();
  assert.equal(lost.calls.serverApplied,1,"the fake server applies the mutation before dropping its response");
  assert.equal(lost.calls.posts.length,1); assert.equal(lost.calls.gets,0,"no automatic GET/POST retry follows the transport failure");
  assert.equal(lost.state.aiFirst.reviewSyncRequired,true);
  assert.equal(lost.confirmButton.disabled,true); assert.equal(lost.roleButton.disabled,true,"unknown outcome retains the bulk-action lock");
  assert.equal(lost.state.aiFirst.uncertainReviewMutation.action,"bulk-confirm");
  assert.equal(JSON.stringify(lost.state.aiFirst.uncertainReviewMutation.recordIds),JSON.stringify(["b"]));
  assert.equal(lost.warning.hidden,false); assert.match(lost.warning.innerHTML,/сервер мог его сохранить/);
  assert.deepEqual([...lost.state.aiFirst.selectedRows],["a","b","c"],"selection remains until a read confirms current state");
  const blockedPost=await lost.context.postReview("bulk-confirm",{record_ids:["b"]});
  assert.equal(blockedPost.saved,false); assert.equal(blockedPost.blocked,true);
  assert.equal(lost.calls.posts.length,1,"no second write is sent while outcome is uncertain");

  const outcomeProbe=createHarness({losePostResponse:true});
  const unknownOutcome=await outcomeProbe.context.postReview("bulk-confirm",{record_ids:["b"]});
  assert.equal(unknownOutcome.saved,null,"a lost transport response is reported as unknown, not as a confirmed rejection");
  assert.equal(unknownOutcome.outcome,"unknown");

  const latestPage1={page_number:1,extraction_fingerprint:"fp-1"},latestPage2={page_number:2,extraction_fingerprint:"fp-2"};
  lost.setRefreshedData({review_token:"token-after-lost-response",draft:{entries:[
    {row:{id:"a",page_number:1},page:latestPage1},{row:{id:"b",page_number:1},page:latestPage1},
    {row:{id:"c",page_number:2},page:latestPage2},
  ],pages:[latestPage1,latestPage2],issues:[{id:"issue-1"}]},review:{row_roles:{},decisions:{b:{active:true,disposition:"confirmed",human_confirmed:true}}}});
  lost.failNextRender();
  let syncResult=await lost.context.refreshReview();
  assert.equal(syncResult.refreshed,false,"a render exception makes recovery unsuccessful");
  assert.equal(lost.state.aiFirst.reviewSyncRequired,true,"render failure keeps the stale-state mutation lock");
  assert.equal(lost.state.aiFirst.reviewRefreshInFlight,false,"a failed refresh can be retried");
  assert.equal(lost.confirmButton.disabled,true); assert.equal(lost.roleButton.disabled,true);
  assert.equal(lost.warning.hidden,false); assert.deepEqual([...lost.state.aiFirst.selectedRows],["a","b","c"]);
  assert.equal(lost.calls.posts.length,1,"render failure still does not repeat the mutation");
  syncResult=await lost.context.refreshReview();
  assert.equal(syncResult.refreshed,true,"the operator can retry the GET-only refresh");
  assert.equal(lost.state.aiFirst.reviewSyncRequired,false);
  assert.equal(lost.state.aiFirst.uncertainReviewMutation,null);
  assert.deepEqual([...lost.state.aiFirst.selectedRows],["a","c"],"confirmed IDs from the uncertain operation are removed after successful synchronization");
  assert.equal(lost.checkboxes[0].checked,true); assert.equal(lost.checkboxes[1].checked,false);
  assert.equal(lost.calls.posts.length,1,"server-applied mutation was never posted twice");

  const conflict=createHarness();
  conflict.setPostError(Object.assign(new Error("revision changed"),{name:"ApiError",status:409,code:"review_revision_conflict"}));
  const conflictResult=await conflict.context.postReview("row",{record_id:"a"});
  assert.equal(conflictResult.conflict,true); assert.equal(conflict.calls.gets,1,"revision conflicts retain their existing automatic state reload");
  assert.equal(conflict.state.aiFirst.reviewSyncRequired,false);
  assert.equal(conflict.state.aiFirst.data.review_token,"token-refreshed");
  console.log("PASS: bulk confirm/role preserve non-submitted selections on POST+GET outcomes; failed refresh locks mutations, explicit GET recovers state without replay and keeps compatible edits");
}

run().catch(error=>{console.error(error);process.exitCode=1;});
