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

const nodes = new Map();
function node(selector) {
  if (!nodes.has(selector)) nodes.set(selector, {
    hidden: false, disabled: false, textContent: "", innerHTML: "", open: true,
    addEventListener(event, callback) { this["on" + event] = callback; },
  });
  return nodes.get(selector);
}
const state = {sourcing: null};
const context = vm.createContext({
  state,
  $: node,
  $$: selector => selector.includes(".project-results-back") ? [node(".project-results-back")] : [],
  escapeHtml: (value) => String(value ?? ""),
  offerTitleHtml: (offer) => String(offer?.title || ""),
  historicalOfferPrice: () => "100 ₽ / шт",
  historicalOfferDate: () => "2026-08-01",
  historicalOfferCounterparty: () => "поставщик",
  renderSourcingDecision: () => "<decision>must-not-overstate</decision>",
  renderProductUnderstanding: () => "",
  renderSourcingRouteExplanation: () => "",
  isExcelTenderProjectResult: () => false,
  renderHumanHistoryDecisionAction: () => "",
  manualHistorySearchAllowed: () => false,
  renderManualHistorySearch: () => "",
  bindHumanHistoryDecisionActions: () => {},
  renderSourcingResult: (result) => {
    context.backTarget = result;
    context.setSourcingModalPhase("project_result");
  },
});
const modalBlock = sourceBetween("function createSourcingState(", "\nconst state =");
const phaseBlock = sourceBetween("function setSourcingModalPhase(", "\nfunction sourcingModeLoadingCopy");
const candidateBlock = sourceBetween("function projectReviewCandidates(", "\nfunction renderProjectPriceCell");
const cardBlock = sourceBetween("function renderHistoricalOfferCard(", "\nfunction sourcingRouteExplanation");
const detailBlock = sourceBetween("function renderProjectItemDetails(", "\nfunction bindSourcingFilters");
vm.runInContext(modalBlock + "\n" + phaseBlock + "\n" + candidateBlock + "\n" + cardBlock + "\n" + detailBlock, context);
state.sourcing = context.createSourcingState();

const submit = node("#tender-sourcing-submit");
context.setSourcingModalPhase("before_run", "excel_tender");
assert.equal(submit.hidden, false, "Excel launch is visible before run");
assert.equal(submit.disabled, false);
assert.equal(submit.textContent, "Запустить подбор по выбранным строкам");
for (const phase of ["queued", "running", "project_result", "position_detail"]) {
  context.setSourcingModalPhase(phase, "excel_tender");
  assert.equal(submit.hidden, true, phase + " hides the project launch");
}
context.setSourcingModalPhase("failed", "excel_tender");
assert.equal(submit.hidden, false, "failed Excel sourcing exposes retry");
assert.equal(submit.textContent, "Повторить подбор");
context.setSourcingModalPhase("running", "manual_project");
assert.equal(submit.hidden, true, "shared manual project flow never exposes Excel launch");
context.setSourcingModalPhase("running", "manual_understanding");
assert.equal(submit.hidden, true, "manual single-row flow never exposes Excel launch");
context.setSourcingModalPhase("running", "single_row");
assert.equal(submit.hidden, true, "PDF/document single-row flow never exposes Excel launch");
context.setSourcingModalPhase("running", "project");
assert.equal(submit.hidden, true, "document project flow never exposes Excel launch");
context.setSourcingModalPhase("closed", "none");
assert.equal(submit.hidden, true, "closing resets the launch presentation");
assert.equal(state.sourcing.modalContext, "none");

const makeOffer = (offer_id, classification, title) => ({
  offer_id, title, provider: "one_c_history", price: 100, price_unit: "шт",
  history_retrieval_classification: classification,
});
const exactOne = makeOffer("exact-1", "EXACT_NAME_UNIT", "Точная запись");
const exactTwo = makeOffer("exact-2", "EXACT_NAME_UNIT", "Вторая точная запись");
const normalizedOne = makeOffer("normalized-1", "NORMALIZED_NAME_UNIT", "Плющ искусственный");
const fuzzy = makeOffer("fuzzy-1", "FUZZY", "Похожая запись");
const historyItem = (offers, reason = "history_requires_review") => ({
  route: {final_source_kind: "history_review", source_mode: "one_c_only", history_reason_code: reason},
  offers,
  match_results: offers.map((offer, index) => ({offer, decision: "ALTERNATIVE", rank: index + 1})),
});

