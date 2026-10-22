from __future__ import annotations

import os
import subprocess

import pytest

from averon_import.services.ai_first.orchestration import CheckpointIdentity, CheckpointStore
from averon_import.services.ai_first.private_acl import (
    PrivateAclError, create_private_directory, verify_private_tree,
)
from averon_import.tools.qwen_vision_pilot import _write_json_private


@pytest.mark.skipif(os.name != "nt", reason="requires the real Windows DACL APIs")
def test_windows_pilot_and_checkpoint_dacls_are_created_and_verified_offline(tmp_path):
    root = create_private_directory(tmp_path / "isolated-pilot-run")
    _write_json_private(root / "safe-report.json", {"status": "dry_run_ready"})

    digest = "a" * 64
    identity = CheckpointIdentity(
        source_sha256=digest,
        source_size_bytes=1,
        source_mtime_ns=0,
        page_count=1,
        run_id="acl-offline-test",
        source_document_id=f"pdf-sha256:{digest}",
        adapter_fingerprint="b" * 64,
        processing_fingerprint="c" * 64,
    )
    checkpoint = CheckpointStore(root / "checkpoint", identity)
    checkpoint.open()
    try:
        assert verify_private_tree(root) >= 5
    finally:
        checkpoint.close()


@pytest.mark.skipif(os.name != "nt", reason="requires the real Windows DACL APIs")
def test_windows_acl_verifier_rejects_unexpected_full_control_ace(tmp_path):
    root = create_private_directory(tmp_path / "unexpected-ace-test")
    subprocess.run(
        ["icacls.exe", str(root), "/grant", "*S-1-3-4:F"],
        check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=10,
    )
    with pytest.raises(PrivateAclError, match="private_acl_unverified"):
        verify_private_tree(root)
