const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

let chromium;
try { ({chromium} = require("playwright")); }
catch (_) {
  console.log("SKIP: optional Playwright package is unavailable; native browser regression was not run");
  process.exit(0);
}

const rootPath = path.resolve(__dirname, "../..");
const source = fs.readFileSync(path.join(rootPath, "averon_import/static/ai_first_ui.js"), "utf8");
const stylesPath = path.join(rootPath, "averon_import/static/styles.css");
const browserPath = process.env.AVERON_TEST_BROWSER || [
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
].find(fs.existsSync);

if (!browserPath || !fs.existsSync(browserPath)) {
  console.log("SKIP: no local Chromium/Edge executable is available; native browser regression was not run");
  process.exit(0);
}

function sourceBetween(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + start.length);
  assert(first >= 0 && last > first, `source block not found: ${start}`);
  return source.slice(first, last);
}

function groupHtml(groupId, rowIds) {
  const headers = Array.from({length:9}, (_, index) => `<th>Исходная колонка ${index + 1}</th>`).join("");
  const rows = rowIds.map((id) => `<tr data-ai-first-row="${id}" data-page-number="1">
    <td class="selector"><label class="ai-first-row-select-hit"><input type="checkbox" data-ai-first-select-row="${id}" aria-label="Выбрать строку ${id}"><span aria-hidden="true"></span></label></td>
    <td><button type="button" data-ai-first-open-row="${id}">${id} · Проверить</button></td>
    ${Array.from({length:9}, (_, index) => `<td><input class="ai-first-cell-input" data-ai-first-cell="${id}" value="${id}-${index + 1}"></td>`).join("")}
  </tr>`).join("");
  return `<section class="ai-first-source-table-group" data-ai-first-table-group="${groupId}">
    <h4>${groupId}</h4><table class="data-table ai-first-table"><thead><tr><th class="selector">Выбор</th><th>Строка</th>${headers}</tr></thead><tbody>${rows}</tbody></table>
  </section>`;
}

const html = `<style>:root{--line:#dfe4ea;--muted:#66707b}body{margin:12px;font-family:Arial,sans-serif}</style>
  <div id="ai-first-review-controls"><div class="ai-first-bulk-toolbar">
    <div class="ai-first-selection-actions"><button data-ai-first-select-visible>Выбрать все видимые</button><button data-ai-first-clear-page-selection>Снять выделение</button><span data-ai-first-selected-count></span></div>
    <input id="ai-first-search"><select id="ai-first-review-filter"><option value="all">Все</option></select>
    <button id="ai-first-confirm-selected"></button><button data-ai-first-bulk-role-submit></button><span data-ai-first-filter-summary></span>
  </div></div>
  <div class="ai-first-table-scroll" id="ai-first-table-scroll" style="width:520px;height:300px;border:1px solid #bbb">
    ${groupHtml("table-1", ["r1", "r2"])}${groupHtml("table-2", ["r3"])}
  </div>`;

const selectionCode = sourceBetween("function aiFirstVisibleSelectedIds", "function renderAIFirstInspectorValues");
const bootstrap = `window.state={document:{document_id:"synthetic-doc",page_count:1},aiFirst:{page:1,selectedRows:new Set(),selectionRevisions:new Map(),pendingRowValues:{r1:{"cell:0":"unsaved correction"}},data:{draft:{pages:[{page_number:1,extraction_fingerprint:"fp-1"}]},review:{row_roles:{},decisions:{}}},renderedEntries:new Map(["r1","r2","r3"].map(id=>[id,{row:{id,page_number:1},page:{page_number:1,extraction_fingerprint:"fp-1"}}]))}};
function aiFirstPageRecord(number){return state.aiFirst.data.draft.pages.find(page=>page.page_number===number)||null}
function aiFirstRowReviewState(){return {status:"neutral"}}
function aiFirstDecision(){return null}
function aiFirstEntrySearchText(entry){return entry.row.id}
window.__inspectorOpens=0;window.__pdfLocatorCalls=0;window.__selectorClickTargets=[];`;
const listeners = `
document.querySelectorAll("#ai-first-table-scroll [data-ai-first-select-row]").forEach(input=>input.addEventListener("click",event=>handleAIFirstRowSelection(input,event)));
document.querySelectorAll(".ai-first-row-select-hit").forEach(label=>label.addEventListener("click",event=>window.__selectorClickTargets.push({tag:event.target.tagName,shift:event.shiftKey}),true));
document.querySelectorAll("#ai-first-table-scroll tr[data-ai-first-row]").forEach(row=>row.addEventListener("click",event=>{if(!shouldOpenAIFirstRowInspector(event))return;window.__inspectorOpens++;window.__pdfLocatorCalls++;}));
document.querySelector("[data-ai-first-select-visible]").addEventListener("click",selectAllVisibleAIFirstRows);
document.querySelector("[data-ai-first-clear-page-selection]").addEventListener("click",clearAIFirstPageSelection);
document.querySelector("#ai-first-search").addEventListener("input",applyAIFirstReviewFilters);
window.__selected=()=>[...state.aiFirst.selectedRows];window.__visibleSelected=()=>aiFirstVisibleSelectedIds();`;

