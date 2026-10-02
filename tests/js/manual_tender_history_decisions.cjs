const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[2], "utf8");
const applyStart = source.indexOf("function applyTenderHistoryDecisionSnapshot(result, snapshot) {");
const applyEnd = source.indexOf("async function openExcelTenderRunDetail", applyStart);
const actionRenderStart = source.indexOf("function renderHumanHistoryDecisionAction(item, candidate) {");
const actionBindStart = source.indexOf("function bindHumanHistoryDecisionActions(projectResult, item) {");
const actionEnd = source.indexOf("function renderSourcingResult(result, row = null) {", actionBindStart);
if ([applyStart, applyEnd, actionRenderStart, actionBindStart, actionEnd].some(index => index < 0)) throw new Error("Human history decision UI functions were not found");
const snippet = [source.slice(applyStart, applyEnd), source.slice(actionRenderStart, actionBindStart), source.slice(actionBindStart, actionEnd)].join("\n");
const actionStart = source.indexOf("function bindHumanHistoryDecisionActions(projectResult, item) {");
const actionBindEnd = source.indexOf("function renderSourcingResult(result, row = null) {", actionStart);
const actionSource = source.slice(actionStart, actionBindEnd);
if (actionSource.includes("setInterval") || actionSource.includes("localStorage") || actionSource.includes("sessionStorage")) {
  throw new Error("History decisions must not add polling or persist confirmation/password state in browser storage");
}

const candidate = {offer:{offer_id:"one_c_history:item-1",title:"Плющ искусственный",price:"123.45",currency:"RUB",price_unit:"шт"},decision:null};
const eligible = {candidate_offer_id:candidate.offer.offer_id,offer:candidate.offer,confirmable:true,evidence_fingerprint:"f".repeat(64)};
const item = {intent:{source_row_id:"a".repeat(32)},historyDecisionCandidates:[eligible],historyEffectiveDecision:null};
const escaped = value => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
const button = {
  dataset:{rowId:item.intent.source_row_id,offerId:candidate.offer.offer_id},
  classList:{contains:name=>name === "history-confirm-candidate"},
  disabled:false, handlers:{},
  addEventListener:(name,handler)=>{button.handlers[name]=handler;},
};
const nodes = {"#sourcing-content":{querySelectorAll:selector=>selector.includes("history-confirm") ? [button] : []}};
let postCount = 0;
let getCount = 0;
let detailRenders = 0;
let toastCount = 0;
const snapshot = {
  decision_revision:1,decision_digest:"d".repeat(64),
  effective:{[item.intent.source_row_id]:{decision_id:"b".repeat(32),candidate_offer_id:candidate.offer.offer_id}},
  rows:[{source_row_id:item.intent.source_row_id,candidates:[{...eligible,decision:{decision_id:"b".repeat(32)}}],effective_decision:{decision_id:"b".repeat(32),candidate_offer_id:candidate.offer.offer_id}}],
};
const context = {
  state:{excelTender:{workspace:{tender_id:"tender"},historyDecisionRevision:0,historyDecisionDigest:null}},
  $:selector=>nodes[selector],
  $$:selector=>selector.includes("history-confirm") ? [button] : [],
  escapeHtml:escaped,
  historicalOfferDate:()=>"2025-04-16",
  encodeURIComponent,
  toast:()=>{toastCount+=1;},
  renderProjectItemDetails:()=>{detailRenders+=1;},
  api:async (url,options={})=>{
    if ((options.method || "GET") === "POST") {
      postCount += 1;
      const body = JSON.parse(options.body);
      if (Object.keys(body).sort().join(",") !== "candidate_offer_id,decision,expected_revision,source_row_id") throw new Error("Confirmation sent non-identity authority fields");
      const error = new Error("Stale decision revision");
      error.code = "TENDER_HISTORY_DECISIONS_STALE";
      throw error;
    }
    getCount += 1;
    return snapshot;
  },
};
vm.runInNewContext(`${snippet}; globalThis.renderAction=renderHumanHistoryDecisionAction; globalThis.apply=applyTenderHistoryDecisionSnapshot; globalThis.bind=bindHumanHistoryDecisionActions;`, context);

const exact = context.renderAction(item, {offer:candidate.offer,decision:"REVIEW"});
if (!exact.includes("Подтвердить эту запись")) throw new Error("Eligible exact candidate should expose the explicit confirmation action");
item.historyDecisionCandidates = [{...eligible,confirmable:false,reason_code:"HISTORY_CANDIDATE_SOURCE_CONFLICT"}];
if (context.renderAction(item, {offer:candidate.offer,decision:"REVIEW"}).includes("Подтвердить эту запись")) throw new Error("Conflicted exact candidate must not expose confirmation");
item.historyDecisionCandidates = [eligible];
if (context.renderAction(item, {offer:{...candidate.offer,offer_id:"fuzzy",history_retrieval_classification:"FUZZY"},decision:"REVIEW"}) !== "") throw new Error("Fuzzy candidate must not expose confirmation");

const result = {run_id:"c".repeat(32),historyDecisionRevision:0,results:[item]};
context.bind(result,item);
button.handlers.click().then(() => {
  if (postCount !== 1 || getCount !== 1) throw new Error(`Expected one mutation and a stale-state refresh; got POST=${postCount}, GET=${getCount}`);
  if (result.historyDecisionRevision !== 1 || item.historyEffectiveDecision?.decision_id !== "b".repeat(32)) throw new Error("Stale response did not refresh durable decision state");
  if (detailRenders !== 1 || toastCount !== 1) throw new Error(`Stale decision UI did not re-render and explain the refresh: renders=${detailRenders}, toast=${toastCount}`);
  process.stdout.write("PASS: exact-only confirmation UI, typed body, stale revision refresh, no polling/storage\n");
}).catch(error => { process.stderr.write(`${error.stack || error}\n`); process.exitCode = 1; });
