"""Application secret storage.

Secrets (API keys) never live in settings.json. Production on Windows uses
the OS credential manager through advapi32 (no third-party dependencies).
Non-Windows development falls back to an explicitly marked plaintext file;
tests use an in-memory store.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

YANDEX_API_KEY = "yandex.api_key"
# Dedicated Yandex AI Studio credential.  It must never fall back to the
# Vision/OCR credential above.
YANDEX_AI_API_KEY = "yandex.ai_api_key"


class SecretStoreError(RuntimeError):
    pass


class SecretStore(Protocol):
    backend_name: str
    is_insecure: bool

    def has(self, key: str) -> bool: ...
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...


class MemorySecretStore:
    """In-memory store for tests and explicit opt-in scenarios."""

    backend_name = "memory"
    is_insecure = True

    def __init__(self):
        self._values: dict[str, str] = {}

    def has(self, key: str) -> bool:
        return bool(self._values.get(key))

    def get(self, key: str) -> str | None:
        return self._values.get(key)

    def set(self, key: str, value: str) -> None:
        if not isinstance(value, str):
            raise SecretStoreError("Значение секрета должно быть строкой")
        self._values[key] = value

    def delete(self, key: str) -> None:
        self._values.pop(key, None)


class InsecureFileSecretStore:
    """Plaintext fallback for developer machines outside Windows.

    Explicitly marked insecure; production deployments on Windows receive
    the credential-manager backend instead.
    """

    backend_name = "insecure-file"
    is_insecure = True

    def __init__(self, directory: Path | None = None):
        directory = Path(directory) if directory else Path(".")
        directory.mkdir(parents=True, exist_ok=True)
        self._path = directory / "secrets.insecure.json"

    def _read(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SecretStoreError(f"Файл секретов повреждён: {exc}") from exc
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    def has(self, key: str) -> bool:
        return bool(self._read().get(key))

    def get(self, key: str) -> str | None:
        value = self._read().get(key)
        return str(value) if isinstance(value, str) and value else None

    def set(self, key: str, value: str) -> None:
        if not isinstance(value, str):
            raise SecretStoreError("Значение секрета должно быть строкой")
        data = self._read()
        data[key] = value
        self._write(data)

    def delete(self, key: str) -> None:
        data = self._read()
        if key in data:
            del data[key]
            self._write(data)


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _CRED_TYPE_GENERIC = 1
    _CRED_PERSIST_ENTERPRISE = 2
    _ERROR_NOT_FOUND = 1168

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    class WindowsCredentialStore:
        """Windows Credential Manager backed store (generic credentials)."""

        backend_name = "windows-credential-manager"
        is_insecure = False

        def __init__(self, prefix: str = "Averon Import"):
            self._prefix = prefix

        def _target(self, key: str) -> str:
            return f"{self._prefix}:{key}"

        def has(self, key: str) -> bool:
            return self.get(key) is not None

        def get(self, key: str) -> str | None:
            pointer = ctypes.POINTER(_CREDENTIAL)()
            if not _advapi32.CredReadW(
                self._target(key), _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)
            ):
                error = ctypes.get_last_error()
                if error == _ERROR_NOT_FOUND:
                    return None
                raise SecretStoreError(f"CredRead завершился с ошибкой {error}")
            try:
                credential = pointer.contents
                size = int(credential.CredentialBlobSize)
                if not size or not credential.CredentialBlob:
                    return ""
                return ctypes.string_at(credential.CredentialBlob, size).decode("utf-8")
            finally:
                _advapi32.CredFree(pointer)

        def set(self, key: str, value: str) -> None:
            if not isinstance(value, str):
                raise SecretStoreError("Значение секрета должно быть строкой")
            blob = value.encode("utf-8")
            buffer = ctypes.create_string_buffer(blob) if blob else None
            credential = _CREDENTIAL()
            credential.Type = _CRED_TYPE_GENERIC
            credential.TargetName = self._target(key)
            credential.Comment = "Averon Import secret"
            credential.Persist = _CRED_PERSIST_ENTERPRISE
            credential.CredentialBlobSize = len(blob)
            if buffer is not None:
                credential.CredentialBlob = ctypes.cast(
                    buffer, ctypes.POINTER(ctypes.c_byte)
                )
            if not _advapi32.CredWriteW(ctypes.byref(credential), 0):
                raise SecretStoreError(
                    f"CredWrite завершился с ошибкой {ctypes.get_last_error()}"
                )

        def delete(self, key: str) -> None:
            if not _advapi32.CredDeleteW(self._target(key), _CRED_TYPE_GENERIC, 0):
                error = ctypes.get_last_error()
                if error == _ERROR_NOT_FOUND:
                    return
                raise SecretStoreError(f"CredDelete завершился с ошибкой {error}")
else:
    WindowsCredentialStore = None  # type: ignore[assignment]


def create_secret_store(data_dir: Path | None = None, kind: str | None = None) -> SecretStore:
    requested = (kind or os.environ.get("AVERON_SECRET_STORE") or "auto").strip().lower()
    if requested == "memory":
        return MemorySecretStore()
    if requested == "insecure-file":
        return InsecureFileSecretStore(data_dir)
    if requested == "wincred" or (requested == "auto" and os.name == "nt"):
        if WindowsCredentialStore is not None:
            return WindowsCredentialStore()
    return InsecureFileSecretStore(data_dir)


def resolve_secret(env_value: str | None, store: SecretStore, key: str) -> str | None:
    """Deterministic resolution: environment overrides the secret store."""
    if env_value and env_value.strip():
        return env_value.strip()
    value = store.get(key)
    if value and value.strip():
        return value.strip()
    return None
