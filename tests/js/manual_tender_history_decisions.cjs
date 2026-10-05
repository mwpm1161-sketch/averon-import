const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[2], "utf8");
const applyStart = source.indexOf("function applyTenderHistoryDecisionSnapshot(result, snapshot) {");
const applyEnd = source.indexOf("async function openExcelTenderRunDetail", applyStart);
const actionRenderStart = source.indexOf("function renderHumanHistoryDecisionAction(item, candidate) {");
const actionBindStart = source.indexOf("function bindHumanHistoryDecisionActions(projectResult, item) {");
const actionEnd = source.indexOf("function renderSourcingResult(result, row = null) {", actionBindStart);
const projectCandidatesStart = source.indexOf("function projectReviewCandidates(item) {");
const historyPresentationStart = source.indexOf("function historyReviewPresentation(candidates, route = null) {");
const historyPresentationEnd = source.indexOf("function projectHasReviewCandidates(item) {", historyPresentationStart);
const projectSequenceStart = source.indexOf("function projectReviewSequence(result) {");
if ([applyStart, applyEnd, actionRenderStart, actionBindStart, actionEnd, projectCandidatesStart, historyPresentationStart, historyPresentationEnd, projectSequenceStart].some(index => index < 0)) throw new Error("Human history decision UI functions were not found");
const snippet = [source.slice(applyStart, applyEnd), source.slice(actionRenderStart, actionBindStart), source.slice(projectSequenceStart, actionEnd), source.slice(projectCandidatesStart, historyPresentationStart), source.slice(historyPresentationStart, historyPresentationEnd)].join("\n");
const actionStart = source.indexOf("function bindHumanHistoryDecisionActions(projectResult, item) {");
const actionBindEnd = source.indexOf("function renderSourcingResult(result, row = null) {", actionStart);
const actionSource = source.slice(actionStart, actionBindEnd);
if (actionSource.includes("setInterval") || actionSource.includes("localStorage") || actionSource.includes("sessionStorage")) {
  throw new Error("History decisions must not add polling or persist confirmation/password state in browser storage");
}

