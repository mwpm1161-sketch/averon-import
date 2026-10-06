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

const navHelpers = sourceBetween("function isExcelTenderProjectResult", "function renderHumanHistoryDecisionAction");
const detailBlock = sourceBetween("function renderProjectItemDetails", "function bindSourcingFilters");
const decisionUI = sourceBetween("function renderHumanHistoryDecisionAction", "function renderSourcingResult");
const decisionSnapshot = sourceBetween("function applyTenderHistoryDecisionSnapshot", "async function openExcelTenderRunDetail");
const projectDecision = sourceBetween("function projectDecision(item)", "function projectReason");

function makeButton(attributes, label) {
  const classNames = new Set((attributes.match(/\bclass="([^"]*)"/)?.[1] || "").split(/\s+/).filter(Boolean));
  const dataset = {};
  for (const match of attributes.matchAll(/\bdata-([a-z0-9-]+)="([^"]*)"/g)) {
    const key = match[1].replace(/-([a-z])/g, (_all, letter) => letter.toUpperCase());
    dataset[key] = match[2];
  }
  const button = {
    classNames, dataset, textContent: label, disabled: /\sdisabled(?:\s|$)/.test(attributes), handlers: {},
    classList: {contains: name => classNames.has(name)},
    addEventListener(name, handler) { this.handlers[name] = handler; },
    async click() {
      if (this.disabled || !this.handlers.click) return false;
      await this.handlers.click({currentTarget:this});
      return true;
    },
  };
  return button;
}

