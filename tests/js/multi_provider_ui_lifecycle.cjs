const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(process.argv[2], "utf8");
function sliceBetween(startText, endText) {
  const start = source.indexOf(startText);
  const end = source.indexOf(endText, start);
  if (start < 0 || end <= start) throw new Error(`Could not locate ${startText}`);
  return source.slice(start, end);
}
function assert(condition, message) { if (!condition) throw new Error(message); }

async function testLazyCapabilitiesAndProtectedChoice() {
  const functions = sliceBetween("function resetExcelMultiProviderChoice() {", "function openSourcingModal() {")
    + sliceBetween("function sourcingModeLoadingCopy(mode) {", "async function loadOneCHistoryStatus()");
  const nodes = {
    "#multi-provider-panel": {hidden: false},
    "#multi-provider-toggle": {checked: false, disabled: false},
    "#multi-provider-options": {hidden: true, innerHTML: "", replaceChildren() { this.innerHTML = ""; }},
    "#multi-provider-warning": {hidden: true},
    "#multi-provider-admission": {hidden: true, textContent: ""},
  };
  const state = {
    authState: "authenticated", currentUser: {user_id: "user-a"},
    sourcing: {modalContext: "excel_tender", sourceMode: "provider_only", sourceModeTouched: false, result: null},
    excelTender: {
      workspace: {tender_id: "tender-a"}, sourcingActive: false, exportActive: false,
      multiProviderEnabled: false, multiProviderProviders: new Set(), multiProviderCapabilities: null,
      multiProviderCapabilitiesPromise: null, multiProviderCapabilitiesTenderId: null,
      multiProviderCapabilitiesUserId: null,
    },
  };
  const context = {
    capabilityRequests: 0, nodes,
    state, SOURCING_MODE_LABELS: {provider_only: "Поставщик", one_c_only: "История 1С", one_c_then_provider: "История 1С → поставщик"},
    $: selector => nodes[selector], escapeHtml: value => String(value),
    api: async () => {
      context.capabilityRequests += 1;
      return {enabled: true, source_mode: "provider_only", providers: [
        {key: "etm_ipro", label: "ЭТМ iPRO"}, {key: "lemana_b2b", label: "Лемана ПРО"},
      ]};
    },
    renderSourcingHistoryControls() {}, renderStaleSourcingHistoryWarning() {},
    sourcingResultMode: () => "provider_only", renderExcelMultiProviderPanel() {},
  };
  await vm.runInNewContext(`(async () => {
    ${functions}
    renderExcelMultiProviderPanel();
    if (capabilityRequests !== 0) throw new Error("Capabilities must be lazy until opt-in");
    if (nodes["#multi-provider-panel"].hidden || nodes["#multi-provider-toggle"].checked) throw new Error("Comparison must start visible and off in provider-only tender context");
    await handleExcelMultiProviderToggle({target:{checked:true}});
    if (capabilityRequests !== 1) throw new Error("Explicit opt-in should issue exactly one capabilities request");
    if (!nodes["#multi-provider-options"].innerHTML.includes("lemana_b2b")) throw new Error("Provider choices must come from capabilities");
    handleExcelMultiProviderSelection({target:{dataset:{multiProviderKey:"etm_ipro"},checked:true}});
    handleExcelMultiProviderSelection({target:{dataset:{multiProviderKey:"lemana_b2b"},checked:true}});
    if (state.excelTender.multiProviderProviders.size !== 2) throw new Error("Both explicit provider choices should be retained");
    state.sourcing.historyStatus = {available:true};
    handleSourcingModeChange({target:{value:"one_c_only"}});
    if (state.excelTender.multiProviderEnabled || state.excelTender.multiProviderProviders.size) throw new Error("Switching to history must clear comparison choice");
    if (!nodes["#multi-provider-panel"].hidden) throw new Error("Comparison must be hidden in history mode");
    await loadExcelMultiProviderCapabilities("tender-a");
    resetExcelMultiProviderState();
    if (state.excelTender.multiProviderCapabilities !== null || state.excelTender.multiProviderCapabilitiesTenderId !== null) throw new Error("Protected capability cache must clear with workspace/session state");
  })()`, context);
  assert(context.capabilityRequests === 1, "Capability request count must stay lazy and cached");
}

