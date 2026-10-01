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
process.stdout.write("PASS: 370-row read-only tender workspace\n");
