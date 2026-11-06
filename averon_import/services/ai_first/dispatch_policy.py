"""Explicit, process-sticky isolation of the durable dispatcher from legacy POSTs.

This is a current-runtime guard, not the old-binary OS egress gate of B5.
No settings, secrets, storage or live clients are created by importing it.
"""
import os
import threading

from .coordination_contracts import CoordinationError

_lock = threading.Lock()
_required = False


def durable_required():
    global _required
    with _lock:
        value = os.environ.get("AVERON_AI_DURABLE_DISPATCH", "0")
        if value != "0":
            _required = True
        if value not in ("0", "1"):
            raise CoordinationError("invalid_durable_mode")
        return _required


def deny_legacy_post():
    if durable_required():
        raise CoordinationError("durable_authorization_required")
