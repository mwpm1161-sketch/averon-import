"""Private local storage helpers for the isolated AI-first pilot.

On Windows, pilot directories are created with a protected DACL and then
read back through the native security APIs. No localized ``icacls`` output is
used as evidence of the resulting permissions.
"""
from __future__ import annotations

import ctypes
from functools import lru_cache
import os
from pathlib import Path
import re
import subprocess
from typing import Any


class PrivateAclError(RuntimeError):
    """A path's private permissions could not be established or verified."""


_SID_RE = re.compile(r"^S-\d-(?:\d+-)*\d+$")
_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"
_FULL_FILE_ACCESS = 0x001F01FF
_OBJECT_INHERIT_ACE = 0x01
_CONTAINER_INHERIT_ACE = 0x02
_INHERITED_ACE = 0x10
_SE_DACL_PROTECTED = 0x1000
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_FILE_OBJECT = 1
_ACL_SIZE_INFORMATION = 2
_ACCESS_ALLOWED_ACE_TYPE = 0
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@lru_cache(maxsize=1)
def _current_user_sid() -> str:
    try:
        result = subprocess.run(
            ["whoami.exe", "/user", "/fo", "csv", "/nh"],
            check=True, capture_output=True, text=True, encoding="utf-8",
            errors="replace", stdin=subprocess.DEVNULL, timeout=10,
        )
        import csv
        rows = list(csv.reader(result.stdout.splitlines()))
        sid = rows[0][-1].strip() if rows else ""
    except Exception:
        raise PrivateAclError("private_acl_unverified") from None
    if not _SID_RE.fullmatch(sid):
        raise PrivateAclError("private_acl_unverified")
    return sid


def _windows_apis() -> tuple[Any, Any]:
    if os.name != "nt":
        raise PrivateAclError("private_acl_unverified")
    try:
        from ctypes import wintypes

        advapi = ctypes.WinDLL("Advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("Kernel32", use_last_error=True)
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        advapi.GetSecurityDescriptorDacl.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL),
        ]
        advapi.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        advapi.SetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]
        advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi.GetSecurityDescriptorControl.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
        advapi.GetAclInformation.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        ]
        advapi.GetAclInformation.restype = wintypes.BOOL
        advapi.GetAce.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi.GetAce.restype = wintypes.BOOL
        advapi.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR),
        ]
        advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        return advapi, kernel
    except Exception:
        raise PrivateAclError("private_acl_unverified") from None


def _sddl(*, sid: str, directory: bool) -> str:
    if not _SID_RE.fullmatch(sid):
        raise PrivateAclError("private_acl_unverified")
    inherit = "OICI" if directory else ""
    return (
        "D:P"
        f"(A;{inherit};FA;;;{sid})"
        f"(A;{inherit};FA;;;{_SYSTEM_SID})"
        f"(A;{inherit};FA;;;{_ADMINISTRATORS_SID})"
    )


def _reject_reparse(path: Path) -> None:
    try:
        if path.is_symlink():
            raise PrivateAclError("private_acl_unverified")
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise PrivateAclError("private_acl_unverified")
    except PrivateAclError:
        raise
    except Exception:
        raise PrivateAclError("private_acl_unverified") from None


def _apply_windows_dacl(path: Path, *, sid: str, directory: bool) -> None:
    advapi, kernel = _windows_apis()
    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    present = ctypes.c_int()
    defaulted = ctypes.c_int()
    try:
        sddl = _sddl(sid=sid, directory=directory)
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None,
        ):
            raise PrivateAclError("private_acl_unverified")
        if not advapi.GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted),
        ) or not present.value or not dacl.value:
            raise PrivateAclError("private_acl_unverified")
        result = advapi.SetNamedSecurityInfoW(
            str(path), _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, dacl, None,
        )
        if result != 0:
            raise PrivateAclError("private_acl_unverified")
    except PrivateAclError:
        raise
    except Exception:
        raise PrivateAclError("private_acl_unverified") from None
    finally:
        if descriptor.value:
            kernel.LocalFree(descriptor)


def _read_windows_dacl(path: Path) -> tuple[bool, list[tuple[str, int, int]]]:
    advapi, kernel = _windows_apis()
    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    entries: list[tuple[str, int, int]] = []
    try:
        result = advapi.GetNamedSecurityInfoW(
            str(path), _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION,
            None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor),
        )
        if result != 0 or not descriptor.value or not dacl.value:
            raise PrivateAclError("private_acl_unverified")

        control = ctypes.c_ushort()
        revision = ctypes.c_ulong()
        if not advapi.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision),
        ):
            raise PrivateAclError("private_acl_unverified")

        class AclSizeInformation(ctypes.Structure):
            _fields_ = [
                ("AceCount", ctypes.c_ulong),
                ("AclBytesInUse", ctypes.c_ulong),
                ("AclBytesFree", ctypes.c_ulong),
            ]

        size_info = AclSizeInformation()
        if not advapi.GetAclInformation(
            dacl, ctypes.byref(size_info), ctypes.sizeof(size_info), _ACL_SIZE_INFORMATION,
        ):
            raise PrivateAclError("private_acl_unverified")
        for index in range(size_info.AceCount):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                raise PrivateAclError("private_acl_unverified")
            header = ctypes.string_at(ace, 8)
            ace_type, ace_flags, _ace_size = header[0], header[1], int.from_bytes(header[2:4], "little")
            access_mask = int.from_bytes(header[4:8], "little")
            if ace_type != _ACCESS_ALLOWED_ACE_TYPE:
                raise PrivateAclError("private_acl_unverified")
            sid_pointer = ctypes.c_void_p(ace.value + 8)
            sid_string = ctypes.c_wchar_p()
            if not advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_string)):
                raise PrivateAclError("private_acl_unverified")
            try:
                entries.append((sid_string.value or "", access_mask, ace_flags))
            finally:
                if sid_string:
                    kernel.LocalFree(ctypes.cast(sid_string, ctypes.c_void_p))
        return bool(control.value & _SE_DACL_PROTECTED), entries
    except PrivateAclError:
        raise
    except Exception:
        raise PrivateAclError("private_acl_unverified") from None
    finally:
        if descriptor.value:
            kernel.LocalFree(descriptor)


