"""Explicit maintenance admission. No PID guessing or permissive legacy default.

The host must supply a reviewed LegacyQuiescenceGuard: all legacy DB clients
stopped, handles closed, restart inhibited for the entire lease. SQLite cannot
prove this for an idle, pre-protocol connection. No such host adapter is shipped
here; callers without one fail closed. Synthetic tests implement their own
guard over subprocesses they exclusively control.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
import ctypes
import os
from pathlib import Path
import threading

from .coordination_contracts import CoordinationError, canonical, digest, uuid_hex
from .private_acl import PrivateAclError, create_private_directory, secure_private_path, verify_private_path, verify_private_tree


def _safe_path(path):
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
            raise CoordinationError("storage_unverified")
    if path.is_relative_to(Path(__file__).resolve().parents[3]):
        raise CoordinationError("ledger_must_be_outside_repository")
    return path


def _identity(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino


def _sync_dir(root):
    if os.name != "nt":
        descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class _NativeLock:
    """Lifetime nonblocking shared/exclusive OS lock; never TTL stealing."""
    def __init__(self, path, *, exclusive, busy_code):
        self.path, self.pid, self.stream = Path(path), os.getpid(), None
        try:
            self.stream = self.path.open("r+b")
        except OSError:
            raise CoordinationError("storage_unverified") from None
        try:
            verify_private_path(self.path, directory=False)
            self.identity = _identity(self.path)
            if self.path.stat().st_size != 1:
                raise CoordinationError("storage_unverified")
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
                if not self.kernel.LockFileEx(self.handle, 1 | (2 if exclusive else 0), 0, 1, 0,
                                              ctypes.byref(self.overlapped)):
                    raise OSError(ctypes.get_last_error(), "lock unavailable")
            elif os.name == "posix":
                import fcntl
                fcntl.flock(self.stream.fileno(), (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            else:
                raise CoordinationError("maintenance_lock_unsupported")
            self.check()
        except BaseException as exc:
            self.stream.close()
            if isinstance(exc, OSError):
                raise CoordinationError(busy_code) from None
            if isinstance(exc, PrivateAclError):
                raise CoordinationError("storage_unverified") from None
            raise

    def __reduce__(self):
        raise TypeError("maintenance_lock_is_process_bound")

    def check(self):
        if self.pid != os.getpid() or self.stream.closed or _identity(self.path) != self.identity:
            raise CoordinationError("maintenance_fenced")
        verify_private_path(self.path, directory=False)

    def close(self):
        if self.stream is None or self.stream.closed:
            return
        try:
            if self.pid == os.getpid():
                if os.name == "nt":
                    self.kernel.UnlockFileEx(self.handle, 0, 1, 0, ctypes.byref(self.overlapped))
                elif os.name == "posix":
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()


class MaintenanceLayout:
    """Separately provisioned private control root, outside the v1 ledger root."""
    def __init__(self, root, ledger_root, installation_id):
        self.root, self.ledger_root = _safe_path(root), _safe_path(ledger_root)
        self.installation_id = uuid_hex(installation_id)
        if self.root.is_relative_to(self.ledger_root) or self.ledger_root.is_relative_to(self.root):
            raise CoordinationError("maintenance_roots_overlap")
        self.path = self.root / "maintenance.lock"
        self.manifest = self.root / "binding.json"
        self.expected = canonical({"version": 1, "installation_id": self.installation_id,
                                   "ledger_root": str(self.ledger_root)}).encode()
        try:
            self.root_identity = _identity(self.root)
            self.manifest_identity = _identity(self.manifest)
            self.verify()
        except (OSError, PrivateAclError):
            raise CoordinationError("maintenance_storage_unverified") from None

    @classmethod
    def provision(cls, root, ledger_root, installation_id):
        root, ledger_root = _safe_path(root), _safe_path(ledger_root)
        uuid_hex(installation_id)
        if root.is_relative_to(ledger_root) or ledger_root.is_relative_to(root):
            raise CoordinationError("maintenance_roots_overlap")
        if root.exists():
            raise CoordinationError("maintenance_target_exists")
        create_private_directory(root)
        payload = canonical({"version": 1, "installation_id": installation_id,
                             "ledger_root": str(ledger_root)}).encode()
        for name, content in (("maintenance.lock", b"\0"), ("binding.json", payload)):
            with (root / name).open("xb") as stream:
                secure_private_path(root / name, directory=False)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        _sync_dir(root)
        verify_private_tree(root)
        return cls(root, ledger_root, installation_id)

    def verify(self):
        try:
            self._verify()
        except (OSError, PrivateAclError):
            raise CoordinationError("maintenance_storage_unverified") from None

    def _verify(self):
        verify_private_path(self.root, directory=True)
        if _identity(self.root) != self.root_identity or _identity(self.manifest) != self.manifest_identity:
            raise CoordinationError("maintenance_storage_replaced")
        if {path.name for path in self.root.iterdir()} != {"maintenance.lock", "binding.json"}:
            raise CoordinationError("maintenance_storage_unverified")
        verify_private_path(self.manifest, directory=False)
        with self.manifest.open("rb") as stream:
            if stream.read(4097) != self.expected:
                raise CoordinationError("maintenance_binding_mismatch")
        verify_private_path(self.path, directory=False)


class MaintenanceClient:
    def __init__(self, root, ledger_root, installation_id):
        self.layout = MaintenanceLayout(root, ledger_root, installation_id)
        self.lock = _NativeLock(self.layout.path, exclusive=False, busy_code="maintenance_busy")
        try:
            self.check()
        except BaseException:
            self.lock.close()
            raise

    def check(self):
        self.layout.verify()
        self.lock.check()

    def close(self):
        self.lock.close()


class LegacyQuiescenceGuard(ABC):
    """Trusted host integration, not an operator boolean or PID-list assertion.

    verify() must prove that *all* nonparticipating readers/writers are stopped,
    their handles closed, and reopening remains inhibited for this lease. It
    returns a stable hash of host evidence, never prompts/credentials/history.
    The caller owns the guard through the complete MaintenanceSession.
    """
    @abstractmethod
    def verify(self, ledger_root: Path, installation_id: str) -> str:
        raise NotImplementedError


class _BorrowedMaintenance:
    def __init__(self, session):
        self.session, self.closed = session, False

    def check(self):
        if self.closed:
            raise CoordinationError("maintenance_fenced")
        self.session.check()

    def close(self):
        with self.session._lifetime_guard:
            if not self.closed:
                self.closed = True
                self.session._borrowers -= 1

    def __reduce__(self):
        raise TypeError("maintenance_client_is_process_bound")

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class MaintenanceSession:
    def __init__(self, root, ledger_root, installation_id, *, legacy_guard):
        if not isinstance(legacy_guard, LegacyQuiescenceGuard):
            raise CoordinationError("legacy_quiescence_required")
        self.layout = MaintenanceLayout(root, ledger_root, installation_id)
        self.guard, self.active = legacy_guard, False
        self._lifetime_guard, self._borrowers = threading.RLock(), 0
        self.lock = _NativeLock(self.layout.path, exclusive=True, busy_code="maintenance_busy")
        try:
            self.evidence_digest = digest(self.guard.verify(self.layout.ledger_root, self.layout.installation_id))
            self.active = True
            self.check()
        except BaseException:
            self.lock.close()
            raise

    def __reduce__(self):
        raise TypeError("maintenance_session_is_process_bound")

    def check(self):
        if not self.active:
            raise CoordinationError("maintenance_fenced")
        self.layout.verify()
        self.lock.check()
        if self.guard.verify(self.layout.ledger_root, self.layout.installation_id) != self.evidence_digest:
            raise CoordinationError("legacy_quiescence_unverified")

    def borrow(self):
        with self._lifetime_guard:
            self.check()
            token = _BorrowedMaintenance(self)
            self._borrowers += 1
            return token

    def close(self):
        with self._lifetime_guard:
            if self._borrowers:
                raise CoordinationError("maintenance_in_use")
            self.active = False
            self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
