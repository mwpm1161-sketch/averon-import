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

class ListNode {
  constructor() { this._html = ""; this.buttons = new Map(); }
  set innerHTML(value) { this._html = String(value); this.buttons = new Map(); }
  get innerHTML() { return this._html; }
  replaceChildren() { this.innerHTML = ""; }
  querySelectorAll(selector) {
    if (this.buttons.has(selector)) return this.buttons.get(selector);
    const className = selector.slice(1);
    const selected = [];
    for (const match of this._html.matchAll(/<button\b([^>]*)>/g)) {
      const attrs = match[1];
      const classes = attrs.match(/class="([^"]*)"/)?.[1] || "";
      if (!classes.split(/\s+/).includes(className)) continue;
      const button = {
        dataset:{
          tenderId:attrs.match(/data-tender-id="([^"]*)"/)?.[1] || "",
          tenderName:attrs.match(/data-tender-name="([^"]*)"/)?.[1] || "",
        },
        listeners:{},
        addEventListener(name, callback) { this.listeners[name] = callback; },
      };
      selected.push(button);
    }
    this.buttons.set(selector,selected);
    return selected;
  }
}

const nodes = new Map([
  ["#recent-tenders-panel", {hidden:true}],
  ["#recent-tenders-list", new ListNode()],
  ["#recent-tenders-count", {textContent:""}],
  ["#recent-documents-panel", {hidden:false}],
  ["#recent-documents-list", new ListNode()],
]);
const summaries = [
  {tender_id:"a".repeat(32),filename:"Смета.xlsx",sheet_name:"Ресурсы",item_count:30,last_access_at:"2026-10-02T10:00:00+00:00"},
  {tender_id:"b".repeat(32),filename:"Тендер B.xlsx",sheet_name:"Лист 1",item_count:12,last_access_at:"2026-10-01T10:00:00+00:00"},
  {tender_id:"c".repeat(32),filename:"Тендер C.xlsx",sheet_name:"Лист 1",item_count:4,last_access_at:"2026-09-30T10:00:00+00:00"},
];
let collection = {tenders:[summaries[0]],active_count:1,limit:3};
let quotaError = false;
let busyTenderId = null;
const apiCalls = [];
const confirmations = [];
const toasts = [];
const views = [];
const state = {
  authState:"authenticated", authGeneration:1, currentUser:{username:"user-a",capabilities:{}},
  recentExcelTenders:[],recentExcelTendersActiveCount:0,recentExcelTendersLimit:null,
  recentExcelTendersError:null,recentExcelTendersRequest:0,
  recentDocuments:[{document_id:"pdf-1",filename:"Existing PDF.pdf"}],
  manual:{active:true,rows:[]},
  excelTender:{previewId:"preview-1",workspace:null},
};
const context = {
  state,
  $:selector => nodes.get(selector),
  api:async (url, options = {}) => {
    const method = String(options.method || "GET").toUpperCase();
    apiCalls.push({url,method});
    if (url === "/api/manual-tenders?limit=10" && method === "GET") return collection;
    if (url.startsWith("/api/manual-tenders/") && method === "GET") {
      return {tender_id:url.split("/").at(-1),filename:"Loaded workspace.xlsx",rows:[{row_type:"item"}],counts:{item:30}};
    }
    if (url.startsWith("/api/manual-tenders/") && method === "DELETE") {
      const tenderId = url.split("/").at(-1);
      if (tenderId === busyTenderId) {
        const error = new Error("busy"); error.code = "TENDER_WORKSPACE_BUSY"; throw error;
      }
      collection = {...collection,tenders:collection.tenders.filter(item => item.tender_id !== tenderId),active_count:Math.max(0,collection.active_count - 1)};
      return {deleted:true};
    }
    if (url.startsWith("/api/manual-tenders/previews/") && method === "POST") {
      if (quotaError) {
        const error = new Error("Достигнут лимит активных тендеров."); error.code = "TENDER_WORKSPACE_QUOTA"; throw error;
      }
      return {tender_id:"new-tender"};
    }
    if (url === "/api/documents?limit=50") return {documents:state.recentDocuments};
    throw new Error(`Unexpected request: ${method} ${url}`);
  },
  escapeHtml:value => String(value).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;"),
  formatRecentTimestamp:value => value ? "02.10.2026, 13:00" : "Дата неизвестна",
  openExcelTenderWorkspace(workspace) { context.openedWorkspace = workspace; },
  clearExcelTenderState() { state.excelTender.workspace = null; context.clearedTender = true; },
  setView(view) { views.push(view); },
  saveManualDraft() {},
  toast(message, kind) { toasts.push({message,kind}); },
  confirm(message) { confirmations.push(message); return true; },
  encodeURIComponent,
  sessionStorage:{setItem() { throw new Error("Tender summaries must not be written to sessionStorage"); }},
};

