const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync(process.argv[2], "utf8");
function sliceBetween(startText, endText) {
  const start = source.indexOf(startText);
  const end = source.indexOf(endText, start);
  assert(start >= 0 && end > start, `Unable to find source section ${startText}`);
  return source.slice(start, end);
}

const runId = suffix => suffix.repeat(32);
const oldRunId = runId("a");
const newRunId = runId("b");
const savedRunId = oldRunId;
const oldExportId = runId("d");
const newExportId = runId("e");
const oldRun = {run_id:oldRunId,status:"completed",source_mode:"provider_only",completed_at:"2026-10-01T10:00:00Z",summary:{}};
const newRun = {run_id:newRunId,status:"completed",source_mode:"one_c_only",completed_at:"2026-10-02T10:00:00Z",summary:{}};
const oldExport = {export_id:oldExportId,run_id:oldRunId,filename:"old.xlsx",created_at:"2026-10-01T10:00:00Z",priced_count:1};
const newExport = {export_id:newExportId,run_id:newRunId,filename:"new.xlsx",created_at:"2026-10-02T10:00:00Z",priced_count:2};

const nodes = new Map();
for (const selector of [
  "#tender-export-run", "#tender-export-artifact", "#tender-export-button", "#tender-export-download",
  "#tender-last-run", "#sourcing-modal", "#sourcing-subtitle", "#sourcing-content", "#tender-export-status",
  "#tender-export-failure", "#tender-export-failure-message",
]) {
  nodes.set(selector, {value:"", innerHTML:"", textContent:"", disabled:false, hidden:false, open:true, showModal() { this.open = true; }});
}

const state = {
  excelTender: {
    workspace:{tender_id:"tender"}, pollGeneration:4, sourcingActive:true, jobId:"source-job",
    exportActive:false, exportJobId:null, selectedExportRunId:oldRunId, selectedExportId:oldExportId,
    latestRuns:[oldRun], latestExports:[oldExport], historyDecisionRevision:0, historyDecisionDigest:null,
  },
  sourcing:{result:null},
};
const apiCalls = [];
const context = {
  state,
  api:async (url, options = {}) => {
    apiCalls.push({url,method:options.method || "GET",body:options.body || null});
    if (options.method === "POST") return {id:"export-job"};
    if (url.endsWith("/runs")) {
      assert.equal(state.excelTender.selectedExportRunId,newRunId,"new durable run is pinned before run controls refresh");
      assert.equal(state.excelTender.selectedExportId,null,"old export selection is cleared before refresh");
      return {runs:[newRun,oldRun]};
    }
    if (url.endsWith("/exports")) return {exports:[oldExport,newExport]};
    if (url.endsWith("/history-decisions")) return {rows:[],effective:{},decision_revision:0,decision_digest:"0"};
    if (url.endsWith(`/runs/${savedRunId}`)) return {run_id:savedRunId,source_mode:"provider_only",summary:{},rows:[]};
    throw new Error(`Unexpected API request: ${url}`);
  },
  getJobForPolling:async () => ({status:"completed",result:{run_id:newRunId,source_mode:"provider_only",results:[]}}),
  renderSourcingResult(result) { state.sourcing.result = result; },
  renderExcelTenderLatestRun() {},
  refreshExcelTenderRuns:null,
  renderExcelTenderExportControls:null,
  refreshExcelTenderExports:null,
  applyTenderHistoryDecisionSnapshot(result) { return result; },
  setSourcingModalPhase() {},
  escapeHtml:value => String(value).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;"),
  $:selector => nodes.get(selector),
  clearTenderExportFailure() {},
  pollExcelTenderPriceExport:async (jobId, tenderId, generation) => {
    context.polledExport = {jobId,tenderId,generation};
  },
  encodeURIComponent,
};

const code = [
  sliceBetween("async function openExcelTenderRunDetail(runId = null) {", "async function refreshExcelTenderRuns"),
  sliceBetween("async function refreshExcelTenderRuns", "function renderExcelTenderExportControls"),
  sliceBetween("function renderExcelTenderExportControls() {", "async function refreshExcelTenderExports"),
  sliceBetween("async function refreshExcelTenderExports", "function tenderExportConfirmationSummary"),
  sliceBetween("function tenderExportConfirmationSummary", "function clearTenderExportFailure"),
  sliceBetween("async function startExcelTenderPriceExport() {", "async function pollExcelTenderPriceExport"),
  sliceBetween("async function pollExcelTenderJob(jobId, tenderId, generation, expectedTotal) {", "async function startExcelTenderSourcing()"),
].join("\n");
vm.runInNewContext(`${code}; globalThis.runPoll=pollExcelTenderJob; globalThis.openRun=openExcelTenderRunDetail; globalThis.renderExportControls=renderExcelTenderExportControls; globalThis.startPriceExport=startExcelTenderPriceExport; globalThis.summary=tenderExportConfirmationSummary;`, context);

(async function main() {
  await context.runPoll("source-job","tender",4,1);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(state.excelTender.selectedExportRunId,newRunId,"successful sourcing pins the new run");
  assert.equal(state.excelTender.selectedExportId,newExportId,"cross-run export is replaced by newest artifact from the pinned run");
  assert(!nodes.get("#tender-export-artifact").innerHTML.includes(oldExportId),"artifact selector excludes exports from other runs");
  assert(nodes.get("#tender-export-artifact").innerHTML.includes(newExportId));
  assert(nodes.get("#tender-export-run").innerHTML.includes(`запуск ${newRunId.slice(0,8)}`),"selected run option visibly includes its short ID");

  state.excelTender.sourcingActive = false;
  await context.startPriceExport();
  const exportPost = apiCalls.find(call => call.method === "POST");
  assert(exportPost, "export request should be submitted");
  assert(exportPost.url.includes(`/runs/${newRunId}/export`),"export POST targets the pinned run rather than a latest-run fallback");

  state.excelTender.latestExports = [oldExport,newExport];
  state.excelTender.selectedExportId = newExportId;
  await context.openRun(oldRunId);
  assert.equal(state.excelTender.selectedExportRunId,oldRunId,"opening saved run detail binds export context to that run");
  assert.equal(state.excelTender.selectedExportId,oldExportId,"saved-run open selects an artifact from that run");
  assert(!nodes.get("#tender-export-artifact").innerHTML.includes(newExportId));

  state.excelTender.selectedExportRunId = newRunId;
  state.excelTender.selectedExportId = oldExportId;
  context.renderExportControls();
  assert.equal(state.excelTender.selectedExportRunId,newRunId,"manual selector choice remains authoritative across rerender");
  assert.equal(state.excelTender.selectedExportId,newExportId,"cross-run artifact cannot remain selected after manual run change");
  assert.equal(nodes.get("#tender-export-run").value,newRunId);
  assert(source.includes('state.excelTender.selectedExportRunId = event.target.value || null;'),"manual run selector change writes the explicit user choice");
  assert(context.summary({selected_count:3,priced_count:2,blank_count:1,historical_count:0},newRunId).includes(`Запуск: ${newRunId.slice(0,8)}`),"confirmation summary identifies the run");
  process.stdout.write("PASS: Excel Tender run pinning, saved-run binding, manual selection, export POST target, and artifact scoping\n");
})().catch(error => { process.stderr.write(`${error.stack || error}\n`); process.exitCode = 1; });