async function testSelectionCapAndExplicitRequestHandshake() {
  const functionSource = sliceBetween("async function startExcelTenderSourcing() {", "function parseManualPaste(text)");
  async function submitWithCount(count, enabled = true) {
    const calls = [];
    const admission = {hidden: true, textContent: ""};
    const modeSelect = {disabled: false};
    const nodes = {
      "#multi-provider-admission": admission,
      "#sourcing-source-mode": modeSelect,
      "#sourcing-modal": {open:true},
      "#sourcing-subtitle": {textContent:""},
      "#sourcing-content": {innerHTML:""},
    };
    const state = {excelTender: {
      workspace:{tender_id:"tender"}, sourcingActive:false, exportActive:false, selectedIds:new Set(Array.from({length:count}, (_, i) => `row-${i}`)), pollGeneration:1,
      multiProviderEnabled:enabled, multiProviderProviders:new Set(["lemana_b2b", "etm_ipro"]), multiProviderCapabilities:{enabled:true},
    }, sourcing:{sourceMode:"provider_only"}};
    const context = {
      state, $: selector => nodes[selector],
      setSourcingModalPhase() {}, renderExcelTenderRows() {},
      sourcingModeLoadingCopy: () => ({title:"Сравнение поставщиков"}),
      api: async (_url, options) => { calls.push(JSON.parse(options.body)); return {id:"job-id"}; },
      pollExcelTenderJob: async () => {}, encodeURIComponent,
    };
    await vm.runInNewContext(`(async () => { ${functionSource}; await startExcelTenderSourcing(); })()`, context);
    return {calls, admission, state};
  }
  const tooMany = await submitWithCount(101);
  assert(tooMany.calls.length === 0, "101 selected rows must be blocked before POST");
  assert(tooMany.admission.textContent.includes("больше 100") && !tooMany.admission.hidden, "101-row warning should be visible without truncation");
  const max = await submitWithCount(100);
  assert(max.calls.length === 1, "Exactly 100 selected rows should be admitted");
  assert(max.calls[0].source_row_ids.length === 100, "The 100-row request must not discard rows");
  assert(max.calls[0].providers.join(",") === "etm_ipro,lemana_b2b", "The explicit provider handshake should be canonical");
  assert(max.calls[0].limit === 20 && !("provider" in max.calls[0]), "M4A request should cap at 20 and omit the scalar legacy provider");
  const legacy = await submitWithCount(1, false);
  assert(legacy.calls.length === 1 && !("providers" in legacy.calls[0]), "The legacy request must omit the multi-provider handshake when opt-in is off");
}