const candidate = {offer:{offer_id:"one_c_history:item-1",title:"Плющ искусственный",price:"123.45",currency:"RUB",price_unit:"шт",history_retrieval_classification:"NORMALIZED_NAME_UNIT"},decision:null};
const eligible = {candidate_offer_id:candidate.offer.offer_id,offer:candidate.offer,confirmable:true,evidence_fingerprint:"f".repeat(64)};
const sourceRowId = "a".repeat(32);
const item = {intent:{source_row_id:sourceRowId,normalized_name:"Исходное изделие",source_text:"Исходное изделие",unit:"шт",article:"A-1",manufacturer:"Maker",model:"M-1"},historyDecisionCandidates:[eligible],historyEffectiveDecision:null};
const fuzzyOffer = {offer_id:"one_c_history:fuzzy-1",provider:"one_c_history",source_item_id:"fuzzy-1",title:"Похожее историческое изделие",price:"234.50",currency:"RUB",price_unit:"шт",article:"A-1",manufacturer:"Maker",history_characteristic:"M-1",retrieved_at:"2026-10-01T00:00:00Z",history_retrieval_classification:"FUZZY",data_provenance:{purchase_date:"2025-04-16",counterparty:"Поставщик"}};
const fuzzyEligible = {candidate_offer_id:fuzzyOffer.offer_id,offer:{...fuzzyOffer,retrieval_classification:"FUZZY"},price_provenance:{purchase_date:"2025-04-16"},match:{decision:"REVIEW",offer_id:fuzzyOffer.offer_id,conflicting_attributes:[],missing_attributes:["name"]},confirmable:false,confirmable_for_explicit_fuzzy:true,confirmation_basis:"FUZZY_MANUAL_CONFIRMATION",retrieval_rank:1,fuzzy_evidence_fingerprint:"g".repeat(64)};
const escaped = value => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
const makeNode = (classes, dataset={}) => {
  const node = {dataset, disabled:false, checked:false, handlers:{},
    classList:{contains:name=>classes.includes(name)},
    addEventListener:(name,handler)=>{node.handlers[name]=handler;}};
  return node;
};
const button = makeNode(["history-confirm-candidate"], {rowId:sourceRowId,offerId:candidate.offer.offer_id});
const nodes = {"#sourcing-content":{querySelectorAll:selector=>selectNodes(selector)}, "#sourcing-modal":{open:true}};
let nodePool = [button];
function selectNodes(selector) {
  const classes = [...selector.matchAll(/\.([a-z0-9-]+)/g)].map(match=>match[1]);
  return nodePool.filter(node=>classes.some(name=>node.classList.contains(name)));
}
let postCount = 0;
let getCount = 0;
let detailRenders = 0;
let toastCount = 0;
let staleFuzzy = false;
const snapshot = {
  decision_revision:1,decision_digest:"d".repeat(64),
  effective:{[sourceRowId]:{decision_id:"b".repeat(32),candidate_offer_id:candidate.offer.offer_id}},
  rows:[{source_row_id:sourceRowId,candidates:[{...eligible,decision:{decision_id:"b".repeat(32)}}],effective_decision:{decision_id:"b".repeat(32),candidate_offer_id:candidate.offer.offer_id}}],
};
const result = {run_id:"c".repeat(32),historyDecisionRevision:0,results:[item]};
result.source_mode="one_c_only";
const context = {
  state:{excelTender:{workspace:{tender_id:"tender"},historyDecisionRevision:0,historyDecisionDigest:null},sourcing:{fuzzyConfirmation:null,reviewDetailNavigation:{sourceRowId,requestPending:false,showCompletion:false,showEarlierActionableHint:false},modalContext:"excel_tender",result:null}},
  $:selector=>nodes[selector] || null,
  $$:selector=>selectNodes(selector),
  escapeHtml:escaped,
  historicalOfferDate:()=>"2025-04-16",
  isExcelTenderProjectResult:()=>true,
  projectDecision:()=>"REVIEW",
  encodeURIComponent,
  toast:()=>{toastCount+=1;},
  renderProjectItemDetails:()=>{detailRenders+=1;},
  api:async (url,options={})=>{
    if ((options.method || "GET") === "POST") {
      postCount += 1;
      const body = JSON.parse(options.body);
      if (body.confirmation_mode === "EXPLICIT_FUZZY_IDENTITY") {
        if (body.explicit_identity_assertion !== true || body.candidate_offer_id !== fuzzyOffer.offer_id) throw new Error("Fuzzy confirmation did not bind the asserted candidate");
        if (staleFuzzy) {
          staleFuzzy = false;
          const error = new Error("Stale decision revision");
          error.code = "TENDER_HISTORY_DECISIONS_STALE";
          throw error;
        }
        const effective = {decision_id:"e".repeat(32),candidate_offer_id:fuzzyOffer.offer_id,confirmation_basis:"FUZZY_MANUAL_CONFIRMATION",identity_assertion:"SAME_PRODUCT_V1"};
        return {
          decision_revision:2,decision_digest:"e".repeat(64),effective:{[sourceRowId]:effective},
          rows:[{source_row_id:sourceRowId,candidates:[{...fuzzyEligible,decision:{decision_id:effective.decision_id}}],effective_decision:effective}],
          events:[],
        };
      }
      if (body.decision === "REVOKE_HISTORY_CONFIRMATION") {
        return {decision_revision:3,decision_digest:"f".repeat(64),effective:{},rows:[{source_row_id:sourceRowId,candidates:[fuzzyEligible],effective_decision:null}],events:[]};
      }
      if (Object.keys(body).sort().join(",") !== "candidate_offer_id,decision,expected_revision,source_row_id") throw new Error("Confirmation sent non-identity authority fields");
      const error = new Error("Stale decision revision");
      error.code = "TENDER_HISTORY_DECISIONS_STALE";
      throw error;
    }
    getCount += 1;
    return getCount === 1 ? snapshot : {decision_revision:1,decision_digest:"d".repeat(64),effective:{},rows:[{source_row_id:sourceRowId,candidates:[fuzzyEligible],effective_decision:null}]};
  },
};
vm.runInNewContext(`${snippet}; globalThis.renderAction=renderHumanHistoryDecisionAction; globalThis.apply=applyTenderHistoryDecisionSnapshot; globalThis.bind=bindHumanHistoryDecisionActions; globalThis.reviewCandidates=projectReviewCandidates; globalThis.historyPresentation=historyReviewPresentation;`, context);

const normalized = context.renderAction(item, {offer:candidate.offer,decision:"REVIEW"});
if (!normalized.includes("Подтвердить эту запись") || !normalized.includes("Подтвердить и далее")) throw new Error("Server-approved normalized candidate should expose both ordinary confirmation actions");
item.historyDecisionCandidates = [{...eligible,confirmable:false,reason_code:"HISTORY_CANDIDATE_SOURCE_CONFLICT"}];
if (context.renderAction(item, {offer:candidate.offer,decision:"REVIEW"}).includes("Подтвердить эту запись")) throw new Error("Non-confirmable normalized candidate must not expose confirmation");
item.historyDecisionCandidates = [eligible];
const unprojectedFuzzyMarkup = context.renderAction(item, {offer:{...candidate.offer,offer_id:"fuzzy",history_retrieval_classification:"FUZZY"},decision:"REVIEW"});
if (!unprojectedFuzzyMarkup.includes("Этот вариант показан для сравнения, но не доступен для подтверждения.") || unprojectedFuzzyMarkup.includes("Сравнить и подтвердить")) throw new Error("A transient fuzzy candidate needs a neutral unavailable diagnostic");

