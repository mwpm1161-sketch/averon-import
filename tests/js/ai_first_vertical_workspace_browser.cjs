const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {chromium} = require("playwright");

const [baseUrl, fixturePath, outputDir] = process.argv.slice(2);
assert(baseUrl && fixturePath && outputDir, "Usage: node ai_first_vertical_workspace_browser.cjs <ASGI URL> <private synthetic fixture.json> <screenshot directory>");
const fixture = JSON.parse(fs.readFileSync(fixturePath, "utf8"));
const baselineMode = process.env.AVERON_LAYOUT_BASELINE === "1";
const executablePath = process.env.AVERON_TEST_BROWSER || "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const measurements = [];

async function geometry(page) {
  return page.evaluate(() => {
    const rect = (selector) => {
      const element = document.querySelector(selector), value = element.getBoundingClientRect();
      return {top:value.top,bottom:value.bottom,left:value.left,right:value.right,width:value.width,height:value.height,
        clientHeight:element.clientHeight,scrollHeight:element.scrollHeight,scrollTop:element.scrollTop,scrollLeft:element.scrollLeft,
        overflowX:getComputedStyle(element).overflowX,overflowY:getComputedStyle(element).overflowY};
    };
    const table = rect("#ai-first-table-scroll"), panel = rect(".ai-first-table-panel");
    const stickyHeader = rect(".topbar");
    const visibleTop = Math.max(table.top,panel.top,stickyHeader.bottom,0);
    const visibleHeight = Math.max(0,Math.min(table.bottom,panel.bottom,innerHeight)-visibleTop);
    const overlaps = (selector) => {
      const container=document.querySelector(selector), children=[...container.children].filter(element=>{const r=element.getBoundingClientRect();return r.width>0&&r.height>0;});
      const result=[];
      for(let i=0;i<children.length;i++) for(let j=i+1;j<children.length;j++) {
        const a=children[i].getBoundingClientRect(),b=children[j].getBoundingClientRect();
        if(Math.min(a.right,b.right)-Math.max(a.left,b.left)>1&&Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1) result.push([children[i].className||children[i].tagName,children[j].className||children[j].tagName]);
      }
      return result;
    };
    const within = (parentSelector, childSelector) => {
      const parent=document.querySelector(parentSelector).getBoundingClientRect();
      return [...document.querySelectorAll(childSelector)].filter(element=>element.getClientRects().length).every(element=>{
        const r=element.getBoundingClientRect();return r.left>=parent.left-1&&r.right<=parent.right+1&&r.top>=parent.top-1&&r.bottom<=parent.bottom+1;
      });
    };
    const issueList=rect("#ai-first-page-issues");
    return {viewport:{width:innerWidth,height:innerHeight},document:{width:document.documentElement.scrollWidth,clientWidth:document.documentElement.clientWidth,
        height:document.documentElement.scrollHeight,clientHeight:document.documentElement.clientHeight,scrollTop:document.documentElement.scrollTop},
      workspace:rect(".ai-first-review-workspace"),panel,stickyHeader,issues:rect("#ai-first-page-issues"),controls:rect("#ai-first-review-controls"),
      table,footer:rect("#ai-first-review-footer"),visibleTableHeight:visibleHeight,rows:document.querySelectorAll("#ai-first-table-scroll tr[data-ai-first-row]").length,
      sourceColumns:document.querySelectorAll("#ai-first-table-scroll table thead th").length-4,
      sourceCells:document.querySelectorAll("#ai-first-table-scroll [data-ai-first-cell]").length,
      issueCount:document.querySelectorAll("#ai-first-page-issues .ai-first-issue").length,
      issueScrollable:issueList.scrollHeight>issueList.clientHeight&&getComputedStyle(document.querySelector("#ai-first-page-issues")).overflowY==="auto",
      verticalScrollable:table.scrollHeight>table.clientHeight,
      bulkOverlaps:overlaps("#ai-first-review-controls .ai-first-bulk-toolbar"),selectionOverlaps:overlaps("#ai-first-review-controls .ai-first-selection-actions"),
      bulkWithin:within("#ai-first-review-controls", "#ai-first-review-controls .ai-first-bulk-toolbar > *, #ai-first-review-controls .ai-first-selection-actions > *")};
  });
}