function testCompositeIdentityAndFailedRendering() {
  const renderer = sliceBetween("function renderMultiProviderPublicRun(run) {", "async function openExcelTenderRunDetail(runId = null)");
  const node = {innerHTML:""};
  const subtitle = {textContent:""};
  const context = {$: selector => selector === "#sourcing-content" ? node : selector === "#sourcing-subtitle" ? subtitle : {open:false}, setSourcingModalPhase() {}, escapeHtml: value => String(value)};
  const run = {
    status:"failed", selected_positions:1, evaluated_positions:1, safe_winner_rows:1, no_safe_winner_rows:0, partial_failure_rows:1,
    rows:[{excel_row:8, partial_failure:true,
      provider_outcomes:[
        {provider_key:"etm_ipro",state:"PARTIAL_SUCCESS",offers_returned_count:7,retained_offer_count:1},
        {provider_key:"lemana_b2b",state:"SUCCESS",offers_returned_count:1,retained_offer_count:1},
      ],
      offers:[
        {provider_key:"etm_ipro",offer_id:"same-id",title:"ETM winner",price:"0.123456789012345678901",currency:"RUB",price_unit:"шт",availability:null,availability_text:"",url:"https://etm.example/item"},
        {provider_key:"lemana_b2b",offer_id:"same-id",title:"Lemana candidate",price:"9.50",currency:"RUB",price_unit:"шт",availability:true,availability_text:"В наличии",url:"https://lemana.example/item"},
      ],
      matches:[{provider_key:"etm_ipro",offer_id:"same-id",decision:"MATCH"},{provider_key:"lemana_b2b",offer_id:"same-id",decision:"ALTERNATIVE"}],
      commercial_evidence:[{provider_key:"etm_ipro",offer_id:"same-id",state:"COMPLETE",vat_basis:"GROSS_INCLUDING_VAT"},{provider_key:"lemana_b2b",offer_id:"same-id",state:"INCOMPLETE",vat_basis:"UNKNOWN"}],
      commercial_selection:{state:"SELECTED",selected_reference:{provider_key:"etm_ipro",offer_id:"same-id"},candidate_references:[{provider_key:"etm_ipro",offer_id:"same-id"},{provider_key:"lemana_b2b",offer_id:"same-id"}],selection_basis:"SOLE_STRONGEST_IDENTITY",reason_codes:[]},
    }],
  };
  vm.runInNewContext(`${renderer}; renderMultiProviderPublicRun(${JSON.stringify(run)});`, context);
  assert(subtitle.textContent.includes("завершился ошибкой после сохранения безопасно обработанных строк"), "Persisted FAILED runs should render their terminal state");
  assert(node.innerHTML.includes("Поставщик вернул 7 предложений. Показаны варианты, участвовавшие в решении."), "Returned and retained counts should be distinguished");
  assert(node.innerHTML.includes("Коммерческая проверка нужна"), "Lemana evidence should explain the verification requirement");
  assert(node.innerHTML.includes("0.123456789012345678901"), "Exact decimal text should be rendered without numeric coercion");
  assert(node.innerHTML.includes('<article class="multi-provider-offer is-selected"><h4>ЭТМ iPRO: ETM winner'), "Winner identity must include provider key when offer IDs collide");
  assert(node.innerHTML.includes('<article class="multi-provider-offer "><h4>Лемана ПРО: Lemana candidate'), "Same-ID other-provider offer must remain a separate candidate");
}

async function testSavedV2RunDoesNotRequestHistoryDecisions() {
  const loader = sliceBetween("async function openExcelTenderRunDetail(runId = null) {", "async function refreshExcelTenderRuns(");
  const calls = [];
  const run = {schema_version:2,run_kind:"multi_provider",status:"failed",rows:[]};
  const dialog = {open:false,showModal(){this.open=true;}};
  const nodes = {"#sourcing-modal":dialog,"#sourcing-subtitle":{textContent:""},"#sourcing-content":{innerHTML:""}};
  let rendered = 0;
  const context = {
    state:{excelTender:{workspace:{tender_id:"tender"},latestRuns:[{run_id:"run-v2",schema_version:2,run_kind:"multi_provider",status:"failed"}]}},
    $: selector => nodes[selector], setSourcingModalPhase() {}, encodeURIComponent,
    api:async url => { calls.push(url); return run; },
    renderMultiProviderPublicRun:() => { rendered += 1; },
  };
  await vm.runInNewContext(`(async () => { ${loader}; await openExcelTenderRunDetail("run-v2"); })()`, context);
  assert(calls.length === 1 && !calls[0].includes("history-decisions"), "Opening saved v2 must request detail only");
  assert(rendered === 1, "Saved v2 detail should use the dedicated renderer");
  const source = sliceBetween("function renderExcelTenderExportControls() {", "async function refreshExcelTenderExports(");
  assert(source.includes('run.run_kind !== "multi_provider" && run.schema_version !== 2'), "V2 runs must stay outside XLSX run options");
}

Promise.all([
  testLazyCapabilitiesAndProtectedChoice(),
  testSelectionCapAndExplicitRequestHandshake(),
  testSavedV2RunDoesNotRequestHistoryDecisions(),
]).then(() => {
  testCompositeIdentityAndFailedRendering();
  process.stdout.write("PASS: M4A lazy opt-in, provider-only request lifecycle, safe v2 rendering, and saved-run retrieval\n");
}).catch(error => { process.stderr.write(`${error.stack || error}\n`); process.exitCode = 1; });
