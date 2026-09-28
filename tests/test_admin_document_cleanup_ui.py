from pathlib import Path


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
