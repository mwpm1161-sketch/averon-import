import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "averon_import" / "static" / "styles.css").read_text(encoding="utf-8")
HTML = (ROOT / "averon_import" / "templates" / "index.html").read_text(encoding="utf-8")


def _function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    next_function = source.find("\nfunction ", start + 1)
    next_async_function = source.find("\nasync function ", start + 1)
    endings = [position for position in (next_function, next_async_function) if position >= 0]
    end = min(endings) if endings else len(source)
    return source[start:end]


def test_document_trash_is_admin_capability_gated_and_accessible():
    render = _function(APP_JS, "renderRecentDocuments")

    assert 'state.currentUser?.capabilities?.document_management === true' in render
    assert 'class="button ghost recent-document-trash"' in render
    assert 'aria-label="Удалить документ"' in render
    assert "openDeleteDocumentDialog(button.dataset.documentId" in render
    assert '.recent-document-trash { width:36px; height:36px;' in CSS


def test_delete_requires_explicit_dialog_confirmation_and_reports_freed_bytes():
    delete = _function(APP_JS, "deleteSelectedDocument")

    assert 'method:"DELETE"' in delete
    assert '`/api/admin/documents/${encodeURIComponent(target.documentId)}`' in delete
    assert 'bytes(result?.freed_bytes)' in delete
    assert 'id="delete-document-modal"' in HTML
    assert 'id="confirm-delete-document"' in HTML
    assert "Восстановить удалённый документ будет нельзя." in HTML
    assert 'id="delete-document-name"' in HTML
    assert 'id="delete-document-name" type="text"' not in HTML


def test_deleted_current_document_is_cleared_without_dirty_confirmation():
    clear = _function(APP_JS, "clearDeletedCurrentDocument")

    assert 'if (state.document?.document_id !== documentId) return false' in clear
    assert 'document:null' in clear
    assert 'dirty:false' in clear
    assert 'state.rows:[]' not in clear
    assert '$("#result-body").innerHTML = ""' in clear
    assert '$("#sourcing-content").innerHTML =' in clear
    assert 'localStorage.getItem("averonCurrentDocument") === documentId' in clear
    assert 'setView("upload")' in clear
    assert 'confirm(' not in clear


def test_missing_open_document_is_explained_as_admin_deletion():
    api = _function(APP_JS, "api")

    assert 'message === "Документ не найден"' in api
    assert 'apiError.documentDeleted = true' in api
    assert 'Документ был удалён администратором.' in api
    assert 'clearDeletedCurrentDocument(missingDocumentId, {notify:false})' in api


def test_cleanup_pending_clears_only_the_deleted_current_document_and_warns():
    start = APP_JS.index("async function deleteSelectedDocument(")
    end = APP_JS.index("\nasync function openExistingDocument(", start)
    functions = APP_JS[start:end]
    script = f"""
const vm = require('vm');
const functions = {json.dumps(functions)};
async function exercise(currentId) {{
  const targetId = 'a'.repeat(32);
  const storage = new Map([['averonCurrentDocument', currentId]]);
  const nodes = new Map();
  const messages = [];
  const context = {{
    state: {{
      document: {{document_id: currentId}}, dirty: true, sourcing: {{projectFilter:'all'}},
      tableSearchTimer: null, pendingDocumentDelete: {{documentId:targetId}},
      recentDocuments: [{{document_id:targetId}}, {{document_id:'b'.repeat(32)}}],
      currentUser: {{capabilities:{{document_management:true}}}},
    }},
    $: selector => {{
      if (!nodes.has(selector)) nodes.set(selector, {{close(){{this.closed=true;}}, removeAttribute(){{}}}});
      return nodes.get(selector);
    }},
    localStorage: {{getItem:key => storage.get(key) || null, removeItem:key => storage.delete(key)}},
    api: async () => {{throw Object.assign(new Error('cleanup pending'), {{
      code:'DOCUMENT_REMOVED_CLEANUP_PENDING', status:500,
    }});}},
    loadRecentDocuments: async () => {{throw new Error('reload unavailable');}},
    renderRecentDocuments: () => {{context.renderedRecent += 1;}},
    renderedRecent: 0,
    toast: (message, type) => messages.push({{message, type}}),
    cancelDocumentNavigation(){{}}, clearExportError(){{}}, rebuildResultIndexes(){{}},
    resetPageInspection(){{}}, updatePageInspectionOpenButton(){{}}, setView(){{}}, setZoom(){{}},
    clearTimeout(){{}},
  }};
  vm.createContext(context);
  vm.runInContext(functions, context);
  await context.deleteSelectedDocument();
  return {{
    documentId:context.state.document?.document_id || null,
    storageId:storage.get('averonCurrentDocument') || null,
    pending:context.state.pendingDocumentDelete,
    recentIds:context.state.recentDocuments.map(item => item.document_id),
    dialogClosed:Boolean(context.$('#delete-document-modal').closed),
    warning:messages[0] || null,
    renderedRecent:context.renderedRecent,
  }};
}}
Promise.all([exercise('a'.repeat(32)), exercise('c'.repeat(32))])
  .then(results => console.log(JSON.stringify(results)));
"""
    completed = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, encoding="utf-8"
    )
    assert completed.returncode == 0, completed.stderr
    current_document, different_document = json.loads(completed.stdout)

    assert current_document["documentId"] is None
    assert current_document["storageId"] is None
    assert current_document["pending"] is None
    assert current_document["recentIds"] == ["b" * 32]
    assert current_document["dialogClosed"]
    assert current_document["warning"] == {
        "message": "Документ удалён, но часть файлов не удалось очистить. Сообщите администратору.",
        "type": "warning",
    }
    assert current_document["renderedRecent"] == 1

    assert different_document["documentId"] == "c" * 32
    assert different_document["storageId"] == "c" * 32
    assert different_document["pending"] is None
    assert different_document["recentIds"] == ["b" * 32]
    assert different_document["dialogClosed"]
    assert different_document["warning"]["type"] == "warning"


def test_cleanup_pending_has_a_distinct_warning_toast_style():
    assert ".toast.warning { background:#8b5a0a; }" in CSS