def verify_private_path(path: str | os.PathLike[str], *, directory: bool | None = None) -> None:
    """Require an exact private DACL (Windows) or private mode (POSIX)."""
    target = Path(path)
    _reject_reparse(target)
    is_directory = target.is_dir() if directory is None else directory
    if os.name != "nt":
        try:
            mode = target.stat().st_mode & 0o777
        except OSError:
            raise PrivateAclError("private_acl_unverified") from None
        if mode & 0o077:
            raise PrivateAclError("private_acl_unverified")
        return

    sid = _current_user_sid()
    protected, entries = _read_windows_dacl(target)
    expected_sids = {sid, _SYSTEM_SID, _ADMINISTRATORS_SID}
    if len(entries) != 3 or {entry[0].upper() for entry in entries} != {value.upper() for value in expected_sids}:
        raise PrivateAclError("private_acl_unverified")
    if any(mask != _FULL_FILE_ACCESS for _sid, mask, _flags in entries):
        raise PrivateAclError("private_acl_unverified")

    flags = {entry[2] for entry in entries}
    if is_directory:
        if protected:
            if flags != {_OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE}:
                raise PrivateAclError("private_acl_unverified")
        elif flags != {_INHERITED_ACE | _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE}:
            raise PrivateAclError("private_acl_unverified")
    else:
        if protected:
            if flags != {0}:
                raise PrivateAclError("private_acl_unverified")
        elif flags != {_INHERITED_ACE}:
            raise PrivateAclError("private_acl_unverified")


def secure_private_path(path: str | os.PathLike[str], *, directory: bool) -> None:
    """Set an exact protected DACL and verify the resulting path."""
    target = Path(path)
    _reject_reparse(target)
    if not target.exists():
        raise PrivateAclError("private_acl_unverified")
    if os.name != "nt":
        try:
            os.chmod(target, 0o700 if directory else 0o600)
        except OSError:
            raise PrivateAclError("private_acl_unverified") from None
        verify_private_path(target, directory=directory)
        return
    sid = _current_user_sid()
    _apply_windows_dacl(target, sid=sid, directory=directory)
    verify_private_path(target, directory=directory)


def create_private_directory(path: str | os.PathLike[str]) -> Path:
    """Create a directory with its final protected DACL applied atomically."""
    target = Path(path)
    created = False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            target.mkdir(mode=0o700)
            created = True
            secure_private_path(target, directory=True)
            return target

        advapi, kernel = _windows_apis()
        sid = _current_user_sid()
        descriptor = ctypes.c_void_p()
        try:
            sddl = _sddl(sid=sid, directory=True)
            if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None,
            ):
                raise PrivateAclError("private_acl_unverified")

            class SecurityAttributes(ctypes.Structure):
                _fields_ = [
                    ("nLength", ctypes.c_ulong),
                    ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", ctypes.c_int),
                ]

            attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, 0)
            kernel.CreateDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(SecurityAttributes)]
            kernel.CreateDirectoryW.restype = ctypes.c_int
            if not kernel.CreateDirectoryW(str(target), ctypes.byref(attributes)):
                raise PrivateAclError("private_acl_unverified")
            created = True
        finally:
            if descriptor.value:
                kernel.LocalFree(descriptor)
        verify_private_path(target, directory=True)
        return target
    except PrivateAclError:
        if created:
            try:
                target.rmdir()
            except OSError:
                pass
        raise
    except Exception:
        if created:
            try:
                target.rmdir()
            except OSError:
                pass
        raise PrivateAclError("private_acl_unverified") from None


def secure_private_tree(root: str | os.PathLike[str]) -> int:
    """Secure the root and every existing descendant, returning item count."""
    target = Path(root)
    try:
        if not target.is_dir():
            raise PrivateAclError("private_acl_unverified")
        _reject_reparse(target)
        secure_private_path(target, directory=True)
        items = sorted(
            target.rglob("*"),
            key=lambda item: (len(item.relative_to(target).parts), item.as_posix()),
        )
        for item in items:
            _reject_reparse(item)
            secure_private_path(item, directory=item.is_dir())
        verify_private_tree(target)
        return len(items)
    except PrivateAclError:
        raise
    except Exception:
        raise PrivateAclError("private_acl_unverified") from None


def verify_private_tree(root: str | os.PathLike[str]) -> int:
    """Read back every DACL/mode below ``root``; return verified item count."""
    target = Path(root)
    try:
        if not target.is_dir():
            raise PrivateAclError("private_acl_unverified")
        _reject_reparse(target)
        verify_private_path(target, directory=True)
        items = list(target.rglob("*"))
        for item in items:
            _reject_reparse(item)
            verify_private_path(item, directory=item.is_dir())
        return len(items) + 1
    except PrivateAclError:
        raise
    except Exception:
        raise PrivateAclError("private_acl_unverified") from None
