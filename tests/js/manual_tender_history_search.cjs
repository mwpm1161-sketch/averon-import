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

class Element {
  constructor(classNames = [], dataset = {}) {
    this.classNames = new Set(classNames);
    this.dataset = dataset;
    this.handlers = {};
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.classList = {contains:name => this.classNames.has(name)};
  }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  async fire(name, event = {}) { return this.handlers[name]?.(event); }
  async click() {
    if (this.disabled) return false;
    await this.fire("click", {currentTarget:this});
    return true;
  }
}

const form = new Element();
const input = new Element();
const assertion = new Element(["manual-history-assertion"], {variantId:"variant-1"});
const confirm = new Element(["manual-history-confirm"], {
  rowId:"row-1", historyItemId:"item-1", variantId:"variant-1", offerId:"one_c_history:item-1",
});
const confirmNext = new Element(["manual-history-confirm-and-next"], {
  rowId:"row-1", historyItemId:"item-1", variantId:"variant-1", offerId:"one_c_history:item-1",
});
confirm.disabled = true;
confirmNext.disabled = true;
const apiCalls = [];
const state = {
  sourcing: {
    modalContext:"excel_tender", result:null, manualHistorySearch:{
      sourceRowId:"row-1", open:true, query:"Клей для плитки СМ17", results:null, busy:false, error:"",
    },
    manualHistoryConfirmation:{sourceRowId:"row-1", historyItemId:"item-1", variantId:"variant-1"},
    reviewDetailNavigation:{sourceRowId:"row-1", requestPending:false},
  },
  excelTender:{workspace:{tender_id:"tender-1"}},
};
const row = {intent:{source_row_id:"row-1", normalized_name:"Клей для плитки СМ17", source_text:"Клей для плитки СМ17", unit:"кг", article:""}};
const projectResult = {run_id:"run-1", results:[row], historyDecisionRevision:0};
state.sourcing.result = projectResult;
let responseIndex = 0;
const snapshotError = new Error("История 1С была обновлена после этого подбора. Запустите подбор заново, чтобы искать и подтверждать записи в актуальной версии истории.");
snapshotError.code = "TENDER_HISTORY_SNAPSHOT_CHANGED";
const successfulResult = {
  history_item_id:"item-1", variant_id:"variant-1", title:"Клей д/плитки СМ 17",
  article:"", manufacturer:"", characteristic:"", price:"123.45", currency:"RUB",
  price_unit:"кг", purchase_date:"2025-04-16", match_decision:"LIKELY_MATCH",
  confirmable_for_manual_search:true, reason_code:null, reason:"", unit_compatible:true,
};
const context = {
  state,
  apiCalls,
  encodeURIComponent,
  escapeHtml:value => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;"),
  api:async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    apiCalls.push({url, method:options.method || "GET", body});
    if (url.endsWith("/history-search")) {
      responseIndex += 1;
      if (responseIndex === 1) throw snapshotError;
      return {snapshot_version:"snapshot-A", results:[successfulResult]};
    }
    return {decision_revision:1, rows:[], events:[], effective:{}};
  },
  $(selector) {
    if (selector === "#sourcing-content .manual-history-search-form") return form;
    if (selector === "#sourcing-content .manual-history-query") return input;
    if (selector === "#sourcing-modal") return {open:true};
    return null;
  },
  $$(selector) {
    if (selector.includes(".manual-history-assertion")) return [assertion];
    if (selector.includes(".manual-history-confirm")) return [confirm, confirmNext];
    return [];
  },
  toast:() => {},
  isExcelTenderProjectResult:value => value === projectResult,
  applyTenderHistoryDecisionSnapshot(result, snapshot) {
    result.historyDecisionRevision = snapshot.decision_revision;
    row.historyEffectiveDecision = {candidate_offer_id:"one_c_history:item-1"};
  },
  nextActionableHistoryReviewItem:() => null,
  hasActionableHistoryReviewItem:() => false,
  renderProjectItemDetails() { this.lastHtml = this.renderManualHistorySearch(row); },
};
context.renderProjectItemDetails = context.renderProjectItemDetails.bind(context);
vm.runInNewContext([
  sourceBetween("function renderManualHistorySearch(item)", "\nfunction bindSourcingFilters"),
  sourceBetween("function bindHumanHistoryDecisionActions", "\nfunction renderSourcingResult"),
  "globalThis.renderManualHistorySearch = renderManualHistorySearch; globalThis.bindHumanHistoryDecisionActions = bindHumanHistoryDecisionActions;",
].join("\n"), context);

