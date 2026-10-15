"""Explicit, private coordinator for publishing a bounded TSTN catalog snapshot."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
import re
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from pydantic import SecretStr

from averon_import.services.secrets import TSTN_CREDENTIALS_CONFIGURED, SecretStore
from averon_import.services.sourcing.providers.base import SourcingProviderError

from .authorization import TSTN_CREDENTIAL_LOCK
from .mirror import (
    TstnCatalogPageClient,
    TstnCatalogMirror,
    TstnMirrorSyncGuardRejected,
)


_GENERATION_RE = re.compile(r"^[a-f0-9]{32}$")
_SYNC_LOCKS: dict[str, Lock] = {}
_SYNC_LOCKS_LOCK = Lock()
_SAFE_MIRROR_ERRORS = {
    "TSTN_SNAPSHOT_LIMIT",
    "TSTN_SNAPSHOT_INVALID",
    "TSTN_MIRROR_UPDATE_FAILED",
    "TSTN_SYNC_LIMIT_INVALID",
    "TSTN_SYNC_GUARD_INVALID",
    "TSTN_CLIENT_MISSING",
}


@dataclass(frozen=True, slots=True)
class TstnCatalogSyncOutcome:
    """Bounded local result; contains no token, generation, or exception text."""

    state: Literal["success", "busy", "failure"]
    item_count: int = 0
    page_count: int = 0
    catalog_revision: str | None = None
    error_code: str | None = None


class TstnCatalogSyncCoordinator:
    """Run one explicit catalog sync against a request-local typed client.

    Construction and representation are passive. The writeable mirror is
    created only inside ``sync`` after its local configuration guard passes.
    The supplied access token is never retained by this coordinator.
    """

    def __init__(
        self,
        data_dir: str | Path,
        secret_store: SecretStore,
        *,
        current_configuration: Callable[[], tuple[bool, str]],
        credential_lock: Any = TSTN_CREDENTIAL_LOCK,
    ) -> None:
        if not callable(current_configuration):
            raise ValueError("TSTN configuration callback is invalid")
        self._data_dir = Path(data_dir)
        self._secret_store = secret_store
        self._current_configuration = current_configuration
        self._credential_lock = credential_lock

    def __repr__(self) -> str:
        return "TstnCatalogSyncCoordinator(<private>)"

    @property
    def catalog_path(self) -> Path:
        """The single TN5A catalog path; this property does not create it."""

        return self._data_dir / "sourcing" / "providers" / "tstn" / "catalog.sqlite3"

    def _configuration_is_current(self, generation: str) -> bool:
        try:
            enabled, current_generation = self._current_configuration()
            return (
                enabled is True
                and current_generation == generation
                and self._secret_store.get(TSTN_CREDENTIALS_CONFIGURED) == generation
            )
        except Exception:
            return False

    @staticmethod
    def _lock_for_path(path: Path) -> Lock:
        key = str(path.resolve())
        with _SYNC_LOCKS_LOCK:
            lock = _SYNC_LOCKS.get(key)
            if lock is None:
                lock = Lock()
                _SYNC_LOCKS[key] = lock
            return lock

    def sync(
        self,
        *,
        client: TstnCatalogPageClient,
        access_token: SecretStr,
        credential_generation: str,
    ) -> TstnCatalogSyncOutcome:
        """Fetch and atomically publish one full bounded snapshot, or fail closed."""

        if (
            not isinstance(access_token, SecretStr)
            or not access_token.get_secret_value()
            or not isinstance(credential_generation, str)
            or not _GENERATION_RE.fullmatch(credential_generation)
        ):
            return TstnCatalogSyncOutcome(
                state="failure",
                error_code="TSTN_SYNC_NOT_CONFIGURED",
            )

        path = self.catalog_path
        sync_lock = self._lock_for_path(path)
        if not sync_lock.acquire(blocking=False):
            return TstnCatalogSyncOutcome(state="busy", error_code="TSTN_SYNC_BUSY")

        try:
            # Serialize the check and writable mirror construction with
            # credential rotation, but never keep the credential lock while
            # downloading the complete catalog.
            with self._credential_lock:
                if not self._configuration_is_current(credential_generation):
                    return TstnCatalogSyncOutcome(
                        state="failure",
                        error_code="TSTN_SYNC_STALE_CONFIGURATION",
                    )
                try:
                    mirror = TstnCatalogMirror(path)
                except Exception:
                    return TstnCatalogSyncOutcome(
                        state="failure",
                        error_code="TSTN_CATALOG_STORAGE_ERROR",
                    )

            @contextmanager
            def supplier_attempt_guard():
                with self._credential_lock:
                    if not self._configuration_is_current(credential_generation):
                        raise TstnMirrorSyncGuardRejected()
                    yield
                    # A same-thread credential mutation can re-enter an RLock;
                    # this second check also makes injected clients fail closed.
                    if not self._configuration_is_current(credential_generation):
                        raise TstnMirrorSyncGuardRejected()

            @contextmanager
            def publication_guard():
                with self._credential_lock:
                    if not self._configuration_is_current(credential_generation):
                        raise TstnMirrorSyncGuardRejected()
                    # Keep the lock through BEGIN/validation/commit so a real
                    # rotation is ordered strictly before or after publication.
                    yield

            try:
                result = mirror.sync(
                    client,
                    access_token=access_token,
                    supplier_attempt_guard=supplier_attempt_guard,
                    publication_guard=publication_guard,
                )
            except TstnMirrorSyncGuardRejected:
                return TstnCatalogSyncOutcome(
                    state="failure",
                    error_code="TSTN_SYNC_STALE_CONFIGURATION",
                )
            except SourcingProviderError as exc:
                code = exc.code if exc.code in _SAFE_MIRROR_ERRORS else "TSTN_SYNC_FAILED"
                return TstnCatalogSyncOutcome(state="failure", error_code=code)
            except Exception:
                return TstnCatalogSyncOutcome(
                    state="failure",
                    error_code="TSTN_SYNC_FAILED",
                )

            return TstnCatalogSyncOutcome(
                state="success",
                item_count=result.item_count,
                page_count=result.page_count,
                catalog_revision=result.revision,
            )
        finally:
            sync_lock.release()