async function run() {
  const browser = await chromium.launch({headless:true,executablePath:browserPath});
  try {
    for (const viewport of [{width:1280,height:900},{width:1000,height:768}]) {
      const page = await browser.newPage({viewport});
      await page.setContent(html);
      await page.addStyleTag({path:stylesPath});
      await page.addScriptTag({content:bootstrap + selectionCode + listeners});

      const checkbox = page.locator('[data-ai-first-select-row="r1"]');
      const label = page.locator('tr[data-ai-first-row="r1"] .ai-first-row-select-hit');
      const targetMetrics = await page.evaluate(() => {
        const label=document.querySelector('tr[data-ai-first-row="r1"] .ai-first-row-select-hit');
        const input=label.querySelector("input");
        const visual=label.querySelector("span");
        const rect=label.getBoundingClientRect();
        return {hit:[rect.width,rect.height],input:[input.getBoundingClientRect().width,input.getBoundingClientRect().height],visual:[visual.getBoundingClientRect().width,visual.getBoundingClientRect().height],outerTarget:document.elementFromPoint(rect.left+3,rect.top+22)?.tagName};
      });
      assert.deepEqual(targetMetrics.hit,[44,44]);
      assert.deepEqual(targetMetrics.input,[44,44]);
      assert.deepEqual(targetMetrics.visual,[21,21]);
      assert.equal(targetMetrics.outerTarget,"INPUT","the label's outer hit area is covered by the native checkbox");
      await label.click({position:{x:3,y:22}}); // outside the 21px checkbox, inside its 44px label target
      assert.equal(await checkbox.isChecked(), true, "native label activation selects the checkbox");
      assert.deepEqual(await page.evaluate(() => window.__selected()), ["r1"]);
      assert.equal(await page.evaluate(() => window.__inspectorOpens), 0, "clicking the label's outer area does not open the row inspector");
      assert.equal(await page.evaluate(() => window.__pdfLocatorCalls), 0, "selector clicks do not trigger PDF locator work");
      assert.equal(await page.evaluate(() => state.aiFirst.pendingRowValues.r1["cell:0"]), "unsaved correction", "selection leaves pending values intact");

      await page.locator('tr[data-ai-first-row="r2"] .ai-first-row-select-hit').click({position:{x:3,y:22},modifiers:["Shift"]});
      const visibleAfterShift = (await page.evaluate(() => window.__visibleSelected())).sort();
      assert.deepEqual(visibleAfterShift, ["r1","r2"], "native Shift-click on the outer label hit area selects the ID range");
      assert.deepEqual(await page.evaluate(() => window.__selectorClickTargets.at(-1)), {tag:"INPUT",shift:true}, "the 44px hit target delivers a native modifier-bearing input click");
      assert.equal(await page.evaluate(() => window.__inspectorOpens), 0, "the Shift label click also avoids opening the inspector");

      await page.locator("#ai-first-search").fill("r2");
      assert.deepEqual(await page.evaluate(() => window.__visibleSelected()), ["r2"], "filtered-out rows are excluded from bulk selection");
      assert.match(await page.locator("[data-ai-first-selected-count]").textContent(), /скрыта фильтром|скрыты фильтром|скрыто фильтром/);

      const scrollMetrics = await page.evaluate(() => {
        const root=document.querySelector("#ai-first-table-scroll");
        const row=root.querySelector("tr[data-ai-first-row]:not([hidden])");
        const before=[row.cells[0].getBoundingClientRect().left,row.cells[1].getBoundingClientRect().left];
        root.scrollLeft=220;
        const after=[row.cells[0].getBoundingClientRect().left,row.cells[1].getBoundingClientRect().left];
        return {overflow:root.scrollWidth>root.clientWidth,delta:after[1]-after[0],stable:Math.abs(before[0]-after[0])<=1&&Math.abs(before[1]-after[1])<=1};
      });
      assert.equal(scrollMetrics.overflow,true);
      assert.equal(scrollMetrics.stable,true,"sticky columns remain fixed during horizontal scroll");
      assert(Math.abs(scrollMetrics.delta-48)<=1,"sticky offsets match the shared selector width");
      await page.close();
    }
  } finally { await browser.close(); }
}

run().then(() => console.log("PASS: native Edge label activation, Shift+Click bubbling, no inspector/PDF request, preserved edits, filter scope and sticky horizontal scroll at 1280px/1000px"))
  .catch((error) => { console.error(error.stack || error); process.exitCode = 1; });
