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
    assert 'id="one-c-history-sample"' in template
    assert "Номер/код номенклатуры" in script
    assert '"item_code", "Номер/код номенклатуры", true' in script
    assert "стабильный код номенклатуры" in script
    assert "идентификация одинаковых позиций будет менее надёжной" in script
    assert "физических строк" in script
    assert "контрагентов" in script
    assert "единиц" in script
    assert ".slice(0, 12)" in script


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