const normalizedChoices = [
  {offer:{...candidate.offer,history_retrieval_classification:"NORMALIZED_NAME_UNIT"}},
  {offer:{...candidate.offer,offer_id:"one_c_history:item-2",history_retrieval_classification:"NORMALIZED_NAME_UNIT"}},
];
const ambiguousMany = context.historyPresentation(normalizedChoices, null);
if (ambiguousMany.title !== "Несколько совпадений после нормализации — требуется выбор" || !ambiguousMany.explanation.includes("Проверьте характеристики и выберите нужную запись")) throw new Error("Multiple normalized history candidates need explicit choice wording");
const ambiguousReason = context.historyPresentation([normalizedChoices[0]], {history_reason_code:"ambiguous_normalized_name_identity"});
if (ambiguousReason.title !== "Несколько совпадений после нормализации — требуется выбор") throw new Error("Normalized ambiguity reason must use explicit choice wording");
const rejected = {route:{final_source_kind:"history_review"},offers:[normalizedChoices[0].offer],match_results:[{offer:normalizedChoices[0].offer,decision:"REJECT"}],review_candidate:{offer:normalizedChoices[0].offer,decision:"REJECT"}};
if (context.reviewCandidates(rejected).length !== 0) throw new Error("A rejected history match must not be synthesized as a REVIEW card");
const fuzzyPresentation = context.historyPresentation([{offer:fuzzyOffer,decision:"REVIEW"}], {final_source_kind:"history_review"});
if (fuzzyPresentation.title !== "Похожие позиции в истории 1С — требуется ручное сравнение" || fuzzyPresentation.explanation !== "Автоматически подтвердить совпадение нельзя. Сравните исходную позицию с записью истории.") throw new Error("Fuzzy-only presentation must clearly require manual comparison");
const laterFuzzyOffer = {...fuzzyOffer,offer_id:"one_c_history:rank-8",source_item_id:"rank-8",title:"Поздний actionable вариант",retrieval_classification:"FUZZY"};
const transientFuzzyOffers = Array.from({length:8}, (_,index)=>({...fuzzyOffer,offer_id:`one_c_history:transient-${index+1}`,history_retrieval_classification:"FUZZY"}));
const laterFuzzyState = {...fuzzyEligible,candidate_offer_id:laterFuzzyOffer.offer_id,offer:laterFuzzyOffer,retrieval_rank:8};
const lateRankProject = {
  route:{final_source_kind:"history_review"}, offers:transientFuzzyOffers,
  match_results:transientFuzzyOffers.map((offer,index)=>({offer,decision:"REVIEW",rank:index+1})),
  historyDecisionCandidates:[laterFuzzyState],
};
const visibleLateRank = context.reviewCandidates(lateRankProject);
const lateRankCard = visibleLateRank.find(candidate=>candidate.offer.offer_id===laterFuzzyOffer.offer_id);
if (!lateRankCard || lateRankCard.rank !== 8) throw new Error("A persisted actionable fuzzy candidate beyond the first five must remain visible with its original rank");
if (!context.renderAction({...item,historyDecisionCandidates:[laterFuzzyState]},lateRankCard).includes("Сравнить и подтвердить")) throw new Error("A persisted late-rank fuzzy candidate must retain its explicit compare action");