const code = [
  sliceBetween("async function confirmExcelTenderImport() {", "function openExcelTenderWorkspace"),
  sliceBetween("function returnToStartFromManual() {", "function clearExcelTenderState"),
  sliceBetween("async function deleteExcelTenderWorkspace() {", "async function pollExcelTenderJob"),
  sliceBetween("async function loadRecentDocuments() {", "async function loadRecentExcelTenders"),
  sliceBetween("async function loadRecentExcelTenders() {", "function removeRecentExcelTenderFromState"),
  sliceBetween("function removeRecentExcelTenderFromState", "function recentTenderErrorMessage"),
  sliceBetween("function recentTenderErrorMessage", "function renderRecentExcelTenders"),
  sliceBetween("function renderRecentExcelTenders() {", "async function openRecentExcelTender"),
  sliceBetween("async function openRecentExcelTender", "async function deleteRecentExcelTender"),
  sliceBetween("async function deleteRecentExcelTender", "function returnToStartFromExcelTender"),
  sliceBetween("function returnToStartFromExcelTender() {", "function formatRecentTimestamp"),
  sliceBetween("function formatRecentTimestamp(value) {", "function renderRecentDocuments"),
  sliceBetween("function renderRecentDocuments() {", "function openDeleteDocumentDialog"),
].join("\n");
vm.runInNewContext(`${code}; globalThis.loadRecent=loadRecentExcelTenders; globalThis.openRecent=openRecentExcelTender; globalThis.deleteRecent=deleteRecentExcelTender; globalThis.deleteCurrent=deleteExcelTenderWorkspace; globalThis.confirmImport=confirmExcelTenderImport; globalThis.renderRecent=renderRecentExcelTenders; globalThis.returnToStart=returnToStartFromExcelTender; globalThis.returnToManualStart=returnToStartFromManual; globalThis.loadPdfs=loadRecentDocuments;`, context);

