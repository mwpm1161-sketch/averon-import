const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const app = fs.readFileSync(process.argv[2], "utf8");
function sourceBetween(start, end) {
  const first = app.indexOf(start);
  const last = app.indexOf(end, first + start.length);
  assert(first >= 0 && last > first, "source block not found: " + start);
  return app.slice(first, last);
}
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
function makeHarness() {
  const nodes = new Map();
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {
      hidden:false, disabled:false, textContent:"", innerHTML:"", open:false,
      addEventListener(event, callback) { this["on" + event] = callback; },
      showModal() { this.open = true; }, close() { this.open = false; },
    });
    return nodes.get(selector);
  };
  const state = {excelTender:null, sourcing:null};
  const apiCalls = [];
  const pollCalls = [];
  const context = vm.createContext({
    state,
    $$: () => [],
    SOURCING_MODE_LABELS:{provider_only:"Поставщик",one_c_only:"История 1С",one_c_then_provider:"История 1С → поставщик"},
    $:node,
    api:(url, options) => {
      apiCalls.push({url, options});
      return Promise.reject(new Error("Unexpected API call: " + url));
    },
    getJobForPolling:jobId => {
      pollCalls.push(jobId);
      return Promise.reject(new Error("Unexpected poll: " + jobId));
    },
    escapeHtml:value => String(value ?? ""),
    renderSourcingHistoryControls:() => {},
    renderSharedProviderSelectionPanel:() => {},
    renderExcelMultiProviderPanel:() => {},
    loadSourcingHistoryStatus:() => Promise.resolve(),
    refreshExcelTenderRuns:() => { context.excelRunRefreshes = (context.excelRunRefreshes || 0) + 1; },
    sourcingModeLoadingCopy:() => ({title:"Searching",detail:"Loading"}),
    renderSourcingResult:(result, row) => {
      context.lastRenderedResult = result;
      context.lastRenderedRow = row;
      node("#sourcing-content").innerHTML = "RESULT:" + String(result?.label || "project");
      context.setSourcingModalPhase("project_result");
    },
    setTimeout:(callback) => { callback(); return 0; },
    Promise,
    Number,
    String,
    Set,
  });
  context.apiCalls = apiCalls;
  context.pollCalls = pollCalls;
  context.makeSourcingState = vm.runInContext(
    sourceBetween("function createSourcingState(overrides = {}) {", "\nconst state =" ) + "; createSourcingState;",
    context,
  );
  const blocks = [
    sourceBetween("function restoreSourcingSubmitButton(", "function setSourcingModalPhase("),
    sourceBetween("function setSourcingModalPhase(", "function sourcingModeLoadingCopy("),
    sourceBetween("async function handleSharedProviderSelectionToggle() {", "async function startSharedSourcing("),
    sourceBetween("async function openSourcingForRow(row) {", "async function executeSourcingForRow("),
    sourceBetween("async function executeSourcingForRow(", "async function runProjectSourcing("),
    sourceBetween("async function runProjectSourcing(", "async function executeProjectSourcing("),
    sourceBetween("async function executeProjectSourcing(", "async function openProjectSourcing("),
    sourceBetween("async function pollSourcingJob(", "function cellHtml("),
    sourceBetween("async function pollExcelTenderJob(", "async function startExcelTenderSourcing("),
    sourceBetween("function commercialEvidenceNote(", "function renderOutcomeNativeSummary("),
  ];
  vm.runInContext(blocks.join("\n"), context);
  state.sourcing = context.makeSourcingState();
  context.openSourcingModal = () => {
    node("#sourcing-modal").showModal();
  };
  return {context, state, node, apiCalls, pollCalls};
}

