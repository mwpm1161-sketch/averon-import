const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const appPath = process.argv[2];
const app = fs.readFileSync(appPath, "utf8");
const guardStart = app.indexOf("function isCurrentOneCHistoryRequest(");
const guardEnd = app.indexOf("function cancelDocumentNavigation(", guardStart);
const fieldsStart = app.indexOf("const ONE_C_HISTORY_FIELDS =");
const fieldsEnd = app.indexOf("function setOneCHistoryMessage(", fieldsStart);
const lifecycleStart = app.indexOf("async function loadOneCHistoryStatus(");
const lifecycleEnd = app.indexOf("function updateSourcingProviderFields(", lifecycleStart);
assert(guardStart >= 0 && guardEnd > guardStart, "request guard source was not found");
assert(fieldsStart >= 0 && fieldsEnd > fieldsStart, "1C import field contract was not found");
assert(lifecycleStart >= 0 && lifecycleEnd > lifecycleStart, "1C lifecycle source was not found");

const status = { active_import: null, profiles: [], activity: { replacement_allowed: true } };
const user = { role: "USER", capabilities: { settings: false, one_c_history_import: true } };
const state = {
  authState: "authenticated",
  currentUser: user,
  oneCHistory: { status: null, preview: null, analysisReady: false, busy: false, requestGeneration: 7 },
};
const calls = [];
const responses = [];
const renderEvents = [];
const nodes = new Map();
const sourcingStatusRefreshes = [];

function node(selector) {
  if (!nodes.has(selector)) {
    nodes.set(selector, {
      value: selector === "#one-c-history-sheet" ? "Purchases" : "",
      files: selector === "#one-c-history-file" ? [{ name: "report.xlsx" }] : [],
      checked: false,
      hidden: false,
      disabled: false,
      textContent: "",
      className: "",
      options: [],
      children: [],
      replaceChildren(...children) { this.children = children; this.options = []; },
      append(...children) { this.children.push(...children); },
      add(option) { this.options.push(option); },
    });
  }
  return nodes.get(selector);
}

class FakeFormData {
  constructor() { this.values = []; }
  append(...value) { this.values.push(value); }
}

class FakeOption {
  constructor(text, value) { this.textContent = text; this.value = value; }
}

function mappingSelects(selector) {
  const containerSelector = selector.startsWith("#one-c-history-group-mappings")
    ? "#one-c-history-group-mappings" : "#one-c-history-mappings";
  return node(containerSelector).children.map((wrapper) => wrapper.children[1]);
}

async function api(path, options) {
  calls.push({ path, options });
  assert(responses.length, `unexpected API call: ${path}`);
  const response = responses.shift();
  if (response instanceof Error) throw response;
  return typeof response === "function" ? response(path, options) : response;
}

const context = vm.createContext({
  state,
  api,
  $: node,
  $$: mappingSelects,
  document: { createElement: (tagName) => ({ tagName, dataset: {}, children: [], options: [], append(...children) { this.children.push(...children); }, add(option) { this.options.push(option); } }) },
  Option: FakeOption,
  FormData: FakeFormData,
  URLSearchParams,
  encodeURIComponent,
  window: { confirm: () => true },
  toast: (...args) => renderEvents.push(["toast", ...args]),
  setOneCHistoryMessage: (...args) => renderEvents.push(["message", ...args]),
  formatSourcingHistoryDate: (value) => value || "",
  loadSourcingHistoryStatus: async () => { sourcingStatusRefreshes.push(true); },
});

vm.runInContext(
  app.slice(fieldsStart, fieldsEnd) + "\n" + app.slice(guardStart, guardEnd) + "\n" + app.slice(lifecycleStart, lifecycleEnd),
  context,
  { filename: "app.js:one-c-history-lifecycle" },
);

context.renderOneCHistoryStatus = () => renderEvents.push(["status-render"]);
context.renderOneCHistoryPreview = (preview) => {
  state.oneCHistory.preview = preview;
  state.oneCHistory.analysisReady = !preview.mapping_required;
  node("#one-c-history-preview-panel").hidden = false;
  context.renderOneCHistoryMapping(preview.group_headers || preview.headers, preview.group_field_mapping || {}, "#one-c-history-group-mappings");
  context.renderOneCHistoryMapping(preview.headers, preview.event_field_mapping || preview.field_mapping || {});
  renderEvents.push(["preview-render", preview.preview_id]);
};
context.renderOneCHistorySummary = () => {};

const preview = {
  preview_id: "user-preview-1", mapping_required: true, sheet_name: "Purchases",
  sheet_names: ["Purchases"], header_row: 4, group_header_row: 3,
  event_header_row: 4, layout_type: "hierarchical_grouped", field_mapping: {},
  headers: ["Name", "Склад", "Склад"], group_headers: ["Code", "Name"],
  group_field_mapping: { item_code: 0, item_name: 1 }, event_field_mapping: { item_name: null, warehouse: null },
  summary: {}, sample: [],
};
const nextPreview = {
  ...preview, preview_id: "user-preview-2", mapping_required: false,
  field_mapping: { item_name: 0, warehouse: 2 },
  event_field_mapping: { item_name: 0, warehouse: 2 },
};