async function runUiLifecycle() {
  context.state.sourcing.result = result;
  context.bind(result,item);
  await button.handlers.click();
  if (postCount !== 1 || getCount !== 1) throw new Error(`Expected one mutation and a stale-state refresh; got POST=${postCount}, GET=${getCount}`);
  if (result.historyDecisionRevision !== 1 || item.historyEffectiveDecision?.decision_id !== "b".repeat(32)) throw new Error("Stale response did not refresh durable decision state");
  if (detailRenders !== 1 || toastCount !== 1) throw new Error(`Stale decision UI did not re-render and explain the refresh: renders=${detailRenders}, toast=${toastCount}`);

  item.historyEffectiveDecision = null;
  item.historyDecisionCandidates = [fuzzyEligible];
  let markup = context.renderAction(item, {offer:fuzzyOffer,decision:"REVIEW"});
  if (!markup.includes("Сравнить и подтвердить") || markup.includes("Подтвердить эту запись") || markup.includes("Подтвердить и далее")) throw new Error("Fuzzy card must offer compare first and no one-click confirmation");
  const compare = makeNode(["history-fuzzy-compare"], {rowId:sourceRowId,offerId:fuzzyOffer.offer_id});
  nodePool = [compare];
  context.bind(result,item);
  await compare.handlers.click();
  if (postCount !== 1) throw new Error("Opening fuzzy comparison must not POST a confirmation");
  markup = context.renderAction(item, {offer:fuzzyOffer,decision:"REVIEW"});
  for (const label of ["Исходная позиция", "История 1С", "M-1", "M-1", "234.50", "2025-04-16", "Название отличается и совпадение не подтверждено автоматически.", "Подтвердите только если это действительно одна и та же позиция.", "Я подтверждаю, что это одна и та же позиция"]) {
    if (!markup.includes(label)) throw new Error(`Fuzzy compare view is missing ${label}`);
  }
  const cancel = makeNode(["history-fuzzy-cancel"], {rowId:sourceRowId,offerId:fuzzyOffer.offer_id});
  nodePool = [cancel];
  context.bind(result,item);
  cancel.handlers.click();
  if (context.state.sourcing.fuzzyConfirmation !== null || !context.renderAction(item, {offer:fuzzyOffer,decision:"REVIEW"}).includes("Сравнить и подтвердить")) throw new Error("Cancelling comparison must return to the same candidate detail");

  const compareAgain = makeNode(["history-fuzzy-compare"], {rowId:sourceRowId,offerId:fuzzyOffer.offer_id});
  nodePool = [compareAgain];
  context.bind(result,item);
  await compareAgain.handlers.click();
  const assertion = makeNode(["history-fuzzy-assertion"], {offerId:fuzzyOffer.offer_id});
  const confirmAndNext = makeNode(["history-fuzzy-confirm-and-next"], {rowId:sourceRowId,offerId:fuzzyOffer.offer_id});
  confirmAndNext.disabled = true;
  nodePool = [assertion,confirmAndNext];
  context.bind(result,item);
  if (!confirmAndNext.disabled) throw new Error("Fuzzy final confirmation must start disabled");
  await confirmAndNext.handlers.click();
  if (postCount !== 1) throw new Error("Unchecked fuzzy assertion must not POST");
  assertion.checked = true;
  assertion.handlers.change();
  if (confirmAndNext.disabled) throw new Error("Checking the explicit identity assertion must enable confirmation");
  await confirmAndNext.handlers.click();
  if (postCount !== 2 || item.historyEffectiveDecision?.confirmation_basis !== "FUZZY_MANUAL_CONFIRMATION") throw new Error("Fuzzy confirmation must use the explicit server mode and show the human-confirmed badge");
  if (context.state.sourcing.reviewDetailNavigation.sourceRowId !== sourceRowId || !context.state.sourcing.reviewDetailNavigation.showCompletion) throw new Error("Confirm-and-next must advance only after successful POST");
  markup = context.renderAction(item, {offer:fuzzyOffer,decision:"REVIEW"});
  if (!markup.includes("Подтверждено пользователем") || !markup.includes("Отменить подтверждение")) throw new Error("Successful fuzzy confirmation badge/revoke action is missing");

  const revoke = makeNode(["history-revoke-confirmation"], {decisionId:"e".repeat(32)});
  nodePool = [revoke];
  context.bind(result,item);
  await revoke.handlers.click();
  if (item.historyEffectiveDecision !== null || !context.renderAction(item, {offer:fuzzyOffer,decision:"REVIEW"}).includes("Сравнить и подтвердить")) throw new Error("Revoke must restore the candidate to unconfirmed comparison state");

  const compareStale = makeNode(["history-fuzzy-compare"], {rowId:sourceRowId,offerId:fuzzyOffer.offer_id});
  nodePool = [compareStale];
  context.bind(result,item);
  await compareStale.handlers.click();
  const staleAssertion = makeNode(["history-fuzzy-assertion"], {offerId:fuzzyOffer.offer_id});
  const staleConfirm = makeNode(["history-fuzzy-confirm"], {rowId:sourceRowId,offerId:fuzzyOffer.offer_id});
  nodePool = [staleAssertion,staleConfirm];
  context.bind(result,item);
  staleAssertion.checked = true;
  staleAssertion.handlers.change();
  staleFuzzy = true;
  await staleConfirm.handlers.click();
  if (context.state.sourcing.reviewDetailNavigation.sourceRowId !== sourceRowId || item.historyEffectiveDecision !== null) throw new Error("A stale fuzzy decision must refresh but remain on the current source row");
  process.stdout.write("PASS: strong confirmation unchanged; fuzzy compare/assertion/two-step, no first-click POST, cancel, next-after-success, revoke, stale refresh, ambiguity wording, REJECT exclusion, no polling/storage\n");
}

runUiLifecycle().catch(error => { process.stderr.write(`${error.stack || error}\n`); process.exitCode = 1; });
