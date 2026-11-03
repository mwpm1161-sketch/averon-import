const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const cssPath = path.join(__dirname, "..", "..", "averon_import", "static", "styles.css");
const styles = fs.readFileSync(cssPath, "utf8");

function lastRule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const matches = [...styles.matchAll(new RegExp(`${escaped}\\s*\\{([^{}]*)\\}`, "g"))];
  assert.ok(matches.length, `Missing CSS rule for ${selector}`);
  return Object.fromEntries(
    matches.at(-1)[1]
      .split(";")
      .map((item) => item.trim())
      .filter(Boolean)
      .map((item) => {
        const separator = item.indexOf(":");
        return [item.slice(0, separator).trim(), item.slice(separator + 1).trim()];
      }),
  );
}

const pdfHeader = lastRule(".ai-first-pdf-panel .pane-header");
assert.equal(pdfHeader.height, "auto");
assert.equal(pdfHeader["box-sizing"], "border-box");
assert.equal(pdfHeader["flex-wrap"], "wrap");

const pdfZoom = lastRule(".ai-first-zoom-controls");
assert.equal(pdfZoom.flex, "0 0 100%");
assert.equal(pdfZoom["margin-left"], "0");
assert.equal(pdfZoom["min-width"], "0");
assert.equal(pdfZoom["flex-wrap"], "wrap");

const tableHeading = lastRule(".ai-first-table-heading");
assert.equal(tableHeading.height, "auto");
assert.equal(tableHeading["flex-wrap"], "wrap");

const tableHeadingButton = lastRule(".ai-first-table-heading .button");
assert.equal(tableHeadingButton["max-width"], "100%");
assert.equal(tableHeadingButton["white-space"], "normal");

assert.equal(lastRule(".ai-first-review-summary")["grid-template-columns"], "repeat(2,minmax(0,1fr))");
const completenessCard = lastRule(".ai-first-review-summary .summary-card.warning");
assert.equal(completenessCard.display, "grid");
assert.equal(completenessCard["grid-template-columns"], "minmax(0,1fr)");
assert.equal(completenessCard["align-items"], "stretch");
const completenessValue = lastRule(".ai-first-review-summary .summary-card.warning b");
assert.equal(completenessValue["font-size"], "17px");
assert.equal(completenessValue["min-width"], "0");
assert.equal(completenessValue["overflow-wrap"], "anywhere");

assert.match(styles, /\.ai-first-table-scroll\s*\{[^}]*overflow:auto/s);
assert.match(styles, /\.ai-first-table th\.selector,\.ai-first-table td\.selector\s*\{\s*position:sticky;\s*left:0;/);
assert.match(styles, /\.ai-first-table th:nth-child\(2\),\.ai-first-table td:nth-child\(2\)\s*\{\s*position:sticky;\s*left:var\(--ai-first-selector-column-width\);/);

console.log("AI-first toolbar layout CSS regressions passed.");
