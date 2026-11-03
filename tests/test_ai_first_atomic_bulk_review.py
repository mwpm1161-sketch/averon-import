"""Synthetic atomic-batch regressions. No cloud calls or customer data."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from averon_import.services.ai_first.bulk_review import BulkReviewError, BulkReviewRequest, execute_bulk_review
from averon_import.services.ai_first.contracts import AIDocumentDraft, AIReviewEvent, AIReviewIssue, AIReviewState
from averon_import.services.ai_first.editor_draft import EditorEdits, review_token, save_editor
from averon_import.services.ai_first.local_workflow import AIWorkspaceStore
from averon_import.services.ai_first.review import AIReviewError, assert_review_ready_for_export, new_review_state, record_structure_change
from averon_import.services.auth import CurrentUser, Role
from test_ai_first_foundation import _cell, _draft, _payload, _row
from test_ai_first_user_workflow import ai_first_client, _make_document, _start_fake, _ApiClient


def setup_state(tmp_path, count=3):
    draft = _draft(_payload(rows=[_row(index, str(index + 1), f"Synthetic item {index + 1}") for index in range(count)]))
    state = new_review_state(draft)
    state.resolved_issue_ids = {issue.id for issue in [*draft.issues, *(i for p in draft.pages for i in p.issues)]}
    store = AIWorkspaceStore(tmp_path / "document")
    store.write_many({"transcription.json": draft.model_dump(mode="json"), "review.json": state.model_dump(mode="json")})
    return store, draft, state


def request_for(draft, state, ids=None, *, editor=None, role="standalone_item", **overrides):
    rows = [(p, t, r) for p in draft.pages for t in p.tables for r in t.rows]
    ids = ids or [r.id for _, _, r in rows]
    selected = [(p, t, r) for p, t, r in rows if r.id in ids]
    body = {"mode": "commit", "operation_id": uuid4().hex, "review_token": review_token(draft, state, editor),
            "draft_revision_id": draft.revision_id, "editor_revision": (editor or {}).get("revision", 0),
            "record_ids": ids, "role": role, "intent": "exclude_from_positions" if role in {"header", "column_numbering", "section", "continuation", "note"} else "confirm_values",
            "parents": {}, "provenance": {r.id: {"page_number": p.page_number, "extraction_fingerprint": p.extraction_fingerprint, "table_id": t.id} for p, t, r in selected},
            "values": {r.id: {f"cell:{c.index}": next((cell.raw_value or "" for cell in r.raw_cells if cell.column_index == c.index), "") for c in t.columns} for _, t, r in selected}}
    body.update(overrides)
    return BulkReviewRequest.model_validate(body)


def reopened(store):
    fresh = AIWorkspaceStore(store.root.parent)
    return fresh, AIDocumentDraft.model_validate(fresh.read("transcription.json")), AIReviewState.model_validate_json(json.dumps(fresh.read("review.json"))), fresh.read("editor-draft.json")


def test_preview_commit_restart_replay_and_payload_binding(tmp_path):
    store, draft, state = setup_state(tmp_path)
    request = request_for(draft, state)
    raw = draft.model_dump(mode="json")
    before = store.read("review.json")
    preview = execute_bulk_review(store, draft, state, None, request.model_copy(update={"mode": "preview"}), "reviewer")
    assert preview["can_commit"] and store.read("review.json") == before
    result = execute_bulk_review(store, draft, state, None, request, "reviewer")
    assert result["saved"] and not result["replayed"]
    store, draft, checked, editor = reopened(store)
    assert draft.model_dump(mode="json") == raw
    assert not checked.confirmed_complete_pages
    assert all(role.role == "standalone_item" for role in checked.row_roles.values())
    assert all(decision.human_confirmed and decision.active for decision in checked.decisions.values())
    with pytest.raises(AIReviewError, match="page_completeness_not_human_confirmed"):
        assert_review_ready_for_export(draft,checked)
    history = checked.model_dump(mode="json")
    replay = execute_bulk_review(store, draft, checked, editor, request, "reviewer")
    assert replay["replayed"] and store.read("review.json") == history
    altered = request.model_copy(deep=True)
    altered.values[request.record_ids[0]]["cell:1"] = "Different synthetic value"
    with pytest.raises(BulkReviewError, match="bulk_operation_id_reused"):
        execute_bulk_review(store, draft, checked, editor, altered, "reviewer")
    with pytest.raises(BulkReviewError, match="bulk_operation_id_reused"):
        execute_bulk_review(store, draft, checked, editor, request, "another-reviewer")


@pytest.mark.parametrize("role", ["header", "column_numbering", "section", "continuation", "note"])
def test_service_roles_exclude_without_deleting_values(tmp_path, role):
    store, draft, state = setup_state(tmp_path)
    req = request_for(draft, state, role=role)
    req.values[req.record_ids[0]]["cell:1"] = "Explicit service correction"
    execute_bulk_review(store, draft, state, None, req, "reviewer")
    _, _, result, _ = reopened(store)
    assert len(draft.pages[0].tables[0].rows) == 3
    assert all(d.disposition == "excluded" and not d.human_confirmed for d in result.decisions.values())
    assert result.decisions[req.record_ids[0]].values["cell:1"] == "Explicit service correction"
    assert all(role.role == req.role for role in result.row_roles.values())


def test_component_parent_requires_current_human_role_and_values(tmp_path):
    store, draft, state = setup_state(tmp_path)
    parent, child = [row.id for row in draft.pages[0].tables[0].rows[:2]]
    invalid = request_for(draft, state, [child], role="component", parents={child: parent})
    with pytest.raises(BulkReviewError, match="component_parent_not_confirmed_item"):
        execute_bulk_review(store, draft, state, None, invalid, "reviewer")
    execute_bulk_review(store, draft, state, None, request_for(draft, state, [parent]), "reviewer")
    store, draft, state, editor = reopened(store)
    req = request_for(draft, state, [child], role="component", parents={child: parent})
    execute_bulk_review(store, draft, state, editor, req, "reviewer")
    store, draft, result, editor = reopened(store)
    assert result.row_roles[child].parent_record_id == parent and result.decisions[child].human_confirmed
    # Changing the parent role invalidates dependent role/value decisions in the same transaction.
    req = request_for(draft, result, [parent], role="section")
    preview = execute_bulk_review(store, draft, result, editor, req.model_copy(update={"mode": "preview"}), "reviewer")
    assert child in preview["invalidated_role_ids"] and child in preview["invalidated_decision_ids"]
    execute_bulk_review(store, draft, result, editor, req, "reviewer")
    _, _, changed, _ = reopened(store)
    assert not changed.row_roles[child].active and not changed.decisions[child].active


@pytest.mark.parametrize("scope", ["cell", "row", "table", "page", "document"])
def test_open_issue_scope_is_fail_closed(tmp_path, scope):
    store, draft, state = setup_state(tmp_path)
    page, table = draft.pages[0], draft.pages[0].tables[0]
    row = table.rows[0]
    ids = {"cell": [row.raw_cells[0].id], "row": [row.id], "table": [table.id], "page": [], "document": []}
    draft.issues.append(AIReviewIssue(id="synthetic-open",source_document_id=draft.source_document_id,code="synthetic",severity="review",page_number=None if scope == "document" else 1,record_ids=ids[scope],detail="Synthetic unresolved evidence"))
    request = request_for(draft, state, [row.id])
    with pytest.raises(BulkReviewError, match="bulk_selection_has_open_issues"):
        execute_bulk_review(store, draft, state, None, request, "reviewer")
    assert store.read("review.json") == state.model_dump(mode="json")


def test_last_row_invalid_batch_has_no_partial_write_and_stale_sources_reject(tmp_path):
    store, draft, state = setup_state(tmp_path, 100)
    request = request_for(draft, state)
    request.values[request.record_ids[-1]]["foreign-column"] = "bad"
    before = store.read("review.json")
    with pytest.raises(BulkReviewError, match="bulk_column_provenance_invalid"):
        execute_bulk_review(store, draft, state, None, request, "reviewer")
    assert store.read("review.json") == before
    request = request_for(draft, state)
    request.provenance[request.record_ids[-1]].extraction_fingerprint = "stale"
    with pytest.raises(BulkReviewError, match="bulk_row_provenance_mismatch"):
        execute_bulk_review(store, draft, state, None, request, "reviewer")
    state = record_structure_change(draft, state, action="split_row", source_record_ids=[request.record_ids[0]], result_rows=[{"cell:0":"x"},{"cell:0":"y"}],reviewer_id="reviewer")
    with pytest.raises(BulkReviewError, match="review_source_superseded"):
        execute_bulk_review(store, draft, state, None, request_for(draft, state, [request.record_ids[0]]), "reviewer")


def test_editor_version_consumption_and_unrelated_confirmation_are_preserved(tmp_path):
    store, draft, state = setup_state(tmp_path)
    first, other = [row.id for row in draft.pages[0].tables[0].rows[:2]]
    execute_bulk_review(store, draft, state, None, request_for(draft, state, [other]), "reviewer")
    store, draft, state, _ = reopened(store)
    prior = state.decisions[other].model_dump()
    editor = save_editor(draft, state, None, EditorEdits(row_values={first:{"cell:1":"Buffered checked value"},other:{"cell:1":"Unrelated pending edit"}}), expected_token=review_token(draft,state,None),reviewer_id="reviewer")
    store.write("editor-draft.json",editor)
    req = request_for(draft,state,[first],editor=editor)
    with pytest.raises(BulkReviewError, match="bulk_editor_revision_conflict"):
        execute_bulk_review(store,draft,state,editor,req.model_copy(update={"editor_revision":0}),"reviewer")
    req.values[first]["cell:1"]="Buffered checked value"
    execute_bulk_review(store,draft,state,editor,req,"reviewer")
    _,_,updated,consumed=reopened(store)
    assert updated.decisions[other].model_dump() == prior
    assert updated.decisions[first].values["cell:1"] == "Buffered checked value"
    assert consumed["edits"]["row_values"] == {other:{"cell:1":"Unrelated pending edit"}}
    assert consumed["history"] == editor["history"] and not updated.confirmed_complete_pages


def test_saved_editor_correction_cannot_be_silently_replaced_and_replay_preserves_new_buffers(tmp_path):
    store,draft,state=setup_state(tmp_path)
    first,other=[row.id for row in draft.pages[0].tables[0].rows[:2]]
    editor=save_editor(draft,state,None,EditorEdits(row_values={first:{"cell:1":"5"},other:{"cell:1":"keep"}}),
                       expected_token=review_token(draft,state,None),reviewer_id="reviewer")
    store.write("editor-draft.json",editor)
    req=request_for(draft,state,[first],editor=editor)
    req.values[first]["cell:1"]="2"
    before_review,before_editor=store.read("review.json"),store.read("editor-draft.json")
    preview=execute_bulk_review(store,draft,state,editor,req.model_copy(update={"mode":"preview"}),"reviewer")
    assert not preview["can_commit"] and preview["errors"][0]["code"] == "bulk_editor_values_mismatch"
    with pytest.raises(BulkReviewError,match="bulk_editor_values_mismatch"):
        execute_bulk_review(store,draft,state,editor,req,"reviewer")
    assert store.read("review.json") == before_review and store.read("editor-draft.json") == before_editor
    req.values[first]["cell:1"]="5"
    execute_bulk_review(store,draft,state,editor,req,"reviewer")
    store,draft,checked,consumed=reopened(store)
    assert checked.decisions[first].values["cell:1"] == "5"
    assert consumed["edits"]["row_values"] == {other:{"cell:1":"keep"}}
    assert consumed["history"] == editor["history"]
    newer=save_editor(draft,checked,consumed,EditorEdits(row_values={first:{"cell:1":"7"},other:{"cell:1":"keep"}}),
                      expected_token=review_token(draft,checked,consumed),reviewer_id="reviewer")
    store.write("editor-draft.json",newer)
    committed=store.read("review.json")
    assert execute_bulk_review(store,draft,checked,newer,req,"reviewer")["replayed"]
    assert store.read("review.json") == committed and store.read("editor-draft.json") == newer


def test_interrupted_file_publication_recovers_receipt_and_editor_once(tmp_path, monkeypatch):
    store,draft,state=setup_state(tmp_path)
    ids=[row.id for row in draft.pages[0].tables[0].rows]
    editor=save_editor(draft,state,None,EditorEdits(row_values={ids[0]:{"cell:1":"Pending"}}),expected_token=review_token(draft,state,None),reviewer_id="reviewer")
    store.write("editor-draft.json",editor)
    req=request_for(draft,state,editor=editor)
    req.values[ids[0]]["cell:1"]="Pending"
    original=store._write_atomic_json
    def interrupted(root,name,value):
        if name == "editor-draft.json":
            raise OSError("synthetic interruption after ledger replacement")
        return original(root,name,value)
    monkeypatch.setattr(store,"_write_atomic_json",interrupted)
    with pytest.raises(RuntimeError,match="ai_first_private_state_unavailable"):
        execute_bulk_review(store,draft,state,editor,req,"reviewer")
    assert (store.root/store._TRANSACTION_NAME).exists()
    fresh,draft,checked,editor=reopened(store)
    history=checked.model_dump(mode="json")
    assert not editor["edits"]["row_values"]
    assert execute_bulk_review(fresh,draft,checked,editor,req,"reviewer")["replayed"]
    assert fresh.read("review.json") == history and not (fresh.root/fresh._TRANSACTION_NAME).exists()


def test_process_exit_mid_commit_and_replay_in_new_process(tmp_path):
    store,draft,state=setup_state(tmp_path)
    editor=save_editor(draft,state,None,EditorEdits(),expected_token=review_token(draft,state,None),reviewer_id="reviewer")
    store.write("editor-draft.json",editor)
    req=request_for(draft,state,editor=editor)
    request_path=tmp_path/"synthetic-request.json"
    request_path.write_text(req.model_dump_json(),encoding="utf-8")
    child='''import json,os,sys
from pathlib import Path
from averon_import.services.ai_first.local_workflow import AIWorkspaceStore
from averon_import.services.ai_first.contracts import AIDocumentDraft,AIReviewState
from averon_import.services.ai_first.bulk_review import BulkReviewRequest,execute_bulk_review
s=AIWorkspaceStore(sys.argv[1]);d=AIDocumentDraft.model_validate(s.read("transcription.json"));r=AIReviewState.model_validate_json(json.dumps(s.read("review.json")));e=s.read("editor-draft.json")
q=BulkReviewRequest.model_validate_json(Path(sys.argv[2]).read_text(encoding="utf-8"))
original=s._write_atomic_json
def interrupted(root,name,value):
 original(root,name,value)
 if name=="review.json" and sys.argv[3]=="crash":os._exit(37)
s._write_atomic_json=interrupted
print("BULK_RESULT="+json.dumps(execute_bulk_review(s,d,r,e,q,"reviewer")))
'''
    env={**os.environ,"PYTHONDONTWRITEBYTECODE":"1"}
    crash=subprocess.run([sys.executable,"-B","-c",child,str(store.root.parent),str(request_path),"crash"],capture_output=True,text=True,env=env,timeout=30)
    assert crash.returncode == 37, crash.stderr
    fresh,draft,checked,editor=reopened(store)
    history=fresh.read("review.json")
    replay=subprocess.run([sys.executable,"-B","-c",child,str(store.root.parent),str(request_path),"replay"],capture_output=True,text=True,env=env,timeout=30)
    assert replay.returncode == 0,replay.stderr
    result_line=next(line for line in replay.stdout.splitlines() if line.startswith("BULK_RESULT="))
    assert json.loads(result_line.removeprefix("BULK_RESULT="))["replayed"] and fresh.read("review.json") == history


def test_failure_before_journal_does_not_accept_any_decision(tmp_path,monkeypatch):
    store,draft,state=setup_state(tmp_path)
    before=store.read("review.json")
    def fail(*args): raise OSError("synthetic pre-commit failure")
    monkeypatch.setattr(store,"_write_atomic_json",fail)
    with pytest.raises(OSError): execute_bulk_review(store,draft,state,None,request_for(draft,state),"reviewer")
    assert AIWorkspaceStore(store.root.parent).read("review.json") == before


def test_unknown_record_and_foreign_column_are_not_confirmed(tmp_path):
    store,draft,state=setup_state(tmp_path)
    body=request_for(draft,state).model_dump(mode="json")
    original=body["record_ids"][-1]
    body["record_ids"][-1]="unknown-row"
    body["values"]["unknown-row"]=body["values"].pop(original)
    body["provenance"]["unknown-row"]=body["provenance"].pop(original)
    with pytest.raises(AIReviewError,match="unknown_review_record"):
        execute_bulk_review(store,draft,state,None,BulkReviewRequest.model_validate(body),"reviewer")
    assert store.read("review.json") == state.model_dump(mode="json")


def test_duplicate_source_identity_is_rejected_before_writes(tmp_path):
    store,draft,state=setup_state(tmp_path)
    draft.pages[0].tables[0].rows[1].raw_cells[0].id=draft.pages[0].tables[0].rows[0].raw_cells[0].id
    with pytest.raises(BulkReviewError,match="bulk_source_identity_invalid"):
        execute_bulk_review(store,draft,state,None,request_for(draft,state),"reviewer")
    assert store.read("review.json") == state.model_dump(mode="json")


def test_malformed_receipt_fails_closed(tmp_path):
    store,draft,state=setup_state(tmp_path)
    req=request_for(draft,state)
    execute_bulk_review(store,draft,state,None,req,"reviewer")
    store,draft,checked,editor=reopened(store)
    checked.history[-1].payload["bulk_operation"]["request_sha256"]="not-a-digest"
    before=store.read("review.json")
    with pytest.raises(BulkReviewError,match="bulk_receipt_invalid"):
        execute_bulk_review(store,draft,checked,editor,request_for(draft,checked),"reviewer")
    assert store.read("review.json") == before


@pytest.mark.parametrize("change", ["duplicate", "unknown", "bad_intent", "oversized"])
def test_strict_packet_constraints(tmp_path,change):
    _,draft,state=setup_state(tmp_path)
    body=request_for(draft,state).model_dump(mode="json")
    if change == "duplicate": body["record_ids"].append(body["record_ids"][0])
    if change == "unknown": body["unexpected"]="forbidden"
    if change == "bad_intent": body["intent"]="exclude_from_positions"
    if change == "oversized": body["values"][body["record_ids"][0]]["cell:1"]="x"*2001
    with pytest.raises(ValidationError): BulkReviewRequest.model_validate(body)


def api_setup(main,client):
    workspace=_make_document(main)
    _start_fake(client,workspace.document_id,pages=[1])
    draft=_draft(_payload(rows=[_row(index,str(index+1),f"Synthetic item {index+1}") for index in range(3)]))
    state=new_review_state(draft)
    state.resolved_issue_ids={issue.id for issue in [*draft.issues,*(i for p in draft.pages for i in p.issues)]}
    actual=AIWorkspaceStore(workspace.root)
    actual.write_many({"transcription.json":draft.model_dump(mode="json"),"review.json":state.model_dump(mode="json")})
    return workspace,actual,draft,state


def test_asgi_two_tabs_timeout_replay_and_preview_are_atomic(ai_first_client):
    main,client=ai_first_client
    workspace,store,draft,state=api_setup(main,client)
    request=request_for(draft,state)
    url=f"/api/documents/{workspace.document_id}/ai-first/review/bulk-role-decision"
    before=store.read("review.json")
    assert client.post(url,json={**request.model_dump(mode="json"),"mode":"preview"}).json()["can_commit"]
    assert store.read("review.json") == before
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _: _ApiClient(main.app).post(url,json=request.model_dump(mode="json")),range(2)))
    assert [r.status_code for r in responses] == [200,200]
    assert sorted(r.json()["replayed"] for r in responses) == [False,True]
    history=store.read("review.json")
    # Response can be lost after server commit; a new client repeats the same key safely.
    assert _ApiClient(main.app).post(url,json=request.model_dump(mode="json")).json()["replayed"]
    assert store.read("review.json") == history
    stale=request.model_copy(update={"operation_id":uuid4().hex})
    response=client.post(url,json=stale.model_dump(mode="json"))
    assert response.status_code == 409 and response.json()["detail"]["code"] == "review_revision_conflict"
    assert store.read("review.json") == history


def test_asgi_distinct_tab_operations_and_auth_fail_closed(ai_first_client,monkeypatch):
    main,client=ai_first_client
    workspace,store,draft,state=api_setup(main,client)
    request=request_for(draft,state)
    url=f"/api/documents/{workspace.document_id}/ai-first/review/bulk-role-decision"
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda key: _ApiClient(main.app).post(url,json=request.model_copy(update={"operation_id":key}).model_dump(mode="json")),[uuid4().hex,uuid4().hex]))
    assert sorted(r.status_code for r in responses) == [200,409]
    before=store.read("review.json")
    monkeypatch.setattr(main,"AI_FIRST_UI_ENABLED",False)
    assert client.post(url,json=request.model_dump(mode="json")).status_code == 404
    assert store.read("review.json") == before


def test_asgi_session_owner_is_required_for_commit_and_replay(ai_first_client,monkeypatch):
    main,client=ai_first_client
    workspace,store,draft,state=api_setup(main,client)
    request=request_for(draft,state)
    url=f"/api/documents/{workspace.document_id}/ai-first/review/bulk-role-decision"
    monkeypatch.setattr(main,"auth_config",lambda:SimpleNamespace(mode="session"))
    main.app.dependency_overrides[main.require_authenticated]=lambda:CurrentUser(username="foreign-synthetic",role=Role.USER,user_id="foreign-synthetic")
    before=store.read("review.json")
    assert client.post(url,json=request.model_dump(mode="json")).status_code == 404
    assert store.read("review.json") == before


def test_asgi_manual_row_on_no_specification_is_valid_for_bulk_review(ai_first_client):
    main,client=ai_first_client
    workspace,store,_,_=api_setup(main,client)
    draft=_draft(_payload(tables=[]),document_id=workspace.document_id)
    state=new_review_state(draft)
    state.resolved_issue_ids={issue.id for issue in [*draft.issues,*(i for p in draft.pages for i in p.issues)]}
    store.write_many({"transcription.json":draft.model_dump(mode="json"),"review.json":state.model_dump(mode="json")})
    assert draft.pages[0].status == "no_specification"
    added=client.post(f"/api/documents/{workspace.document_id}/ai-first/review/add-row",json={
        "review_token":review_token(draft,state,None),"page_number":1,"values":{"cell:0":"M-1","cell:1":"Synthetic manual item"}})
    assert added.status_code == 200,added.text
    state=AIReviewState.model_validate_json(json.dumps(added.json()["review"]))
    manual=state.manual_rows[-1]
    req=request_for(draft,state,[manual.id],values={manual.id:dict(manual.values)},provenance={manual.id:{
        "page_number":1,"extraction_fingerprint":draft.pages[0].extraction_fingerprint,"table_id":None}})
    url=f"/api/documents/{workspace.document_id}/ai-first/review/bulk-role-decision"
    assert client.post(url,json={**req.model_dump(mode="json"),"mode":"preview"}).json()["can_commit"]
    result=client.post(url,json=req.model_dump(mode="json"))
    assert result.status_code == 200,result.text
    checked=AIReviewState.model_validate_json(json.dumps(store.read("review.json")))
    assert checked.row_roles[manual.id].role == "standalone_item" and checked.decisions[manual.id].human_confirmed
    assert checked.decisions[manual.id].values == manual.values
    assert store.read("transcription.json") == draft.model_dump(mode="json") and not checked.confirmed_complete_pages
    assert client.post(url,json=req.model_dump(mode="json")).json()["replayed"]


def test_asgi_saved_editor_correction_mismatch_returns_conflict_without_consumption(ai_first_client):
    main,client=ai_first_client
    workspace,store,draft,state=api_setup(main,client)
    first,other=[row.id for row in draft.pages[0].tables[0].rows[:2]]
    saved=client.post(f"/api/documents/{workspace.document_id}/ai-first/review/save",json={
        "review_token":review_token(draft,state,None),"edits":{"row_values":{first:{"cell:1":"5"},other:{"cell:1":"keep"}}}})
    assert saved.status_code==200,saved.text
    editor=store.read("editor-draft.json")
    req=request_for(draft,state,[first],editor=editor)
    req.values[first]["cell:1"]="2"
    before_review,before_editor=store.read("review.json"),store.read("editor-draft.json")
    response=client.post(f"/api/documents/{workspace.document_id}/ai-first/review/bulk-role-decision",json=req.model_dump(mode="json"))
    assert response.status_code==409 and response.json()["detail"]["code"]=="bulk_editor_values_mismatch"
    assert store.read("review.json")==before_review and store.read("editor-draft.json")==before_editor


@pytest.mark.parametrize("status",["failed","incomplete"])
def test_manual_row_on_failed_or_incomplete_page_remains_blocked(tmp_path,status):
    store,draft,state=setup_state(tmp_path)
    state=record_structure_change(draft,state,action="add_row",source_record_ids=[],result_rows=[{"cell:0":"manual"}],reviewer_id="reviewer",page_number=1)
    manual=state.manual_rows[-1]
    draft.pages[0].status=status
    draft.pages[0].error_code="synthetic-failure" if status=="failed" else None
    req=request_for(draft,state,[manual.id],values={manual.id:dict(manual.values)},provenance={manual.id:{
        "page_number":1,"extraction_fingerprint":draft.pages[0].extraction_fingerprint,"table_id":None}})
    before=store.read("review.json")
    with pytest.raises(AIReviewError,match="manual_row_page_revision_mismatch"):
        execute_bulk_review(store,draft,state,None,req,"reviewer")
    assert store.read("review.json") == before


@pytest.mark.parametrize("history_count",[0,1000])
def test_valid_100_row_packet_with_accumulated_history(tmp_path,monkeypatch,history_count):
    draft=_draft(_payload(headers=[f"Synthetic column {n}" for n in range(9)],rows=[
        _row(index,str(index+1),f"Synthetic item {index+1}",cells=[_cell(column,f"R{index+1} C{column+1}") for column in range(9)]) for index in range(100)]))
    state=new_review_state(draft)
    state.resolved_issue_ids={issue.id for issue in [*draft.issues,*(i for p in draft.pages for i in p.issues)]}
    first=draft.pages[0].tables[0].rows[0]
    state.history=[AIReviewEvent(action="confirm_row",source_document_id=draft.source_document_id,reviewer_id="historical-synthetic-reviewer",
                   page_number=1,source_record_ids=[first.id],payload={"disposition":"confirmed","values":{"cell:1":"Historical synthetic value"}}) for _ in range(history_count)]
    preserved=[event.model_dump() for event in state.history]
    store=AIWorkspaceStore(tmp_path/"large-document")
    store.write_many({"transcription.json":draft.model_dump(mode="json"),"review.json":state.model_dump(mode="json")})
    req=request_for(draft,state)
    writes=[]
    original=store.write_many
    def counted(payloads):
        writes.append(tuple(payloads))
        return original(payloads)
    monkeypatch.setattr(store,"write_many",counted)
    start=perf_counter()
    preview=execute_bulk_review(store,draft,state,None,req.model_copy(update={"mode":"preview"}),"reviewer")
    preview_ms=(perf_counter()-start)*1000
    assert preview["can_commit"] and not writes
    start=perf_counter()
    result=execute_bulk_review(store,draft,state,None,req,"reviewer")
    commit_ms=(perf_counter()-start)*1000
    _,_,checked,editor=reopened(store)
    assert result["row_count"]==100 and len(checked.decisions)==100 and len(checked.row_roles)==100
    assert [event.model_dump() for event in checked.history[:history_count]]==preserved
    assert len(checked.history)==history_count+200 and not checked.confirmed_complete_pages
    assert len(writes)==1 and editor is None
    assert execute_bulk_review(store,draft,checked,None,req,"reviewer")["replayed"] and len(writes)==1
    print(f"BULK_PERF rows=100 columns=9 history={history_count} preview_ms={preview_ms:.3f} commit_ms={commit_ms:.3f} writes={len(writes)}")
