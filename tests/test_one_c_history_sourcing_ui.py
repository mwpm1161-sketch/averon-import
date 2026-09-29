from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _sources():
    return (
        (ROOT / "averon_import" / "templates" / "index.html").read_text(encoding="utf-8"),
        (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8"),
        (ROOT / "averon_import" / "static" / "styles.css").read_text(encoding="utf-8"),
    )


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

    assert "История 1С" in historical
    assert "Дата закупки:" in historical and "Контрагент:" in historical
    assert "Текущая доступность не подтверждена." in historical
    assert "Валюта в истории не указана" in app
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
    assert "Не рассчитывается для истории" in project_renderer
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
