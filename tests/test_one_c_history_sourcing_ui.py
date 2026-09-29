from pathlib import Path
import json
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _sources():
    return (
        (ROOT / "averon_import" / "templates" / "index.html").read_text(encoding="utf-8"),
        (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8"),
        (ROOT / "averon_import" / "static" / "styles.css").read_text(encoding="utf-8"),
    )


def _css_rule(css, selector):
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, f"missing CSS rule for {selector}"
    return match.group(1)


def test_sourcing_mode_controls_are_persistent_and_offer_the_three_routes():
    html, app, _css = _sources()

    controls = html.split('id="sourcing-modal"', 1)[1].split('id="sourcing-content"', 1)[0]
    assert 'id="sourcing-source-mode"' in controls
    assert '<option value="one_c_then_provider">История 1С → поставщик</option>' in controls
    assert '<option value="one_c_only">Только история 1С</option>' in controls
    assert '<option value="provider_only">Только поставщик</option>' in controls
    assert 'id="sourcing-history-status"' in controls
    assert 'id="sourcing-history-refresh"' in controls
    assert "sourceMode: \"provider_only\"" in app
    assert 'api("/api/sourcing/history-status")' in app
    assert 'await loadSourcingHistoryStatus();' in app
    assert 'openSourcingModal()' in app and 'if (!state.sourcing.historyStatusLoaded) void loadSourcingHistoryStatus()' in app
    assert '"#sourcing-source-mode").addEventListener("change",handleSourcingModeChange)' in app
    assert '"#sourcing-history-refresh").addEventListener("click",() => { void loadSourcingHistoryStatus(); })' in app
    assert 'option.disabled = option.value !== "provider_only" && !available' in app
    assert 'await Promise.all([loadOneCHistoryStatus(), loadSourcingHistoryStatus()])' in app
    assert "setInterval(" not in app[app.index("const SOURCING_MODE_LABELS"):app.index("async function loadOneCHistoryStatus")]
    sourcing_helpers = app[app.index("const SOURCING_MODE_LABELS"):app.index("async function loadOneCHistoryStatus")]
    assert "localStorage" not in sourcing_helpers and "sessionStorage" not in sourcing_helpers


def test_sourcing_mode_runtime_defaults_and_preserves_session_selection():
    _html, app, _css = _sources()

    def extract_function(name):
        async_marker = f"async function {name}("
        marker = async_marker if async_marker in app else f"function {name}("
        start = app.index(marker)
        ends = [
            index for marker in ("\nfunction ", "\nasync function ")
            if (index := app.find(marker, start + 1)) >= 0
        ]
        return app[start:min(ends) if ends else len(app)]

    functions = "\n".join(extract_function(name) for name in (
        "loadSourcingHistoryStatus",
        "openSourcingModal",
        "handleSourcingModeChange",
        "clearSourcingProtectedState",
    ))
    script = f"""
const vm = require('vm');
const context = {{
  authState: 'authenticated',
  status: {{available: true}},
  api: () => Promise.resolve(context.status),
  renderSourcingHistoryControls: () => {{}},
  SOURCING_MODE_LABELS: {{one_c_only: 'Только история 1С', one_c_then_provider: 'История 1С → поставщик', provider_only: 'Только поставщик'}},
  createSourcingState: () => ({{sourceMode: 'provider_only', historyStatus:null, historyStatusLoaded:false,
    historyStatusGeneration:0, historyStatusPromise:null, sourceModeTouched:false, result:null,
    modeChangedAfterResult:false}}),
  $: selector => selector === '#sourcing-modal'
    ? {{showModal: () => {{}}}}
    : {{options: [], value: '', textContent: '', hidden: false}},
  state: {{authState: 'authenticated', sourcing: null}},
}};
vm.createContext(context);
vm.runInContext({json.dumps(functions)}, context);
(async () => {{
  context.state.sourcing = context.createSourcingState();
  await context.loadSourcingHistoryStatus();
  const activeDefault = context.state.sourcing.sourceMode;

  context.state.sourcing = context.createSourcingState();
  context.status = {{available: false}};
  await context.loadSourcingHistoryStatus();
  const unavailableDefault = context.state.sourcing.sourceMode;

  context.state.sourcing = context.createSourcingState({{sourceMode: 'provider_only'}});
  context.status = {{available: true}};
  await context.loadSourcingHistoryStatus();
  context.handleSourcingModeChange({{target: {{value: 'one_c_then_provider'}}}});
  context.status = {{available: true, event_count: 20}};
  await context.loadSourcingHistoryStatus();
  await context.openSourcingModal();
  const touchedSelection = context.state.sourcing.sourceMode;

  context.handleSourcingModeChange({{target: {{value: 'provider_only'}}}});
  await context.openSourcingModal();
  const providerSelection = context.state.sourcing.sourceMode;

  context.state.sourcing.result = {{source_mode: 'provider_only'}};
  context.state.sourcing.sourceModeTouched = false;
  context.state.sourcing.sourceMode = 'one_c_then_provider';
  await context.loadSourcingHistoryStatus();
  const resultModeIdentity = context.state.sourcing.sourceMode;

  context.clearSourcingProtectedState();
  const cleared = {{
    sourceMode: context.state.sourcing.sourceMode,
    sourceModeTouched: context.state.sourcing.sourceModeTouched,
  }};
  context.status = {{available: true}};
  await context.loadSourcingHistoryStatus();
  console.log(JSON.stringify({{activeDefault, unavailableDefault, touchedSelection, providerSelection, resultModeIdentity, cleared, nextSessionDefault: context.state.sourcing.sourceMode}}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(["node", "-e", script], capture_output=True, check=True, text=True)
    actual = json.loads(completed.stdout)

    assert actual == {
        "activeDefault": "one_c_only",
        "unavailableDefault": "provider_only",
        "touchedSelection": "one_c_then_provider",
        "providerSelection": "provider_only",
        "resultModeIdentity": "one_c_then_provider",
        "cleared": {"sourceMode": "provider_only", "sourceModeTouched": False},
        "nextSessionDefault": "one_c_only",
    }


def test_sourcing_routes_are_sent_for_row_project_and_manual_project_requests():
    _html, app, _css = _sources()
    row_search = app.split("async function openSourcingForRow", 1)[1].split("async function runProjectSourcing", 1)[0]
    project_search = app.split("async function runProjectSourcing", 1)[1].split("async function openProjectSourcing", 1)[0]
    manual_search = app.split("async function openManualProjectSourcing", 1)[1].split("async function pollSourcingJob", 1)[0]

    assert "source_mode:sourceMode" in row_search
    assert "source_mode:sourceMode" in project_search
    assert "await runProjectSourcing(manualRowsForSourcing(), null)" in manual_search
    assert "requestGeneration" in row_search and "requestGeneration" in project_search
    assert "sourceModeTouched" in app and "modeChangedAfterResult" in app
    assert "Режим изменён. Запустите подбор повторно, чтобы применить." in _html
    assert "renderSourcingResult(result, row)" in row_search
    assert "sourcingResultMode(result)" in app


def test_history_results_are_distinct_from_live_provider_offers_and_totals():
    _html, app, _css = _sources()
    historical = app.split("function renderHistoricalOfferCard", 1)[1].split("function sourcingRouteExplanation", 1)[0]
    route_copy = app.split("function sourcingRouteExplanation", 1)[1].split("function renderSourcingRouteExplanation", 1)[0]
    result_renderer = app.split("function renderSourcingResult", 1)[1].split("async function openSourcingForRow", 1)[0]
    project_renderer = app.split("function renderProjectResultRow", 1)[1].split("function renderProjectSourcingList", 1)[0]
    price_renderer = app.split("function historicalOfferPrice", 1)[1].split("function historicalOfferDate", 1)[0]

    assert "История 1С" in historical
    assert "Дата закупки:" in historical and "Контрагент:" in historical
    assert "Текущая доступность не подтверждена." in historical
    assert "Валюта в истории не указана" in app
    assert "formatMoney(amount, currency)" in price_renderer
    assert "return currency" in price_renderer
    assert "Валюта в истории не указана" in price_renderer
    assert "history_purchase_date" in app.split("function historicalOfferDate", 1)[1].split("function historicalOfferCounterparty", 1)[0]
    assert "role=\"status\"" in app
    assert "В истории 1С нет безопасно подтверждённого совпадения, а поиск у поставщика завершился ошибкой." in route_copy
    assert "В истории 1С были варианты, требующие проверки; показан результат поставщика." in route_copy
    assert "Найдены варианты в истории 1С — требуется проверка" in result_renderer
    assert "Исторические цены 1С не включены в текущую стоимость поставщика." in result_renderer
    assert "positions_history_matched" in result_renderer
    assert "positions_provider_matched" in result_renderer
    assert "positions_fallback_called" in result_renderer
    assert "positions_history_review" in result_renderer
    assert "HISTORY_SAFE_MATCH" in app.split("const SOURCING_DECISION_LABELS", 1)[1].split("function sourcingDecisionLabel", 1)[0]
    assert 'final_source_kind === "historical_purchase"' in app.split("function projectDecision", 1)[1].split("function projectReason", 1)[0]
    assert 'Не рассчитывается</span><small class="project-result-secondary">для истории' in project_renderer
    assert 'historicalReview ? " · требуется проверка"' in project_renderer
    assert "Рекомендуемое предложение" in result_renderer  # provider-only legacy path remains intact


def test_history_mode_period_uses_reported_dates_and_auth_reset_scrubs_sourcing_state():
    _html, app, _css = _sources()
    status_start = app.index("function sourcingHistoryPeriod")
    status_end = app.index("function renderSourcingHistoryControls", status_start)
    status_code = app[status_start:status_end]
    reset_start = app.index("function clearSourcingProtectedState")
    reset_end = app.index("function clearOneCHistoryProtectedState", reset_start)
    reset_code = app[reset_start:reset_end]
    boot_start = app.index("async function boot()")
    boot_end = app.index("function updateCloudStatus", boot_start)
    boot_code = app[boot_start:boot_end]

    assert "status?.period_start" in status_code and "status?.period_end" in status_code
    assert "3 месяца" not in status_code and "90" not in status_code
    assert "state.sourcing = createSourcingState()" in reset_code
    assert "#sourcing-source-mode" in reset_code and 'select.value = "provider_only"' in reset_code
    assert "#sourcing-mode-change-note" in reset_code and "#sourcing-content" in reset_code
    assert "clearSourcingProtectedState()" in boot_code
    assert "sourceMode:" not in app.split("function clearSourcingProtectedState", 1)[1].split("function clearOneCHistoryProtectedState", 1)[0]


def test_sourcing_controls_and_historical_cards_fit_narrow_viewports():
    _html, _app, css = _sources()

    assert ".sourcing-mode-controls" in css and "min-width:0" in css
    assert ".sourcing-mode-field select { width:100%; min-width:0; max-width:100%; }" in css
    assert "@media (max-width:760px)" in css
    assert "@media (max-width:420px)" in css
    assert ".sourcing-route-coverage { grid-template-columns:repeat(2,minmax(0,1fr)); }" in css
    assert ".historical-offer-card.recommended" in css


def test_project_result_price_and_total_columns_wrap_without_hiding_facts():
    _html, app, css = _sources()
    price_total = _css_rule(css, ".project-result-price, .project-result-total")
    price_primary = _css_rule(css, ".project-result-price-primary")
    renderer = app.split("function renderProjectResultRow", 1)[1].split("function renderProjectSourcingList", 1)[0]
    price_renderer = app.split("function renderProjectPriceCell", 1)[1].split("function renderProjectResultRow", 1)[0]

    assert "white-space:normal" in price_total
    assert "overflow-wrap:anywhere" in price_total
    assert "min-width:0" in price_total and "line-height:1.4" in price_total
    assert "white-space:nowrap" not in price_total
    assert "overflow-wrap:anywhere" in price_primary
    assert "Валюта в истории не указана" in price_renderer
    assert "Не рассчитывается</span><small class=\"project-result-secondary\">для истории" in renderer
    assert "project-result-price" in renderer and "project-result-total" in renderer
    assert "text-overflow:ellipsis" not in price_total + price_primary
    assert "overflow:hidden" not in price_total + price_primary


def test_project_result_status_and_source_cells_wrap_with_internal_table_scroll():
    _html, app, css = _sources()
    status = _css_rule(css, ".project-result-status, .project-result-source")
    decision = _css_rule(css, ".project-result-status .sourcing-decision-label")
    list_scroll = _css_rule(css, ".sourcing-project-list")
    row_renderer = app.split("function renderProjectResultRow", 1)[1].split("function renderProjectSourcingList", 1)[0]
    price_renderer = app.split("function renderProjectPriceCell", 1)[1].split("function renderProjectResultRow", 1)[0]

    assert "min-width:0" in status and "overflow-wrap:anywhere" in status
    assert "white-space:normal" in decision and "overflow-wrap:anywhere" in decision
    assert "project-result-status" in row_renderer and "project-result-source" in row_renderer
    assert "Поставщик в истории:" in row_renderer
    assert "overflow-x:auto" in list_scroll
    assert "overflow-x:hidden" not in css
    assert "text-overflow:ellipsis" not in status
    assert "return formatMoney(offer?.price, offer?.currency)" in price_renderer
    assert 'sourcingProviderLabel(offer)' in row_renderer