const unique = historyItem([exactOne, fuzzy]);
const uniqueCandidates = context.projectReviewCandidates(unique);
assert.deepEqual(Array.from(uniqueCandidates, (item) => item.offer.offer_id), ["exact-1"]);
assert.equal(context.historyReviewPresentation(uniqueCandidates, unique.route).title,
  "Найдено точное название и единица измерения в истории 1С");
const exactCard = context.renderHistoricalOfferCard(uniqueCandidates[0], {route: unique.route});
assert(exactCard.includes("Требуется подтверждение"));
assert(!exactCard.includes("must-not-overstate"), "exact history review must not be labeled as an alternative");

const normalizedPrimary = historyItem([fuzzy, normalizedOne]);
const normalizedCandidates = context.projectReviewCandidates(normalizedPrimary);
assert.deepEqual(Array.from(normalizedCandidates, item => item.offer.offer_id), ["normalized-1"],
  "normalized candidates take precedence over fuzzy neighbours");
const normalizedPresentation = context.historyReviewPresentation(normalizedCandidates, normalizedPrimary.route);
assert.equal(normalizedPresentation.title, "Совпадает после нормализации названия и единицы");
assert.equal(normalizedPresentation.explanation,
  "Формулировка отличается, но нормализованное название и единица измерения совпадают. Проверьте запись перед подтверждением.");
const normalizedCard = context.renderHistoricalOfferCard(normalizedCandidates[0], {route: normalizedPrimary.route});
assert(normalizedCard.includes("Сильное совпадение"));
assert(normalizedCard.includes("normalized-retrieval"));
assert(!normalizedCard.includes("Точное название"));

const rejectedNormalized = historyItem([normalizedOne]);
rejectedNormalized.match_results = [{offer:normalizedOne, decision:"REJECT", rank:1}];
assert.deepEqual(Array.from(context.projectReviewCandidates(rejectedNormalized)), [],
  "a real REJECT result must not become a synthetic REVIEW card");

const unpricedCard = context.renderHistoricalOfferCard({
  offer: {...exactOne, price: null}, decision: "REVIEW",
}, {route: unique.route});
assert(unpricedCard.includes("нет цены, пригодной для расчёта"));

const ambiguous = historyItem([exactOne, exactTwo, fuzzy], "ambiguous_exact_name_identity");
const ambiguousCandidates = context.projectReviewCandidates(ambiguous);
assert.deepEqual(Array.from(ambiguousCandidates, (item) => item.offer.offer_id), ["exact-1", "exact-2"]);
assert.equal(context.historyReviewPresentation(ambiguousCandidates, ambiguous.route).title,
  "Несколько точных вариантов — требуется выбор");
const ambiguousArticle = historyItem([
  makeOffer("article-ambiguous-1", "EXACT_ARTICLE", "Статья в истории"),
], "ambiguous_or_non_strict_evidence");
assert.equal(context.historyReviewPresentation(
  context.projectReviewCandidates(ambiguousArticle), ambiguousArticle.route,
).title, "Несколько точных вариантов — требуется выбор");

const fuzzyOnly = historyItem([fuzzy]);
const fuzzyCandidates = context.projectReviewCandidates(fuzzyOnly);
assert.equal(context.historyReviewPresentation(fuzzyCandidates, fuzzyOnly.route).title,
  "Похожие позиции в истории 1С — требуется ручное сравнение");
const fuzzyCard = context.renderHistoricalOfferCard(fuzzyCandidates[0], {route: fuzzyOnly.route});
assert(fuzzyCard.includes("Похожее название · не подтверждено"));
assert(!fuzzyCard.includes("must-not-overstate"));

const projectResult = {results: [unique]};
context.setSourcingModalPhase("project_result", "excel_tender");
context.renderProjectItemDetails(projectResult, {...unique, intent: {normalized_name: "Источник", source_text: "Источник"}});
assert.equal(state.sourcing.modalPhase, "position_detail");
assert.equal(submit.hidden, true);
node(".project-results-back").onclick();
assert.equal(context.backTarget, projectResult, "back returns to the original project result");
assert.equal(state.sourcing.modalPhase, "project_result");
assert.equal(submit.hidden, true);

console.log("PASS: exact-first history review and sourcing modal lifecycle");
