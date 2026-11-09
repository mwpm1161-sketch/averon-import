"""Private, content-addressed B3.2 execution inputs; no PDF/OCR/HTTP body."""
from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
import sqlite3
import time

from .coordination_contracts import CoordinationError, canonical, fingerprint, uuid_hex
from .private_acl import (
    PrivateAclError, _reject_reparse, create_private_directory, secure_private_path,
    verify_private_path,
)
from .durable_job_contracts import (
    AuthorizedDocumentContext, ExecutionInput, MAX_EXECUTION_BYTES, execution_bytes,
    parse_execution_bytes,
)

_SHARDS = 16
_FILE_LOCK_WAIT_SECONDS = 10.0
_MANIFEST = "durable-execution-store.json"
_LOCK = "durable-execution-stage.lock"


def _digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _open_readonly(path):
    flags = os.O_RDONLY
    if os.name == "posix":
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    if os.name == "nt":
        flags |= getattr(os, "O_BINARY", 0)
    return os.fdopen(os.open(path, flags), "rb")


def _canonical_existing(path):
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        _reject_reparse(item)
    try:
        if path.resolve(strict=True) != path:
            raise CoordinationError("execution_storage_unverified")
    except OSError:
        raise CoordinationError("execution_storage_unverified") from None
    return path


def _assert_existing_ancestors(path):
    path = Path(os.path.abspath(path))
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    _canonical_existing(current)
    for item in missing:
        # A missing path component cannot itself be a junction/reparse point.
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
            raise CoordinationError("execution_storage_unverified")
    return path


def _sync_directory(path):
    """POSIX directory fsync. Windows callers use write-through rename below."""
    if os.name == "posix":
        try:
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            raise CoordinationError("execution_directory_sync_failed") from None