async function testLateRowResponsesCannotReplaceNewPreflight() {
  for (const lateFailure of [false, true]) {
    const harness = makeHarness();
    const {context, state, node} = harness;
    const rowA = {name:"Row A"};
    const rowB = {name:"Row B"};
    await context.openSourcingForRow(rowA);
    const request = deferred();
    context.api = () => request.promise;
    const oldRun = context.executeSourcingForRow(rowA);

    node("#sourcing-modal").close();
    context.handleSourcingModalClose();
    await context.openSourcingForRow(rowB);
    const pendingContent = node("#sourcing-content").innerHTML;
    if (lateFailure) request.reject(new Error("late A failure"));
    else request.resolve({label:"Row A result"});
    await oldRun;

    assert.equal(state.sourcing.row, rowB);
    assert.equal(state.sourcing.pendingSourcing.row, rowB);
    assert.equal(state.sourcing.modalPhase, "before_run");
    assert.equal(state.sourcing.result, null);
    assert.equal(node("#sourcing-content").innerHTML, pendingContent);
    assert(!node("#sourcing-content").innerHTML.includes("Row A"));
  }
}

async function testLateCapabilityPreflightCannotMutateNewContext() {
  const {context, state, node} = makeHarness();
  const request = deferred();
  await context.openSourcingForRow({name:"A"});
  context.api = () => request.promise;
  const oldPreflight = context.handleSharedProviderSelectionToggle();
  node("#sourcing-modal").close();
  context.handleSourcingModalClose();
  await context.openSourcingForRow({name:"B"});
  request.resolve({providers:[{key:"etm_ipro"}]});
  await oldPreflight;
  assert.equal(state.sourcing.modalPhase, "before_run");
  assert.equal(state.sourcing.pendingSourcing.row.name, "B");
  assert.equal(state.sourcing.providerSelectionEnabled, false);
  assert.equal(state.sourcing.providerSelectionCapabilities, null);
}

async function testLateProjectSubmissionCannotReplaceNewPreflight() {
  const {context, state, node} = makeHarness();
  const button = {textContent:"Start", disabled:false};
  await context.runProjectSourcing([{name:"Project A"}], "doc-a", button);
  const submission = deferred();
  context.api = () => submission.promise;
  const oldRun = context.executeProjectSourcing([{name:"Project A"}], "doc-a", button);
  node("#sourcing-modal").close();
  context.handleSourcingModalClose();
  await context.runProjectSourcing([{name:"Project B"}], "doc-b", button);
  const newContent = node("#sourcing-content").innerHTML;
  submission.resolve({id:"job-a"});
  await oldRun;
  assert.equal(state.sourcing.pendingSourcing.rows[0].name, "Project B");
  assert.equal(state.sourcing.modalPhase, "before_run");
  assert.equal(node("#sourcing-content").innerHTML, newContent);
  assert.equal(context.pollCalls.length, 0);
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, "Start");
}

async function testProjectPollingAndOldCleanupCannotTouchNewRun() {
  const {context, state, node} = makeHarness();
  const button = {textContent:"Start", disabled:false};
  await context.runProjectSourcing([{name:"Project A"}], "doc-a", button);
  const submitted = deferred();
  const oldPoll = deferred();
  context.api = () => submitted.promise;
  context.getJobForPolling = () => {
    context.pollCalls.push("job-a");
    return oldPoll.promise;
  };
  const oldRun = context.executeProjectSourcing([{name:"Project A"}], "doc-a", button);
  submitted.resolve({id:"job-a"});
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(context.pollCalls, ["job-a"]);
  assert.equal(state.sourcing.modalPhase, "running");
  node("#sourcing-modal").close();
  context.handleSourcingModalClose();
  await context.runProjectSourcing([{name:"Project B"}], "doc-b", button);
  button.disabled = true;
  button.textContent = "Project B active";
  const newContent = node("#sourcing-content").innerHTML;
  oldPoll.resolve({status:"completed",result:{label:"Project A result"}});
  await oldRun;
  assert.equal(state.sourcing.pendingSourcing.rows[0].name, "Project B");
  assert.equal(state.sourcing.modalPhase, "before_run");
  assert.equal(node("#sourcing-content").innerHTML, newContent);
  assert.equal(context.lastRenderedResult, undefined);
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, "Project B active");
}

