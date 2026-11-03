const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

let chromium;
try { ({chromium} = require("playwright")); }
catch (_) {
  console.log("SKIP: optional Playwright package is unavailable; summary-card browser regression was not run");
  process.exit(0);
}

const rootPath = path.resolve(__dirname, "../..");
const stylesPath = path.join(rootPath, "averon_import", "static", "styles.css");
const browserPath = process.env.AVERON_TEST_BROWSER || [
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
].find(fs.existsSync);

if (!browserPath || !fs.existsSync(browserPath)) {
  console.log("SKIP: no local Chromium/Edge executable is available; summary-card browser regression was not run");
  process.exit(0);
}

const html = `<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>AI-first summary-card regression</title></head>
<body><div class="app-shell"><aside class="sidebar"></aside><main><section class="review-view">
  <div class="summary-row ai-first-review-summary">
    <div class="summary-card"><small>Строк распознано</small><b>25</b></div>
    <div class="summary-card"><small>Предметных строк</small><b>19</b></div>
    <div class="summary-card"><small>Подтверждённые самостоятельные позиции</small><b>18</b></div>
    <div class="summary-card warning"><small>Страницы · полнота</small><b>100/100 · 100 подтверждено</b></div>
  </div>
</section></main></div></body></html>`;

async function run() {
  const browser = await chromium.launch({headless:true, executablePath:browserPath});
  try {
    const page = await browser.newPage({viewport:{width:1000,height:900}});
    await page.setContent(html);
    await page.addStyleTag({path:stylesPath});
    for (const width of [800, 1000, 1280, 1440]) {
      await page.setViewportSize({width, height:900});
      const layout = await page.evaluate(() => {
        const card = document.querySelector(".summary-card.warning");
        const label = card.querySelector("small");
        const value = card.querySelector("b");
        const cardRect = card.getBoundingClientRect();
        const labelRect = label.getBoundingClientRect();
        const valueRect = value.getBoundingClientRect();
        return {
          width:innerWidth,
          documentWidth:document.documentElement.scrollWidth,
          clientWidth:document.documentElement.clientWidth,
          fontSize:getComputedStyle(value).fontSize,
          labelBottom:labelRect.bottom,
          valueTop:valueRect.top,
          cardLeft:cardRect.left,
          cardRight:cardRect.right,
          labelLeft:labelRect.left,
          labelRight:labelRect.right,
          valueLeft:valueRect.left,
          valueRight:valueRect.right,
          valueText:value.textContent,
        };
      });
      assert.equal(layout.valueText, "100/100 · 100 подтверждено");
      assert.equal(layout.fontSize, "17px", `readable font at ${width}px`);
      assert(layout.valueTop >= layout.labelBottom, `label and value do not overlap at ${width}px`);
      assert(layout.labelLeft >= layout.cardLeft && layout.valueRight <= layout.cardRight, `content stays in card at ${width}px`);
      assert(layout.documentWidth <= layout.clientWidth, `no document-level horizontal overflow at ${width}px`);
    }
  } finally {
    await browser.close();
  }
  console.log("PASS: AI-first completeness card keeps long values readable and contained at 800/1000/1280/1440px.");
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