const content = {
  _html: "", buttons: [],
  get innerHTML() { return this._html; },
  set innerHTML(html) {
    this._html = html;
    this.buttons = [];
    for (const match of html.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)) {
      this.buttons.push(makeButton(match[1], match[2].replace(/<[^>]*>/g, "")));
    }
  },
  querySelectorAll(selector) {
    const groups = selector.split(",").map(group => [...group.matchAll(/\.([\w-]+)/g)].map(match => match[1])).filter(group => group.length);
    return this.buttons.filter(button => groups.some(group => group.every(name => button.classNames.has(name))));
  },
};
const subtitle = {textContent:""};
const modal = {open:true};
const state = {
  sourcing: {
    modalPhase:"project_result", modalContext:"excel_tender", projectFilter:"review",
    reviewDetailNavigation:{sourceRowId:null,showCompletion:false,requestPending:false}, result:null,
  },
  excelTender:{workspace:{tender_id:"tender-1"}},
};
const bySelector = {"#sourcing-content":content,"#sourcing-subtitle":subtitle,"#sourcing-modal":modal};
const context = {
  state,
  $: selector => bySelector[selector] || content.buttons.find(button => button.classNames.has(selector.replace(/^\./,""))) || null,
  $$: selector => content.querySelectorAll(selector),
  escapeHtml: value => String(value ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;"),
  encodeURIComponent,
  setSourcingModalPhase: phase => { state.sourcing.modalPhase = phase; },
  projectMatch: item => item.review_candidate || null,
  projectReviewCandidates: item => item.reviewCandidates || [],
  historyReviewPresentation: () => ({title:"Проверка истории 1С",explanation:"Требуется проверка."}),
  renderHistoricalOfferCard: candidate => `<article>${candidate.offer.title}</article>`,
  historicalOfferDate: () => "2026-09-20",
  renderOfferCard: () => "<article>Предложение</article>",
  renderProductUnderstanding: () => "",
  renderSourcingRouteExplanation: () => "",
  toastMessages:[],
  toast(message) { context.toastMessages.push(message); },
  apiCalls:[],
  staleNextPost:false,
  deferNextPost:false,
  pendingResolve:null,
  effective:new Map(),
  revision:0,
  decisionSerial:0,
  renderSourcingResult(result) { this.backResult = result; },
};
context.renderSourcingResult = resultValue => { context.backResult = resultValue; };
const item = (sourceRowId, routeKind, candidates = [], {decision="REVIEW", effective=null} = {}) => {
  const reviewCandidates = candidates.map(candidate => ({offer:candidate.offer,decision:"REVIEW",rank:1}));
  return {
    intent:{source_row_id:sourceRowId,normalized_name:sourceRowId,source_text:sourceRowId},
    route:{final_source_kind:routeKind,source_mode:"one_c_only",history_outcome:routeKind === "history_review" ? "REVIEW" : null},
    review_candidate:decision === "REVIEW" ? {decision:"REVIEW",offer:null} : {decision,offer:null},
    reviewCandidates, historyDecisionCandidates:candidates, historyEffectiveDecision:effective,
    offers:reviewCandidates.map(entry => entry.offer), match_results:reviewCandidates,
  };
};
const candidate = (offerId, {confirmable=true,reason_code=null} = {}) => ({
  candidate_offer_id:offerId, confirmable, reason_code,
  offer:{offer_id:offerId,title:offerId,price:"12.50",currency:"RUB",price_unit:"шт",history_retrieval_classification:"EXACT_NAME_UNIT"},
});
const safe = item("safe-row","historical_purchase",[],{decision:"HISTORY_SAFE_MATCH"});
const first = item("row-1","history_review",[candidate("offer-1")]);
const genericReview = item("row-2","provider",[],{decision:"REVIEW"});
const fuzzy = item("row-3","history_review",[candidate("fuzzy-3",{confirmable:false,reason_code:"HISTORY_CANDIDATE_SOURCE_CONFLICT"})]);
fuzzy.reviewCandidates[0].offer.history_retrieval_classification = "FUZZY";
const confirmed = item("row-4","history_review",[candidate("offer-4")],{effective:{decision_id:"decision-4",candidate_offer_id:"offer-4"}});
confirmed.historyEffectiveDecision = confirmed.historyDecisionCandidates[0].decision = confirmed.route.effective_decision = {decision_id:"decision-4",candidate_offer_id:"offer-4"};
const ambiguous = item("row-5","history_review",[candidate("offer-5-a"),candidate("offer-5-b")]);
const last = item("row-6","history_review",[candidate("offer-6")]);
const result = {run_id:"run-1",source_mode:"one_c_only",results:[safe,first,genericReview,fuzzy,confirmed,ambiguous,last],historyDecisionRevision:0,human_confirmed_count:1};
context.effective.set("row-4",{decision_id:"decision-4",candidate_offer_id:"offer-4"});
state.sourcing.result = result;

function currentSnapshot() {
  const effective = Object.fromEntries(context.effective.entries());
  return {
    decision_revision:context.revision,decision_digest:String(context.revision).padStart(64,"d"),effective,
    rows:result.results.map(row => ({
      source_row_id:row.intent.source_row_id,
      candidates:row.historyDecisionCandidates,
      effective_decision:effective[row.intent.source_row_id] || null,
    })),
  };
}
function applyDecision(body) {
  context.revision += 1;
  if (body.decision === "REVOKE_HISTORY_CONFIRMATION") {
    const entry = [...context.effective.entries()].find(([, decision]) => decision.decision_id === body.decision_id);
    if (entry) context.effective.delete(entry[0]);
  } else {
    context.decisionSerial += 1;
    context.effective.set(body.source_row_id,{decision_id:`decision-${context.decisionSerial}`,candidate_offer_id:body.candidate_offer_id});
  }
  return currentSnapshot();
}
context.api = async (url, options = {}) => {
  context.apiCalls.push({url,method:options.method || "GET",body:options.body ? JSON.parse(options.body) : null});
  if ((options.method || "GET") === "GET") return currentSnapshot();
  if (context.staleNextPost) {
    context.staleNextPost = false;
    const error = new Error("Stale decision revision"); error.code = "TENDER_HISTORY_DECISIONS_STALE"; throw error;
  }
  const body = JSON.parse(options.body);
  if (body.decision === "REVOKE_HISTORY_CONFIRMATION") body.decision_id = decodeURIComponent(url.split("/").at(-2));
  if (context.deferNextPost) {
    context.deferNextPost = false;
    return new Promise(resolve => { context.pendingResolve = () => resolve(applyDecision(body)); });
  }
  return applyDecision(body);
};

const snippet = [
  projectDecision,
  detailBlock,
  navHelpers,
  decisionUI,
  decisionSnapshot,
].join("\n");
vm.runInNewContext(`${snippet}; globalThis.sequence=projectReviewSequence; globalThis.position=projectReviewPosition; globalThis.adjacent=nextProjectReviewItem; globalThis.actionable=nextActionableHistoryReviewItem; globalThis.detail=renderProjectItemDetails;`, context);

(async function main() {
const ids = context.sequence(result).map(entry => entry.sourceRowId);
assert.deepEqual(Array.from(ids),["row-1","row-2","row-3","row-4","row-5","row-6"],"review sequence keeps project order and excludes automatic SAFE rows");
assert.equal(context.position(result,"row-5"),4,"position is resolved by source_row_id");
assert.equal(context.adjacent(result,"row-1",-1),null,"previous does not wrap at the first row");
assert.equal(context.adjacent(result,"row-6",1),null,"next does not wrap at the last row");

const getButton = className => content.buttons.find(button => button.classNames.has(className));
const currentRowId = () => state.sourcing.reviewDetailNavigation.sourceRowId;
const openRow = sourceRowId => context.detail(result,result.results.find(row => row.intent.source_row_id === sourceRowId));
const click = async className => {
  const button = getButton(className);
  assert(button,`button ${className} should be present`);
  await button.click();
  return button;
};
openRow("row-1");
assert(content.innerHTML.includes("Проверка позиции 1 из 6"));
assert(!content.innerHTML.includes("Запустить подбор по выбранным строкам"),"project launch CTA must not appear in detail");
assert.equal(getButton("project-review-previous").disabled,true);
assert.equal(getButton("project-review-next").disabled,false);
const callCountBeforeNavigation = context.apiCalls.length;
await click("project-review-next");
assert.equal(currentRowId(),"row-2","next follows source order");
assert(content.innerHTML.includes("Проверка позиции 2 из 6"));
await click("project-review-previous");
assert.equal(currentRowId(),"row-1","previous returns to prior review row");
assert.equal(context.apiCalls.length,callCountBeforeNavigation,"local navigation makes no network calls");

await click("project-review-next");
await click("project-review-next");
assert.equal(currentRowId(),"row-3");
assert(!getButton("history-confirm-candidate"),"fuzzy/blocked rows must not expose confirm");
assert(!getButton("history-confirm-and-next"));
assert(getButton("project-review-next"),"fuzzy rows remain reachable in manual sequence");
await click("project-review-next");
assert.equal(currentRowId(),"row-4");
assert(content.innerHTML.includes("Подтверждено пользователем ✓"));
assert(getButton("history-revoke-confirmation"));
assert(!getButton("history-confirm-candidate"),"effective candidate must not show a duplicate confirmation");
await click("project-review-next");
assert.equal(currentRowId(),"row-5");
assert(content.innerHTML.includes("Проверка позиции 5 из 6"));
assert.equal(content.buttons.filter(button => button.classNames.has("history-confirm-and-next")).length,2,"ambiguous exact choices each have their own fast action");

const secondConfirmation = content.buttons.filter(button => button.classNames.has("history-confirm-and-next"))[1];
assert.equal(secondConfirmation.dataset.offerId,"offer-5-b");
await secondConfirmation.click();
assert.equal(context.apiCalls.at(-1).method,"POST");
assert.equal(context.apiCalls.at(-1).body.candidate_offer_id,"offer-5-b","the pressed ambiguous candidate is the one confirmed");
assert.equal(context.apiCalls.filter(call => call.method === "GET").length,0,"successful confirm-and-next adds no GET");
assert.equal(currentRowId(),"row-6","already-confirmed row and non-confirmable rows are skipped for the fast action");
assert.equal(result.human_confirmed_count,2,"summary overlay count updates from the applied snapshot");
assert.equal(getButton("project-review-next").disabled,true,"last review position disables manual next");
await click("history-confirm-and-next");
assert.equal(currentRowId(),"row-6","fast action does not wrap after the last review position");
assert(content.innerHTML.includes("Дальше доступных для подтверждения позиций нет."));
assert(!content.innerHTML.includes("Все доступные для подтверждения позиции просмотрены."),"earlier unresolved positions prevent a false completion state");
for (let step=0;step<5;step++) await click("project-review-previous");
assert.equal(currentRowId(),"row-1","manual previous reaches earlier unresolved rows without wrapping");
await click("history-confirm-and-next");
assert.equal(currentRowId(),"row-1");
assert(content.innerHTML.includes("Все доступные для подтверждения позиции просмотрены."));
assert(content.buttons.some(button => button.classNames.has("project-results-back")),"completion offers return to project result");
assert.equal(result.human_confirmed_count,4);

// Completion leaves manual review navigation available; the filter is untouched.
await click("project-review-next");
assert.equal(currentRowId(),"row-2");
await click("project-review-next");
await click("project-review-next");
assert.equal(currentRowId(),"row-4");
assert(getButton("history-revoke-confirmation"));
await click("history-revoke-confirmation");
assert.equal(currentRowId(),"row-4","revoke stays on the current source row");
assert.equal(result.human_confirmed_count,3);
assert(getButton("history-confirm-and-next"),"revoked row becomes actionable again");

context.staleNextPost = true;
const beforeStale = context.apiCalls.length;
await click("history-confirm-and-next");
assert.equal(currentRowId(),"row-4","stale refresh remains on the same source row");
assert.equal(context.apiCalls.slice(beforeStale).filter(call => call.method === "POST").length,1);
assert.equal(context.apiCalls.slice(beforeStale).filter(call => call.method === "GET").length,1);
assert.equal(context.toastMessages.length,1);

// While a request is pending, navigation and repeated decisions are disabled.
context.deferNextPost = true;
const pendingButton = getButton("history-confirm-and-next");
const pendingRequest = pendingButton.click();
assert.equal(getButton("project-review-previous").disabled,true);
assert.equal(getButton("project-review-next").disabled,true);
assert.equal(getButton("history-confirm-candidate").disabled,true);
assert.equal(currentRowId(),"row-4");
context.pendingResolve();
await pendingRequest;
assert.equal(currentRowId(),"row-4","final confirmation stays visible and completes only after the POST succeeds");
assert(content.innerHTML.includes("Все доступные для подтверждения позиции просмотрены."));

const manualOnly = item("manual-only","none",[],{decision:"NO_MATCH"});
manualOnly.route.history_outcome = "NO_MATCH";
const beforeManual = item("before-manual","history_review",[candidate("before-manual-offer")]);
const afterManual = item("after-manual","history_review",[candidate("after-manual-offer")]);
const manualFlow = {results:[beforeManual,manualOnly,afterManual]};
assert.deepEqual(Array.from(context.sequence(manualFlow),entry => entry.sourceRowId),["before-manual","manual-only","after-manual"]);
assert.equal(context.actionable(manualFlow,"before-manual")?.intent.source_row_id,"after-manual",
  "automatic confirm-and-next keeps skipping manual-search-only rows");
assert.equal(context.actionable(manualFlow,"before-manual",{includeManualSearch:true})?.intent.source_row_id,"manual-only",
  "manual confirm-and-next includes an unresolved NO_MATCH row");
manualOnly.historyEffectiveDecision = {decision_id:"manual-confirmed"};
assert.equal(context.actionable(manualFlow,"before-manual",{includeManualSearch:true})?.intent.source_row_id,"after-manual",
  "manual confirm-and-next advances after the current manual row is confirmed");

const back = getButton("project-results-back");
await back.click();
assert.equal(context.backResult,result);
assert.equal(state.sourcing.projectFilter,"review","back preserves the project filter");

process.stdout.write("PASS: Excel Tender review sequence, identity/order, boundaries, fast confirmation, revoke, stale/race safety, filter, and local navigation\n");
})().catch(error => { process.stderr.write(`${error.stack || error}\n`); process.exitCode = 1; });