(async () => {
  fs.mkdirSync(outputDir,{recursive:true});
  const browser = await chromium.launch({headless:true,executablePath});
  try {
    const context = await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:1000,height:768}});
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(baseUrl);
    await page.locator("#auth-login-username").fill("staging-owner");
    await page.locator("#auth-login-password").fill(fixture.password);
    await page.locator("#auth-login-submit").click();
    await page.waitForFunction(() => state.authState === "authenticated" && state.bootComplete);

    async function openCase(key, issueCount, width, height=768) {
      await page.setViewportSize({width,height});
      await page.evaluate(documentId => openExistingDocument(documentId),fixture.documents[`layout_${key}`]);
      await page.locator("#ai-first-open-saved").click();
      await page.waitForFunction(() => state.aiFirst.data?.draft?.pages?.length && document.querySelectorAll("#ai-first-table-scroll tr[data-ai-first-row]").length === 25);
      await page.waitForFunction(() => {const image=document.querySelector("#ai-first-pdf-image");return image.complete&&image.naturalWidth>0;});
      await page.evaluate(() => {const table=document.querySelector("#ai-first-table-scroll"),header=document.querySelector(".topbar");const top=table.getBoundingClientRect().top+scrollY;window.scrollTo(0,top-header.getBoundingClientRect().height-8);});
      const metrics=await geometry(page);
      measurements.push({case:issueCount,width,height,visibleTableHeight:metrics.visibleTableHeight,workspaceHeight:metrics.workspace.height,
        tableClientHeight:metrics.table.clientHeight,panelHeight:metrics.panel.height,controlsHeight:metrics.controls.height,
        issueViewportHeight:metrics.issues.clientHeight,documentHeight:metrics.document.height,documentWidth:metrics.document.width});
      fs.writeFileSync(path.join(outputDir,"measurements.json"),JSON.stringify({measurements},null,2));
      if (!baselineMode&&issueCount>=5) {
        const issueRoot=page.locator("#ai-first-page-issues");
        await issueRoot.evaluate(element=>element.scrollTop=element.scrollHeight);
        const lastIssue=await issueRoot.locator(".ai-first-issue").last().boundingBox(), issuePanel=await issueRoot.boundingBox();
        assert(lastIssue.y>=issuePanel.y-1&&lastIssue.y+lastIssue.height<=issuePanel.y+issuePanel.height+1,`${issueCount} issue details: the last item can be reached by scrolling`);
        await issueRoot.evaluate(element=>element.scrollTop=0);
      }
      await page.evaluate(() => document.querySelectorAll("#toast-root .toast.success").forEach(node=>node.remove()));
      const screenshotName=`issues-${issueCount}-${width}x${height}`;
      await page.screenshot({path:path.join(outputDir,`${screenshotName}-viewport.png`)});
      await page.evaluate(() => window.scrollTo(0,0));
      await page.screenshot({path:path.join(outputDir,`${screenshotName}-full.png`),fullPage:true});
      assert.equal(metrics.rows,25,`${issueCount} issues: 25 specification rows`);
      assert(metrics.sourceColumns===9&&metrics.sourceCells===225,`${issueCount} issues: nine source columns and 225 editable cells`);
      assert.equal(metrics.issueCount,issueCount,`${issueCount} open issues are present`);
      if (!baselineMode) assert(metrics.visibleTableHeight>=180,`${issueCount} issues at ${width}x${height}: visible table height ${metrics.visibleTableHeight}px; expected at least 180px`);
      assert(metrics.document.width<=metrics.document.clientWidth,`${issueCount} issues: no page-wide horizontal scroll`);
      assert(metrics.table.overflowX==="auto"&&metrics.table.overflowY==="auto","table keeps independent horizontal and vertical scrolling");
      if (!baselineMode) assert(metrics.bulkOverlaps.length===0&&metrics.selectionOverlaps.length===0,`${issueCount} issues at ${width}px: bulk actions wrap without overlap`);
      if (!baselineMode) assert(metrics.bulkWithin,`${issueCount} issues at ${width}px: bulk controls are not clipped`);
      if (!baselineMode) assert(metrics.verticalScrollable,`${issueCount} issues at ${width}px: table keeps independent vertical scrolling`);
      if (!baselineMode&&issueCount>=5) assert(metrics.issueScrollable,`${issueCount} issue details remain reachable in their scroll region`);
      return metrics;
    }

    await openCase("0",0,1000);
    await openCase("5",5,1000);
    await openCase("80",80,1000);
    await openCase("80",80,1280,900);
    await openCase("80",80,1440,900);

    if (baselineMode) {
      assert(measurements.some(item=>item.visibleTableHeight<180),"baseline reproduces the clipped table viewport");
      fs.writeFileSync(path.join(outputDir,"measurements.json"),JSON.stringify({status:"BASELINE_REPRODUCED",measurements},null,2));
      console.log(JSON.stringify({status:"BASELINE_REPRODUCED",measurements}));
      return;
    }

    const pdfImage=page.locator("#ai-first-pdf-image");
    const initialZoom=await page.locator("#ai-first-zoom-label").textContent();
    await page.locator("#ai-first-zoom-100").click();
    await page.locator("#ai-first-zoom-in").click();
    assert.notEqual(await page.locator("#ai-first-zoom-label").textContent(),initialZoom,"PDF zoom controls remain interactive");
    assert((await pdfImage.evaluate(image=>image.naturalWidth))>0,"PDF page remains rendered");

    const root=page.locator("#ai-first-table-scroll");
    const before=await page.evaluate(() => {
      const scroller=document.querySelector("#ai-first-table-scroll");
      scroller.scrollLeft=0;
      const first=scroller.querySelector("tbody tr"), left=[first.cells[0].getBoundingClientRect().left,first.cells[1].getBoundingClientRect().left];
      scroller.scrollLeft=scroller.scrollWidth;
      const cells=[...first.cells], last=cells[cells.length-1].getBoundingClientRect();
      return {root:scroller.getBoundingClientRect().toJSON(),left,right:[cells[0].getBoundingClientRect().left,cells[1].getBoundingClientRect().left],
        last:{left:last.left,right:last.right},max:scroller.scrollLeft};
    });
    assert(before.max>0,"the original columns have horizontal overflow");
    assert(await page.evaluate(() => document.documentElement.scrollWidth<=document.documentElement.clientWidth),"horizontal table scroll does not create page-wide overflow");
    assert(Math.abs(before.left[0]-before.right[0])<=1&&Math.abs(before.left[1]-before.right[1])<=1,"both sticky columns stay fixed while the last source column is reached");
    assert(before.last.left>=before.root.left&&before.last.right<=before.root.right,"last source column is visible within the table scroller");

    await root.evaluate(element=>{element.scrollTop=0;element.scrollLeft=0;});
    const lastRow=page.locator("#ai-first-table-scroll tbody tr[data-ai-first-row]").last();
    await lastRow.scrollIntoViewIfNeeded();
    const lastRowRect=await lastRow.boundingBox(), rootRect=await root.boundingBox();
    assert(lastRowRect.y+lastRowRect.height>rootRect.y&&lastRowRect.y<rootRect.y+rootRect.height,"last specification row is reachable in the vertical scroller");
    const selectedId=await lastRow.getAttribute("data-ai-first-row");
    await lastRow.locator(`[data-ai-first-open-row="${selectedId}"]`).click();
    await page.waitForFunction(id=>state.aiFirst.inspectorRowId===id,selectedId);
    const inspector=await page.locator("#ai-first-row-inspector").boundingBox();
    const pane=await page.locator(".ai-first-table-panel").boundingBox();
    assert(inspector.height>=180&&inspector.y>=pane.y&&inspector.y+inspector.height<=pane.y+pane.height,"inspector stays in the growing table pane");
    await page.locator("#ai-first-row-inspector").screenshot({path:path.join(outputDir,"inspector-last-row.png")});

    const footerLocator=page.locator("#ai-first-review-footer");
    await footerLocator.scrollIntoViewIfNeeded();
    const footer=await footerLocator.boundingBox();
    const viewportHeight=await page.evaluate(() => innerHeight);
    assert(footer.height>0&&footer.y<viewportHeight&&footer.y+footer.height>0,"footer remains reachable by natural document scrolling");
    assert.equal(errors.length,0,`browser JavaScript errors: ${errors.join("; ")}`);
    fs.writeFileSync(path.join(outputDir,"measurements.json"),JSON.stringify({checks:63,measurements},null,2));
    console.log(JSON.stringify({status:baselineMode?"BASELINE_REPRODUCED":"PASS",checks:63,measurements}));
  } finally {await browser.close();}
})().catch(error=>{console.error(error.stack||error);process.exitCode=1;});
