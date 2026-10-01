const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[2], "utf8");
const start = source.indexOf("function renderExcelTenderRows() {");
const end = source.indexOf("async function deleteExcelTenderWorkspace()", start);
if (start < 0 || end < 0) throw new Error("Tender row renderer was not found");
const nodes = {
  "#tender-row-search": {value: ""},
  "#tender-rows": {innerHTML: ""},
  "#tender-row-count": {textContent: ""},
  "#tender-select-all": {checked: false, indeterminate: false},
};
const context = {
  state: {excelTender: {workspace: {rows: Array.from({length: 370}, (_, index) => ({
    source_row_id: `row-${index}`, row_type: "item", excel_row: index + 13,
    resource_code: `R-${index % 7}`, name: `Синтетическая позиция ${index + 1}`,
    raw_unit: "шт", quantity_raw: "1", article: "", manufacturer: "", model: "",
    quantity_trusted: true, warnings: [],
  }))}, selectedIds: new Set(), filterTimer: null}},
  $: selector => nodes[selector],
  escapeHtml: value => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;"),
};
vm.runInNewContext(`${source.slice(start, end)}; renderExcelTenderRows();`, context);
const allRows = (nodes["#tender-rows"].innerHTML.match(/<tr\b/g) || []).length;
if (allRows !== 370) throw new Error(`Expected 370 rows, got ${allRows}`);
nodes["#tender-row-search"].value = "позиция 370";
vm.runInNewContext(`${source.slice(start, end)}; renderExcelTenderRows();`, context);
const filteredRows = (nodes["#tender-rows"].innerHTML.match(/<tr\b/g) || []).length;
if (filteredRows !== 1) throw new Error(`Expected one filtered row, got ${filteredRows}`);

function verifyTenderDefaultSelection() {
  const openStart = source.indexOf("function openExcelTenderWorkspace(workspace, {persist = true} = {}) {");
  const openEnd = source.indexOf("function renderExcelTenderLatestRun()", openStart);
  if (openStart < 0 || openEnd < 0) throw new Error("Tender workspace opener was not found");
  const selectors = ["#tender-document-name", "#tender-document-meta", "#tender-preview-pane", "#tender-workspace-pane", "#tender-workspace-summary", "#tender-row-search"];
  const elements = Object.fromEntries(selectors.map(selector => [selector, {value:"", textContent:"", hidden:false}]));
  const context = {
    state: {excelTender: {pollGeneration:0,selectedIds:new Set(),sourcingActive:false,latestRuns:[]}},
    $: selector => elements[selector],
    renderExcelTenderRows: () => {},
    setView: () => {},
    refreshExcelTenderRuns: () => Promise.resolve(),
  };
  const workspace = {tender_id:"tender",rows:[{source_row_id:"item-1",row_type:"item"},{source_row_id:"section-1",row_type:"section"},{source_row_id:"item-2",row_type:"item"}],counts:{item:2},filename:"synthetic.xlsx",source_sha256:"abc",sheet_name:"Sheet",header_row:12,logical_right_edge:5};
  vm.runInNewContext(`${source.slice(openStart, openEnd)}; openExcelTenderWorkspace(workspace,{persist:false});`, {...context, workspace});
  const selected = [...context.state.excelTender.selectedIds].sort().join(",");
  if (selected !== "item-1,item-2") throw new Error(`New tender should select all and only item rows: ${selected}`);
}
verifyTenderDefaultSelection();

