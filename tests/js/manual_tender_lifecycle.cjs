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

verifyPollingLifecycle().then(() => process.stdout.write("PASS: 370-row workspace and generation-safe adaptive tender polling\n"))
  .catch(error => { process.stderr.write(`${error.stack || error}\n`); process.exitCode = 1; });
