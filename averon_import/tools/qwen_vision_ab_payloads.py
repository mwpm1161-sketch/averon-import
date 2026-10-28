"""Offline-only preparation of equal-byte contextual request controls.

There is deliberately no transport, file input, CLI or live-run entry point.
Bodies are ephemeral and excluded from repr; callers must not log them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json

from averon_import.services.ocr.qwen_vision_client import (
    QwenVisionClient, QwenVisionError, _PROMPT, _USER_CONTEXT_LABEL, _USER_CONTEXT_POLICY, validate_user_context,
)


@dataclass(frozen=True, slots=True)
class ContextABPayload:
    case: str
    system_message_count: int
    user_context_present: bool
    body: bytes = field(repr=False)

    @property
    def request_bytes(self) -> int:
        return len(self.body)


def prepare_context_ab(
    client: QwenVisionClient, image: bytes, page: int, user_context: str,
) -> tuple[ContextABPayload, ContextABPayload, ContextABPayload]:
    """A/B keep hint/policy/image fixed; C empties the hint only if needed.

    All bodies use the largest native serialization length, with only legal
    trailing JSON whitespace added to shorter bodies. Unknown client layouts
    fail closed instead of silently changing the experimental contrast.
    """
    context = validate_user_context(user_context)
    if context is None:
        raise QwenVisionError("ab_context_required")
    client.measure_request_bytes(image, page, user_context=context)
    merged = json.loads(client._request_body(image, page, context))
    if ([m.get("role") for m in merged["messages"]] != ["system", "user"]
            or merged["messages"][0]["content"] != _PROMPT + "\n\n" + _USER_CONTEXT_POLICY):
        raise QwenVisionError("ab_context_layout_mismatch")
    legacy = json.loads(json.dumps(merged, ensure_ascii=False))
    legacy["messages"][0]["content"] = _PROMPT
    legacy["messages"].insert(1, {"role": "system", "content": _USER_CONTEXT_POLICY})
    no_hint = json.loads(json.dumps(merged, ensure_ascii=False))
    # Keep the same text-part count and label, removing only the hint itself.
    no_hint["messages"][-1]["content"][1]["text"] = _USER_CONTEXT_LABEL
    variants = [
        ("A_two_system_context", 2, True, legacy),
        ("B_one_system_context", 1, True, merged),
        ("C_one_system_empty_context", 1, False, no_hint),
    ]
    encoded = [(case, count, hint, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
               for case, count, hint, value in variants]
    size = max(len(body) for _, _, _, body in encoded)
    if size > client.config.limits.max_request_bytes:
        raise QwenVisionError("request_size_limit")
    return tuple(ContextABPayload(case, count, hint, body + b" " * (size - len(body)))
                 for case, count, hint, body in encoded)