async function verifyPollingLifecycle() {
  const pollStart = source.indexOf("async function waitForExcelTenderPreview(generation) {");
  const pollEnd = source.indexOf("async function refreshExcelTenderPreview", pollStart);
  if (pollStart < 0 || pollEnd < 0) throw new Error("Tender preview poller was not found");
  const pollSource = source.slice(pollStart, pollEnd);
  const delays = [];
  const terminal = [];
  const sequence = ["queued", "queued", "queued", "queued", "analyzing", "ready"];
  const pollContext = {
    state: {excelTender: {pollGeneration: 7, previewId: "preview-id"}},
    api: async () => ({status: sequence.shift()}),
    renderExcelTenderPreview: preview => terminal.push(preview.status),
    $: () => ({textContent: ""}),
    setTimeout: (callback, delay) => { delays.push(delay); callback(); },
    encodeURIComponent,
  };
  await vm.runInNewContext(`${pollSource}; waitForExcelTenderPreview(7);`, pollContext);
  if (delays.join(",") !== "2000,4000,8000,8000,1000") throw new Error(`Unexpected adaptive polling delays: ${delays}`);
  if (terminal.join(",") !== "ready") throw new Error(`Expected terminal ready render, got ${terminal}`);

  let staleRequests = 0;
  let staleWaits = 0;
  const staleContext = {
    state: {excelTender: {pollGeneration: 8, previewId: "preview-id"}},
    api: async () => {
      staleRequests += 1;
      staleContext.state.excelTender.pollGeneration += 1;
      return {status: "queued"};
    },
    renderExcelTenderPreview: () => { throw new Error("Stale preview must not render"); },
    $: () => ({textContent: ""}),
    setTimeout: callback => { staleWaits += 1; callback(); },
    encodeURIComponent,
  };
  await vm.runInNewContext(`${pollSource}; waitForExcelTenderPreview(8);`, staleContext);
  if (staleRequests !== 1 || staleWaits !== 0) throw new Error(`Stale poll continued: requests=${staleRequests}, waits=${staleWaits}`);
}

async function verifySourcingPollingLifecycle() {
  const pollStart = source.indexOf("async function pollExcelTenderJob(jobId, tenderId, generation, expectedTotal) {");
  const pollEnd = source.indexOf("async function startExcelTenderSourcing()", pollStart);
  if (pollStart < 0 || pollEnd < 0) throw new Error("Tender sourcing poller was not found");
  const pollSource = source.slice(pollStart, pollEnd);
  const delays = [];
  const statuses = ["queued", "queued", "queued", "running", "completed"];
  const state = {excelTender:{workspace:{tender_id:"tender"},pollGeneration:4,sourcingActive:true,jobId:"job"}};
  let rendered = 0;
  const pollContext = {
    state,
    JOB_RESTART_MESSAGE:"Задание было прервано перезапуском сервера. Запустите его повторно.",
    getJobForPolling:async () => ({status:statuses.shift(),current:1,total:2,result:{results:[]}}),
    $:() => ({textContent:"",innerHTML:""}),
    renderSourcingResult:() => { rendered += 1; },
    refreshExcelTenderRuns:() => Promise.resolve(),
    setTimeout:(callback,delay) => { delays.push(delay); callback(); return 0; },
  };
  await vm.runInNewContext(`${pollSource}; pollExcelTenderJob("job","tender",4,2);`, pollContext);
  if (delays.join(",") !== "2000,4000,8000,1000") throw new Error(`Unexpected sourcing poll delays: ${delays}`);
  if (rendered !== 1) throw new Error(`Expected shared sourcing result renderer once, got ${rendered}`);

  let requests = 0;
  let waits = 0;
  const staleContext = {
    ...pollContext,
    state:{excelTender:{workspace:{tender_id:"tender"},pollGeneration:7,sourcingActive:true,jobId:"job"}},
    getJobForPolling:async () => { requests += 1; staleContext.state.excelTender.pollGeneration += 1; return {status:"queued"}; },
    setTimeout:(callback) => { waits += 1; callback(); return 0; },
  };
  await vm.runInNewContext(`${pollSource}; pollExcelTenderJob("job","tender",7,2);`, staleContext);
  if (requests !== 1 || waits !== 0) throw new Error(`Stale sourcing poll continued: requests=${requests}, waits=${waits}`);

  const restartContext = {
    ...pollContext,
    state:{excelTender:{workspace:{tender_id:"tender"},pollGeneration:8,sourcingActive:true,jobId:"job"}},
    getJobForPolling:async () => { throw new Error("Задание было прервано перезапуском сервера. Запустите его повторно."); },
  };
  let restartMessage = "";
  try { await vm.runInNewContext(`${pollSource}; pollExcelTenderJob("job","tender",8,2);`, restartContext); }
  catch (error) { restartMessage = error.message; }
  if (restartMessage !== "Подбор был прерван перезапуском сервера. Запустите его повторно.") throw new Error(`Unexpected lost-job message: ${restartMessage}`);
}

Promise.all([verifyPollingLifecycle(), verifySourcingPollingLifecycle()]).then(() => process.stdout.write("PASS: 370-row workspace, default tender selection, and generation-safe adaptive sourcing polling\n"))
  .catch(error => { process.stderr.write(`${error.stack || error}\n`); process.exitCode = 1; });