class _StageLock:
    """One preprovisioned process-wide staging lock; bounded, never TTL-stolen."""
    def __init__(self, path):
        self.path, self.pid, self.stream = path, os.getpid(), None
        try:
            _reject_reparse(path)
            verify_private_path(path, directory=False)
            if path.stat().st_size != 1:
                raise CoordinationError("execution_storage_unverified")
            self.stream = path.open("r+b")
            deadline = time.monotonic() + _FILE_LOCK_WAIT_SECONDS
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        from ctypes import wintypes
                        class Overlapped(ctypes.Structure):
                            _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                                ("hEvent", wintypes.HANDLE)]
                        self.overlapped = Overlapped()
                        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                        self.kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                            wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
                        self.kernel.LockFileEx.restype = wintypes.BOOL
                        self.kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                            wintypes.DWORD, ctypes.c_void_p]
                        self.kernel.UnlockFileEx.restype = wintypes.BOOL
                        self.handle = msvcrt.get_osfhandle(self.stream.fileno())
                        ok = self.kernel.LockFileEx(self.handle, 1 | 2, 0, 1, 0, ctypes.byref(self.overlapped))
                        if not ok:
                            raise BlockingIOError(ctypes.get_last_error(), "stage busy")
                    elif os.name == "posix":
                        import fcntl
                        fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    else:
                        raise CoordinationError("execution_lock_unsupported")
                    return
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise CoordinationError("execution_stage_busy") from None
                    time.sleep(0.025)
        except BaseException:
            if self.stream is not None:
                self.stream.close()
            raise

    def close(self):
        if self.stream is None or self.stream.closed:
            return
        try:
            if self.pid == os.getpid():
                if os.name == "nt":
                    self.kernel.UnlockFileEx(self.handle, 0, 1, 0, ctypes.byref(self.overlapped))
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class PrivateExecutionStore:
    """An explicitly provisioned private storage root bound to one installation."""
    def __init__(self, root, installation_id, *, _fault_hook=None):
        self.root = _canonical_existing(root)
        self.installation_id = uuid_hex(installation_id)
        self._fault_hook = _fault_hook
        self.manifest_path, self.lock_path = self.root / _MANIFEST, self.root / _LOCK
        try:
            verify_private_path(self.root, directory=True)
            verify_private_path(self.manifest_path, directory=False)
            verify_private_path(self.lock_path, directory=False)
            if self.manifest_path.stat().st_size != len(self._manifest_bytes()) or self.manifest_path.read_bytes() != self._manifest_bytes():
                raise CoordinationError("execution_storage_binding_mismatch")
            entries = {p.name for p in self.root.iterdir()}
            if entries != {_MANIFEST, _LOCK, *[f"{i:02x}" for i in range(_SHARDS)]}:
                raise CoordinationError("execution_storage_unverified")
            for i in range(_SHARDS):
                verify_private_path(self.root / f"{i:02x}", directory=True)
        except (OSError, PrivateAclError):
            raise CoordinationError("execution_storage_unverified") from None

    @classmethod
    def provision(cls, root, installation_id):
        """Explicit test/operator setup; never called by app start or DB open."""
        root = Path(os.path.abspath(root))
        _assert_existing_ancestors(root.parent)
        if root.exists():
            raise CoordinationError("execution_storage_already_exists")
        installation_id = uuid_hex(installation_id)
        create_private_directory(root)
        try:
            for index in range(_SHARDS):
                create_private_directory(root / f"{index:02x}")
            manifest = root / _MANIFEST
            with manifest.open("xb") as stream:
                secure_private_path(manifest, directory=False)
                stream.write(canonical({"version": 1, "installation_id": installation_id,
                    "shards": _SHARDS}).encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            lock = root / _LOCK
            with lock.open("xb") as stream:
                secure_private_path(lock, directory=False)
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())
            for index in range(_SHARDS):
                _sync_directory(root / f"{index:02x}")
            _sync_directory(root)
            _sync_directory(root.parent)
            return cls(root, installation_id)
        except BaseException:
            # Never recursively erase an uncertain private store.
            raise

    def _manifest_bytes(self):
        return canonical({"version": 1, "installation_id": self.installation_id,
                          "shards": _SHARDS}).encode("utf-8")

    def _check_root(self):
        try:
            if _canonical_existing(self.root) != self.root:
                raise CoordinationError("execution_storage_unverified")
            verify_private_path(self.root, directory=True)
            verify_private_path(self.manifest_path, directory=False)
            if self.manifest_path.stat().st_size != len(self._manifest_bytes()) or self.manifest_path.read_bytes() != self._manifest_bytes():
                raise CoordinationError("execution_storage_binding_mismatch")
            verify_private_path(self.lock_path, directory=False)
            if self.lock_path.stat().st_size != 1:
                raise CoordinationError("execution_storage_unverified")
        except (OSError, PrivateAclError):
            raise CoordinationError("execution_storage_unverified") from None

    def _paths(self, job_id, execution_id):
        if (type(job_id) is not str or len(job_id) != 35 or not job_id.startswith("dj_")
                or any(c not in "0123456789abcdef" for c in job_id[3:])
                or type(execution_id) is not str or len(execution_id) != 64
                or any(c not in "0123456789abcdef" for c in execution_id)):
            raise CoordinationError("execution_identity_invalid")
        shard = int(execution_id[:2], 16) % _SHARDS
        parent = self.root / f"{shard:02x}"
        stem = job_id
        return parent, parent / (stem + ".json"), parent / (stem + ".pending")

    def lock(self):
        self._check_root()
        return _StageLock(self.lock_path)

    @staticmethod
    def _verify_parent(parent):
        _reject_reparse(parent)
        verify_private_path(parent, directory=True)

    @staticmethod
    def _read_one(path, expected_digest, expected_bytes):
        try:
            _reject_reparse(path)
            verify_private_path(path, directory=False)
            before = path.stat()
            if not path.is_file() or before.st_size != expected_bytes or before.st_size > MAX_EXECUTION_BYTES:
                raise CoordinationError("execution_input_corrupt")
            with _open_readonly(path) as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size):
                    raise CoordinationError("execution_input_corrupt")
                raw = stream.read(MAX_EXECUTION_BYTES + 1)
            after = path.stat()
            if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    or len(raw) != expected_bytes or hashlib.sha256(raw).hexdigest() != expected_digest):
                raise CoordinationError("execution_input_corrupt")
            return parse_execution_bytes(raw)
        except CoordinationError:
            raise
        except (OSError, PrivateAclError, ValueError, TypeError):
            raise CoordinationError("execution_input_corrupt") from None

    def _sync_parent(self, parent):
        verify_private_path(parent, directory=True)
        _sync_directory(parent)

    def _write_pending(self, parent, path, raw):
        self._verify_parent(parent)
        try:
            with path.open("xb") as stream:
                secure_private_path(path, directory=False)
                half = max(1, len(raw) // 2)
                stream.write(raw[:half])
                if self._fault_hook is not None:
                    self._fault_hook("during_pending")
                stream.write(raw[half:])
                stream.flush()
                os.fsync(stream.fileno())
            self._read_one(path, _digest_bytes(raw), len(raw))
            self._sync_parent(parent)
            if self._fault_hook is not None:
                self._fault_hook("after_pending_fsync")
        except CoordinationError:
            raise
        except FileExistsError:
            raise CoordinationError("execution_pending_exists") from None
        except (OSError, PrivateAclError):
            raise CoordinationError("execution_stage_write_failed") from None

    def _install(self, parent, pending, final, expected_digest, expected_bytes):
        self._verify_parent(parent)
        self._read_one(pending, expected_digest, expected_bytes)
        try:
            if os.name == "nt":
                # MOVEFILE_WRITE_THROUGH, without REPLACE_EXISTING: no overwrite.
                from ctypes import wintypes
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
                kernel.MoveFileExW.restype = wintypes.BOOL
                if not kernel.MoveFileExW(str(pending), str(final), 0x00000008):
                    error = ctypes.get_last_error()
                    if error not in (80, 183):
                        raise OSError(error, "write-through install failed")
                if self._fault_hook is not None:
                    self._fault_hook("after_final_install")
            else:
                os.link(pending, final, follow_symlinks=False)
                self._sync_parent(parent)
                if self._fault_hook is not None:
                    self._fault_hook("after_final_install")
                pending.unlink()
                self._sync_parent(parent)
            installed = self._read_one(final, expected_digest, expected_bytes)
            if self._fault_hook is not None:
                self._fault_hook("after_final_fsync")
            return installed
        except FileExistsError:
            return self._read_one(final, expected_digest, expected_bytes)
        except CoordinationError:
            raise
        except (OSError, PrivateAclError):
            # Lost install receipt is resolved by re-reading the exact final.
            if final.exists():
                return self._read_one(final, expected_digest, expected_bytes)
            raise CoordinationError("execution_stage_install_failed") from None

    def stage(self, job_id, execution_id, expected_digest, expected_bytes,
              candidate: ExecutionInput | None, *, allow_create: bool):
        """Filesystem-only step. The caller must not hold a SQLite transaction."""
        parent, final, pending = self._paths(job_id, execution_id)
        with self.lock():
            self._check_root()
            self._verify_parent(parent)
            if final.exists():
                value = self._read_one(final, expected_digest, expected_bytes)
                if pending.exists():
                    _reject_reparse(pending)
                    verify_private_path(pending, directory=False)
                    info = pending.stat()
                    if not pending.is_file() or info.st_size > MAX_EXECUTION_BYTES:
                        raise CoordinationError("execution_storage_unverified")
                    pending.unlink()
                    self._sync_parent(parent)
                return value
            if pending.exists():
                return self._install(parent, pending, final, expected_digest, expected_bytes)
            if not allow_create or candidate is None:
                raise CoordinationError("execution_input_missing")
            raw = execution_bytes(candidate)
            if len(raw) != expected_bytes or _digest_bytes(raw) != expected_digest:
                raise CoordinationError("execution_input_mismatch")
            self._write_pending(parent, pending, raw)
            return self._install(parent, pending, final, expected_digest, expected_bytes)

    def read(self, job_id, execution_id, expected_digest, expected_bytes):
        parent, final, _pending = self._paths(job_id, execution_id)
        self._check_root()
        self._verify_parent(parent)
        if not final.exists():
            raise CoordinationError("execution_input_missing")
        return self._read_one(final, expected_digest, expected_bytes)

    def remove_pending(self, job_id, execution_id, expected_digest, expected_bytes):
        """Strict explicit cleanup of temp bytes only; immutable final input is retained.

        This path verifies the final artifact before touching the pending file.
        Post-cancellation cleanup must not use it: a damaged final has already
        been sealed by a committed cancellation. See ``remove_pending_after_cancel``.
        """
        parent, final, pending = self._paths(job_id, execution_id)
        with self.lock():
            self._check_root()
            self._verify_parent(parent)
            if final.exists():
                self._read_one(final, expected_digest, expected_bytes)
            if pending.exists():
                _reject_reparse(pending)
                verify_private_path(pending, directory=False)
                if not pending.is_file() or pending.stat().st_size > MAX_EXECUTION_BYTES:
                    raise CoordinationError("execution_storage_unverified")
                pending.unlink()
                self._sync_parent(parent)

    def remove_pending_after_cancel(self, job_id, execution_id):
        """Best-effort cleanup after a cancellation is already durably committed.

        Only the private temporary ``.pending`` file is considered. The immutable
        final artifact is never read or removed, so a corrupt final cannot
        retroactively fail the cancellation. Unverifiable pending paths are left
        as private orphans for a later strict cleanup; ACL/I/O failures are
        intentionally swallowed because the durable cancellation already stands.
        """
        try:
            parent, _final, pending = self._paths(job_id, execution_id)
            with self.lock():
                self._check_root()
                self._verify_parent(parent)
                if not pending.exists():
                    return
                _reject_reparse(pending)
                verify_private_path(pending, directory=False)
                if not pending.is_file() or pending.stat().st_size > MAX_EXECUTION_BYTES:
                    return
                pending.unlink()
                self._sync_parent(parent)
        except (CoordinationError, OSError, PrivateAclError):
            return