(async function main() {
  const list = nodes.get("#recent-tenders-list");
  const panel = nodes.get("#recent-tenders-panel");
  const count = nodes.get("#recent-tenders-count");

  // A fresh browser state obtains durable summaries from the authenticated server collection.
  await context.loadRecent();
  assert.equal(apiCalls[0].url,"/api/manual-tenders?limit=10");
  assert.equal(panel.hidden,false);
  assert.equal(count.textContent,"Активные тендеры: 1 из 3");
  assert(list.innerHTML.includes("Смета.xlsx"));
  assert(list.innerHTML.includes("30 позиций"));
  assert.equal(apiCalls.filter(call => call.url.startsWith("/api/manual-tenders/") && call.method === "GET").length,0,"cards do not eagerly fetch workspace rows");

  // Empty and full quota states show the count and friendly empty state.
  collection = {tenders:[],active_count:0,limit:3};
  await context.loadRecent();
  assert.equal(panel.hidden,false);
  assert(list.innerHTML.includes("Сохранённых тендеров пока нет."));
  assert.equal(count.textContent,"Активные тендеры: 0 из 3");
  collection = {tenders:summaries,active_count:3,limit:3};
  await context.loadRecent();
  assert.equal(count.textContent,"Активные тендеры: 3 из 3");
  assert.equal((list.innerHTML.match(/class="recent-tender-item"/g) || []).length,3);

  // Open fetches only the selected full workspace and delegates to the existing initializer.
  const chosen = list.querySelectorAll(".recent-tender-open").find(button => button.dataset.tenderId === summaries[1].tender_id);
  assert(chosen);
  chosen.listeners.click();
  await new Promise(resolve => setImmediate(resolve));
  assert(apiCalls.some(call => call.url === `/api/manual-tenders/${summaries[1].tender_id}`));
  assert.equal(context.openedWorkspace.tender_id,summaries[1].tender_id);
  assert(Array.isArray(context.openedWorkspace.rows));

  // Delete confirms the selected row, calls its owner-bound ID route, then refreshes count and cards.
  state.excelTender.workspace = {tender_id:summaries[0].tender_id};
  await context.deleteRecent(summaries[0].tender_id,summaries[0].filename);
  assert.match(confirmations.at(-1),/Исходный Excel, запуски подбора, подтверждения и готовые экспорты/);
  assert(apiCalls.some(call => call.url === `/api/manual-tenders/${summaries[0].tender_id}` && call.method === "DELETE"));
  assert(context.clearedTender,"deleting the cached workspace from Start clears its protected state");
  assert.equal(state.excelTender.workspace,null);
  assert.equal(state.recentExcelTendersActiveCount,2);
  assert(!list.innerHTML.includes(summaries[0].tender_id));
  assert.equal(count.textContent,"Активные тендеры: 2 из 3");

  // Busy delete is reported safely and never removes the row.
  busyTenderId = summaries[1].tender_id;
  await context.deleteRecent(summaries[1].tender_id,summaries[1].filename);
  assert(toasts.at(-1).message === "Тендер сейчас используется. Повторите удаление после завершения операции.");
  assert(collection.tenders.some(item => item.tender_id === busyTenderId));

  // Confirmation quota refreshes and exposes the server list without auto-eviction.
  collection = {tenders:summaries,active_count:3,limit:3};
  quotaError = true;
  await context.confirmImport();
  assert.equal(views.at(-1),"upload");
  assert.equal(count.textContent,"Активные тендеры: 3 из 3");
  assert(toasts.at(-1).message.includes("лимит активных тендеров (3 из 3)"));
  assert(!apiCalls.some(call => call.method === "DELETE" && call.url.includes("/" + summaries[2].tender_id)));

  // Existing in-workspace deletion keeps the same endpoint, clears state, returns to Start, and refreshes summaries.
  quotaError = false;
  state.excelTender.workspace = {tender_id:summaries[2].tender_id};
  collection = {tenders:[summaries[2]],active_count:1,limit:3};
  await context.deleteCurrent();
  assert(context.clearedTender);
  assert.equal(views.at(-1),"upload");
  assert(!collection.tenders.length);
  assert.equal(count.textContent,"Активные тендеры: 0 из 3");

  // Returning to Start refreshes once. PDF recents still use their existing endpoint and state.
  const beforeReturn = apiCalls.filter(call => call.url === "/api/manual-tenders?limit=10").length;
  context.returnToStart();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(apiCalls.filter(call => call.url === "/api/manual-tenders?limit=10").length,beforeReturn + 1);
  const beforeManualReturn = apiCalls.filter(call => call.url === "/api/manual-tenders?limit=10").length;
  context.returnToManualStart();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(apiCalls.filter(call => call.url === "/api/manual-tenders?limit=10").length,beforeManualReturn + 1);
  assert.equal(state.manual.active,false);
  assert.equal(views.at(-1),"upload");
  await context.loadPdfs();
  assert.equal(apiCalls.at(-1).url,"/api/documents?limit=50");
  assert.equal(state.recentDocuments[0].document_id,"pdf-1");
  assert(source.includes('try { await loadRecentExcelTenders(); } catch (_) { renderRecentExcelTenders(); }'),"authenticated boot loads server-owned tender summaries");
  assert(source.includes('if (recentTendersList) recentTendersList.replaceChildren();'),"switching or clearing a protected session scrubs old tender summaries from the DOM");
  assert(source.includes('const payload = await api("/api/manual-tenders?limit=10");'));
  assert(source.includes('const payload = await api("/api/documents?limit=50");'),"PDF recent documents retain their existing API");
  assert(!code.includes("sessionStorage"),"recent tender summaries are not stored in browser session state");
  process.stdout.write("PASS: recent tender reload, quota counts, open/delete, busy/quota UX, current deletion, and PDF recents isolation\n");
})().catch(error => { process.stderr.write(`${error.stack || error}\n`); process.exitCode = 1; });