async function testNormalSearchAndImmediateRetryStillWork() {
  const {context, state, node} = makeHarness();
  const row = {name:"Normal row"};
  await context.openSourcingForRow(row);
  context.api = async () => ({label:"Normal result"});
  await context.executeSourcingForRow(row);
  assert.equal(context.lastRenderedResult.label, "Normal result");
  assert.equal(state.sourcing.modalPhase, "project_result");

  await context.openSourcingForRow(row);
  let attempts = 0;
  context.api = async () => {
    attempts += 1;
    if (attempts === 1) throw new Error("temporary failure");
    return {label:"Retry result"};
  };
  await context.executeSourcingForRow(row);
  assert.equal(state.sourcing.modalPhase, "failed");
  await context.executeSourcingForRow(row);
  assert.equal(context.lastRenderedResult.label, "Retry result");
  assert.equal(attempts, 2);
  assert(node("#sourcing-content").innerHTML.includes("Retry result"));
}

async function testLateExcelPollingCannotReplaceSharedContext() {
  const {context, state, node} = makeHarness();
  const request = deferred();
  state.excelTender = {
    workspace:{tender_id:"tender-a"}, pollGeneration:1, sourcingActive:true, jobId:"job-a",
  };
  state.sourcing.modalContext = "excel_tender";
  state.sourcing.requestGeneration = 7;
  node("#sourcing-modal").open = true;
  context.getJobForPolling = () => request.promise;
  const generation = state.sourcing.requestGeneration;
  const oldPoll = context.pollExcelTenderJob(
    "job-a", "tender-a", 1, 1,
    () => state.sourcing.requestGeneration === generation
      && state.sourcing.modalContext === "excel_tender"
      && node("#sourcing-modal").open,
  );
  await new Promise(resolve => setImmediate(resolve));
  await context.openSourcingForRow({name:"Shared row B"});
  const newContent = node("#sourcing-content").innerHTML;
  request.resolve({status:"completed",result:{label:"Excel row A result",source_mode:"provider_only"}});
  await oldPoll;
  assert.equal(state.sourcing.row.name, "Shared row B");
  assert.equal(state.sourcing.modalContext, "shared");
  assert.equal(state.sourcing.modalPhase, "before_run");
  assert.equal(node("#sourcing-content").innerHTML, newContent);
  assert.equal(context.lastRenderedResult, undefined);
}

function testSerializedVatBasisLabels() {
  const {context} = makeHarness();
  const note = evidence => context.commercialEvidenceNote({}, evidence);
  assert.equal(note({state:"COMPLETE",vat_basis:"GROSS_INCLUDING_VAT"}), "Коммерческие сведения проверены: цена с НДС.");
  assert.equal(note({state:"COMPLETE",vat_basis:"NET_EXCLUDING_VAT"}), "Коммерческие сведения проверены: цена без НДС.");
  assert.equal(note({state:"COMPLETE",vat_basis:"UNKNOWN"}), "Коммерческие сведения проверены: основание НДС требует проверки.");
  for (const state of ["INCOMPLETE", "INVALID"]) {
    assert.equal(note({state,vat_basis:"GROSS_INCLUDING_VAT"}), "Предварительная цена: НДС и единица цены не подтверждены.");
  }
}

(async () => {
  await testLateRowResponsesCannotReplaceNewPreflight();
  await testLateCapabilityPreflightCannotMutateNewContext();
  await testLateProjectSubmissionCannotReplaceNewPreflight();
  await testProjectPollingAndOldCleanupCannotTouchNewRun();
  await testNormalSearchAndImmediateRetryStillWork();
  await testLateExcelPollingCannotReplaceSharedContext();
  testSerializedVatBasisLabels();
  console.log("PASS: SOL F1/F2/F3 lifecycle regressions");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