(async () => {
  state.sourcing.manualHistorySearch.open = false;
  const closedMarkup = context.renderManualHistorySearch(row);
  assert(closedMarkup.includes("Найти другую запись в истории 1С"));
  state.sourcing.manualHistorySearch.open = true;
  const manualMarkup = context.renderManualHistorySearch(row);
  assert(manualMarkup.includes("Скрыть ручной поиск"));
  assert(manualMarkup.includes("class=\"manual-history-search-form\""));
  assert(manualMarkup.includes("Нужной записи нет?"));

  context.bindHumanHistoryDecisionActions(projectResult, row);
  input.value = "СМ17";
  await input.fire("input", {target:input});
  assert.equal(apiCalls.length, 0, "typing a query must not issue a network request");
  await form.fire("submit", {preventDefault() {}});
  assert.equal(apiCalls.length, 1, "one explicit submit produces one local history request");
  assert.equal(apiCalls[0].method, "POST");
  assert.deepEqual(Object.keys(apiCalls[0].body).sort(), ["limit", "query", "source_row_id"]);
  assert.equal(apiCalls[0].body.query, "СМ17");
  assert(context.lastHtml.includes(snapshotError.message), "snapshot change is presented in the search UI");

  await form.fire("submit", {preventDefault() {}});
  assert.equal(apiCalls.length, 2, "a second explicit submit adds exactly one request");
  assert.equal(state.sourcing.manualHistorySearch.results.length, 1);
  const blockedResult = {...successfulResult, history_item_id:"item-2", variant_id:"variant-2", title:"Другая запись", confirmable_for_manual_search:false, reason:"Единица измерения несовместима."};
  state.sourcing.manualHistorySearch.results.push(blockedResult);
  state.sourcing.manualHistoryConfirmation = null;
  let markup = context.renderManualHistorySearch(row);
  assert(markup.includes("Найдено вручную в истории 1С"));
  assert(markup.includes("Сравнить и подтвердить"));
  assert(markup.includes("Единица измерения несовместима."), "blocked candidates explain why they cannot be confirmed");
  state.sourcing.manualHistorySearch.results = [];
  assert(context.renderManualHistorySearch(row).includes("В истории 1С ничего не найдено по этому запросу"));
  state.sourcing.manualHistorySearch.results = [successfulResult, blockedResult];

  state.sourcing.manualHistoryConfirmation = {sourceRowId:"row-1", historyItemId:"item-1", variantId:"variant-1"};
  markup = context.renderManualHistorySearch(row);
  assert(markup.includes("История 1С"));
  assert(markup.includes("Я подтверждаю, что это одна и та же позиция"));
  assert.match(markup, /manual-history-confirm[^>]*disabled/);
  assert.match(markup, /manual-history-confirm-and-next[^>]*disabled/);
  assert.equal(await confirm.click(), false, "confirmation is unavailable before the assertion checkbox");

  assertion.checked = true;
  await assertion.fire("change");
  assert.equal(confirm.disabled, false);
  assert.equal(confirmNext.disabled, false);
  await confirmNext.click();
  const confirmation = apiCalls.at(-1);
  assert.equal(confirmation.method, "POST");
  assert.deepEqual(confirmation.body, {
    decision:"CONFIRM_HISTORY_CANDIDATE", source_row_id:"row-1", expected_revision:0,
    confirmation_mode:"EXPLICIT_MANUAL_HISTORY_SEARCH", explicit_identity_assertion:true,
    manual_history_ref:{history_item_id:"item-1", variant_id:"variant-1"},
  }, "final request contains stable IDs and assertion only, not commercial fields");
  console.log("PASS: manual history search is explicit, bounded, two-step, and snapshot-change aware");
})().catch(error => { console.error(error); process.exitCode = 1; });
