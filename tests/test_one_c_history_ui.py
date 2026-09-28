from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_admin_settings_has_bounded_one_c_import_controls():
    template = (ROOT / "averon_import" / "templates" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="one-c-history-file" type="file"' in template
    assert 'id="one-c-history-preview"' in template
    assert 'id="one-c-history-analyze"' in template
    assert 'id="one-c-history-import" disabled' in template
    assert 'id="one-c-history-status"' in template
    assert 'id="one-c-history-mappings"' in template
    assert 'id="one-c-history-group-mappings"' in template
    assert 'id="one-c-history-group-header-row"' in template
    assert 'id="one-c-history-sample"' in template
    assert "Номер/код номенклатуры" in script
    assert '"item_code", "Номер/код номенклатуры", true' in script
    assert "стабильный код номенклатуры" in script
    assert "идентификация одинаковых позиций будет менее надёжной" in script
    assert "физических строк" in script
    assert "контрагентов" in script
    assert "единиц" in script
    assert ".slice(0, 12)" in script


def test_one_c_history_protected_state_is_scrubbed_on_logout_expiry_and_identity_change():
    script = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    clear_start = script.index("function clearOneCHistoryProtectedState()")
    clear_end = script.index("function isCurrentOneCHistoryRequest", clear_start)
    clear_code = script[clear_start:clear_end]
    memory_start = script.index("function clearProtectedMemory()")
    memory_end = script.index("function clearOneCHistoryProtectedState()", memory_start)
    memory_code = script[memory_start:memory_end]
    boot_start = script.index("async function boot()")
    boot_end = script.index("function updateCloudStatus", boot_start)
    boot_code = script[boot_start:boot_end]
    one_c_start = script.index("async function loadOneCHistoryStatus()")
    one_c_end = script.index("function updateSourcingProviderFields", one_c_start)
    one_c_code = script[one_c_start:one_c_end]

    assert "clearOneCHistoryProtectedState();" in memory_code
    assert '"#one-c-history-file"' in clear_code
    assert '"#one-c-history-group-mappings"' in clear_code
    assert '"#one-c-history-mappings"' in clear_code
    assert '"#one-c-history-sample"' in clear_code
    assert '"#one-c-history-preview-panel"' in clear_code
    assert '"#one-c-history-profile-name"' in clear_code
    assert "previousUser.role" in boot_code and "clearOneCHistoryProtectedState()" in boot_code
    assert "requestGeneration" in clear_code
    assert "isCurrentOneCHistoryRequest(generation)" in one_c_code
    assert "localStorage" not in one_c_code
    assert "sessionStorage" not in one_c_code


def test_one_c_history_ui_only_loads_on_open_or_explicit_action_and_does_not_persist_upload_data():
    script = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    start = script.index("const ONE_C_HISTORY_FIELDS")
    end = script.index("function updateSourcingProviderFields", start)
    code = script[start:end]

    assert 'loadOneCHistoryStatus();' in script[script.index('$("#settings-button").addEventListener'):]
    assert '$("#one-c-history-refresh").addEventListener("click",loadOneCHistoryStatus)' in script
    assert "setInterval(" not in code
    assert "localStorage" not in code
    assert "sessionStorage" not in code
    assert not re.search(r"loadOneCHistoryStatus\(\);\s*\n\s*boot", code)


def test_one_c_history_settings_layout_constrains_controls_and_keeps_table_scroll_local():
    css = (ROOT / "averon_import" / "static" / "styles.css").read_text(encoding="utf-8")

    assert ".settings-card { min-width:0; max-width:calc(100vw - 32px);" in css
    assert ".settings-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr));" in css
    assert css.count("grid-template-columns:repeat(auto-fit,minmax(min(230px,100%),1fr))") == 3
    assert ".one-c-history-panel input,.one-c-history-panel select { width:100%; min-width:0; max-width:100%; }" in css
    assert ".one-c-history-panel button { min-width:0; max-width:100%; white-space:normal; overflow-wrap:anywhere; }" in css
    assert ".one-c-history-actions { justify-content:flex-end; flex-wrap:wrap;" in css
    assert ".one-c-history-sample-wrap { overflow:auto;" in css
    assert ".one-c-history-panel { overflow-x:" not in css


def test_one_c_history_ui_surfaces_post_commit_warnings_as_success():
    script = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    start = script.index("async function importOneCHistory()")
    end = script.index("async function", start + 10)
    import_code = script[start:end]

    assert "Array.isArray(result.warnings)" in import_code
    assert "result.status === \"succeeded\"" in import_code
    assert "postCommitWarnings.join(\" \")" in import_code
    assert 'toast(`${importMessage}${warningSuffix}`, "success")' in import_code