async function main() {
  assert.equal(user.capabilities.settings, false);
  assert.equal(user.capabilities.one_c_history_import, true);

  responses.push({ ...status, revision: "status-for-user" });
  await context.loadOneCHistoryStatus();
  assert.equal(state.oneCHistory.status.revision, "status-for-user", "USER status response must be accepted");

  responses.push(preview);
  await context.startOneCHistoryPreview();
  assert.equal(state.oneCHistory.preview.preview_id, preview.preview_id, "USER preview must be accepted and rendered");
  assert(renderEvents.some((event) => event[0] === "preview-render" && event[1] === preview.preview_id));
  assert.equal(state.oneCHistory.busy, false, "preview completion must clear busy");
  const eventFields = node("#one-c-history-mappings").children;
  const warehouseControl = eventFields.map((wrapper) => wrapper.children[1])
    .find((select) => select.dataset.oneCHistoryField === "warehouse");
  assert(warehouseControl, "ambiguous event warehouse must have a manual mapping selector");
  assert.deepEqual(warehouseControl.options.slice(1).map((option) => option.textContent), ["1. Name", "2. Склад", "3. Склад"]);
  assert(!node("#one-c-history-group-mappings").children.some((wrapper) => wrapper.children[1].dataset.oneCHistoryField === "warehouse"),
    "warehouse must never appear as a group-level mapping");

  responses.push({
    sheet_name: "Purchases", header_row: 4, group_header_row: 3,
    event_header_row: 4, headers: ["Name", "Склад", "Склад"], field_mapping: { item_name: null },
    group_headers: ["Code", "Name"], group_field_mapping: { item_code: 0, item_name: 1 },
    event_field_mapping: { item_name: null, warehouse: null },
    layout_type: "hierarchical_grouped",
  });
  await context.inspectOneCHistorySheet();
  assert.equal(state.oneCHistory.preview.sheet_name, "Purchases", "USER inspect response must be accepted");
  const inspectedWarehouse = node("#one-c-history-mappings").children.map((wrapper) => wrapper.children[1])
    .find((select) => select.dataset.oneCHistoryField === "warehouse");
  inspectedWarehouse.value = "2"; // USER chooses the second duplicate warehouse column.
  assert.equal(context.oneCHistoryMappingPayload().warehouse, 2);
  assert(!Object.hasOwn(context.oneCHistoryMappingPayload("#one-c-history-group-mappings"), "warehouse"));

  responses.push(nextPreview);
  await context.analyzeOneCHistoryMapping();
  assert.equal(state.oneCHistory.analysisReady, true, "USER analyze response must be accepted");
  assert.equal(state.oneCHistory.busy, false, "analysis completion must clear busy");
  const analyzeCall = calls.find((call) => call.path.endsWith("/mapping"));
  const analyzedMapping = JSON.parse(analyzeCall.options.body);
  assert.equal(analyzedMapping.event_field_mapping.warehouse, 2, "manual warehouse selection is sent to analysis");
  assert(!Object.hasOwn(analyzedMapping.group_field_mapping, "warehouse"));

  let completeImport;
  responses.push(() => new Promise((resolve) => { completeImport = resolve; }));
  responses.push({ ...status, revision: "after-import" });
  const importPromise = context.importOneCHistory();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(state.oneCHistory.preview.preview_id, nextPreview.preview_id, "preview remains until terminal import success");
  assert.equal(state.oneCHistory.busy, true);
  completeImport({ status: "succeeded", idempotent: false });
  await importPromise;
  const importCall = calls.find((call) => call.path === "/api/one-c-history/imports");
  const importedMapping = JSON.parse(importCall.options.body);
  assert.equal(importedMapping.event_field_mapping.warehouse, 2, "manual warehouse selection is sent to import");
  assert(!Object.hasOwn(importedMapping.group_field_mapping, "warehouse"));
  assert.equal(importedMapping.save_profile, false, "USER cannot save import profiles");
  assert.equal(state.oneCHistory.preview, null, "successful import consumes the preview");
  assert.equal(state.oneCHistory.busy, false);
  assert.equal(state.oneCHistory.status.revision, "after-import", "success refreshes history status");
  assert.equal(sourcingStatusRefreshes.length, 1, "success refreshes sourcing status");

  state.oneCHistory.preview = { ...nextPreview, preview_id: "retry-preview" };
  state.oneCHistory.analysisReady = true;
  state.oneCHistory.status = status;
  const busy = new Error("История 1С сейчас используется в подборе.");
  busy.code = "ONE_C_HISTORY_IN_USE";
  responses.push(busy);
  responses.push({ ...status, revision: "after-busy" });
  await context.importOneCHistory();
  assert.equal(state.oneCHistory.preview.preview_id, "retry-preview", "busy 409 must preserve the prepared preview");
  assert.equal(state.oneCHistory.analysisReady, true, "busy 409 must preserve analysis");
  assert.equal(state.oneCHistory.busy, false, "busy 409 must clear busy");
  assert.equal(node("#one-c-history-import").disabled, false, "retry remains enabled after status refresh");
  assert.equal(state.oneCHistory.status.revision, "after-busy");

  state.currentUser = { role: "USER", capabilities: { settings: false, one_c_history_import: false } };
  state.oneCHistory.status = null;
  responses.push({ ...status, revision: "must-be-discarded" });
  await context.loadOneCHistoryStatus();
  assert.equal(state.oneCHistory.status, null, "a user without the capability must not accept the response");

  state.currentUser = { role: "ADMIN", capabilities: { settings: true, one_c_history_import: true } };
  responses.push({ ...status, revision: "admin-status" });
  await context.loadOneCHistoryStatus();
  assert.equal(state.oneCHistory.status.revision, "admin-status", "ADMIN lifecycle remains admitted");

  assert(calls.some((call) => call.path === "/api/one-c-history/previews"));
  assert(calls.some((call) => call.path.includes("/sheets/Purchases")));
  assert(calls.some((call) => call.path.endsWith("/mapping")));
  assert(calls.some((call) => call.path === "/api/one-c-history/imports"));
  assert.equal(responses.length, 0, "all planned responses should be consumed");
  console.log("PASS: capability-scoped 1C request lifecycle");
}

main().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
