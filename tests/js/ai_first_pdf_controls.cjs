const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.resolve(__dirname, "../../averon_import/static/ai_first_ui.js"), "utf8");
function sourceBetween(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + start.length);
  assert(first >= 0 && last > first, `source block not found: ${start}`);
  return source.slice(first, last);
}

const image = {naturalWidth:1200,style:{}};
const stage = {clientWidth:620};
const wrap = {style:{}};
const label = {textContent:""};
const button = {disabled:false};
const exportNote = {textContent:""};
const exportFeedback = {hidden:false};
const retryButton = {hidden:true};
const elements = new Map([
  ["#ai-first-pdf-image",image],
  [".ai-first-pdf-stage",stage],
  [".ai-first-pdf-image-wrap",wrap],
  ["#ai-first-zoom-label",label],
  ["#ai-first-export-pilot",button],
  ["#ai-first-export-note",exportNote],
  ["#ai-first-export-feedback",exportFeedback],
  ["#ai-first-retry-export-preview",retryButton],
]);
const state = {document:{document_id:"synthetic-document"},aiFirst:{pdfZoom:1,pdfFitWidth:true,exportPages:new Set([1]),reviewGeneration:4,exportPreviewSelectionKey:"1|4"}};
let previewCalls = 0;
let previewFails = false;
let holdPreview = false;
let rejectHeldPreview = null;
const context = {
  state,
  document:{querySelector:selector=>elements.get(selector) || null},
  window:{getComputedStyle:()=>({paddingLeft:"10px",paddingRight:"10px"})},
  api:async (url,options) => {
    previewCalls += 1;
    assert(url.endsWith("/ai-first/pilot-export/preview"));
    assert.equal(JSON.stringify(JSON.parse(options.body)),"{\"page_numbers\":[1]}");
    if (holdPreview) return new Promise((_resolve,reject)=>{rejectHeldPreview=reject;});
    if (previewFails) throw new Error("synthetic preview failure");
    return {page_numbers:[1],ocr_row_count:2,manual_row_count:1,total_row_count:3,verified_position_count:2,document_page_count:8};
  },
};

const zoomCode = sourceBetween("const AI_FIRST_PDF_MIN_ZOOM", "function showAIFirstPage");
vm.runInNewContext(`${zoomCode}; globalThis.setZoom=setAIFirstPdfZoom; globalThis.renderZoom=renderAIFirstPdfZoom; globalThis.nudgeZoom=nudgeAIFirstPdfZoom;`,context);
context.renderZoom();
assert.equal(image.style.width,"600px","fit width accounts for the PDF pane padding");
assert.equal(label.textContent,"По ширине");
context.setZoom(1);
assert.equal(image.style.width,"1200px","100% uses the PDF's natural pixel width");
assert.equal(label.textContent,"100%");
context.nudgeZoom(1);
assert.equal(image.style.width,"1440px","zoom buttons advance in bounded 20% steps");
context.setZoom(99);
assert.equal(image.style.width,"2880px","maximum zoom remains bounded");

const exportCode = sourceBetween("function updateAIFirstExportControls", "function aiFirstColumnList");
  vm.runInNewContext(`${exportCode}; globalThis.updateExport=updateAIFirstExportControls; globalThis.loadPreview=loadAIFirstPilotExportPreview; globalThis.retryPreview=retryAIFirstPilotExportPreview;`,context);
async function run() {
  await context.loadPreview([1],"1|4");
  context.updateExport();
  assert.equal(previewCalls,1,"the preview comes from the server once per selected fragment and review generation");
  assert.equal(button.disabled,false,"the test export is enabled only after the server has checked the fragment");
  assert(exportNote.textContent.includes("К проверенным позициям допущено 2"));
  assert(exportNote.textContent.includes("строк в файле — 3"));
  assert(exportNote.textContent.includes("OCR 2"));
  assert(exportNote.textContent.includes("вручную 1"));
  assert(exportNote.textContent.includes("Предварительная проверка допуска не гарантирует создание файла"));
  assert.equal(exportFeedback.hidden,false,"test and partial-scope warnings appear beside the download action after page selection");
  state.aiFirst.exportPages.clear();
  context.updateExport();
  assert.equal(button.disabled,true,"the workbook remains disabled without selected pages");
  assert.equal(exportFeedback.hidden,true,"the test-export note stays contextual and hidden before page selection");
  state.aiFirst.exportPages = new Set([1]);
  state.aiFirst.reviewGeneration = 5;
  previewFails = true;
  context.updateExport();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(button.disabled,true,"the workbook stays disabled when the server cannot verify its contents");
  assert(exportNote.textContent.includes("Не удалось проверить строки"));
  assert.equal(retryButton.hidden,false,"an explicit retry is offered after preview failure");
  previewFails = false;
  await context.retryPreview();
  assert.equal(previewCalls,3,"manual retry issues one fresh preview request");
  assert.equal(button.disabled,false,"a successful retry unlocks the export button");
  assert.equal(retryButton.hidden,true,"the retry control hides after successful preview");

  state.aiFirst.reviewGeneration = 6;
  state.aiFirst.exportPages = new Set([1]);
  holdPreview = true;
  context.updateExport();
  assert.equal(typeof rejectHeldPreview,"function","a pending preview request was started");
  state.aiFirst.exportPages.clear();
  context.updateExport();
  rejectHeldPreview(new Error("stale preview failure"));
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.notEqual(state.aiFirst.exportPreviewErrorKey,"1|6","a stale response cannot mark a changed selection as failed");
  assert.equal(button.disabled,true,"no selected pages remains fail-closed while a stale response settles");
  console.log("PASS: AI-first PDF zoom bounds, server-derived test-export count and contextual safety notes");
}
run().catch(error=>{console.error(error);process.exitCode=1;});
