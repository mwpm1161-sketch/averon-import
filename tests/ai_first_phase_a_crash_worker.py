"""Killed by the parent test at a real checkpoint/transport boundary; offline."""
from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image
from averon_import.services.ai_first.orchestration import PageImage
from test_ai_first_qwen_vision_adapter import (
    FakeTransport, _adapter, _make_orchestrator, _orchestration_config, _response,
)


async def main():
    pdf, checkpoint, ready = map(Path, sys.argv[1:4])
    phase = sys.argv[4]

    def handler(page, _call, _kwargs):
        if page == 2 and phase == "transport":
            ready.write_text("ready", encoding="ascii")
            threading.Event().wait(60)
        return _response(page)

    runner = await asyncio.to_thread(
        _make_orchestrator, pdf, _adapter(FakeTransport(handler)),
        _orchestration_config(concurrency=1), checkpoint,
    )

    class LocalSyntheticRenderer:
        def render(self, _path, number, dpi, _config):
            data = BytesIO()
            Image.new("RGB", (16, 16), "white").save(data, format="PNG")
            return PageImage(number, data.getvalue(), "image/png", 16, 16, dpi)

    runner.renderer = LocalSyntheticRenderer()
    original = runner.checkpoint.write

    async def write(record):
        await original(record)
        if record.page_number == 2 and record.state == "running":
            has_intent = record.attempts[-1].dispatch_intent_at is not None
            if (phase == "prepared" and not has_intent) or (phase == "intent" and has_intent):
                ready.write_text("ready", encoding="ascii")
                await asyncio.Event().wait()

    runner.checkpoint.write = write
    await runner.run(pdf)


if __name__ == "__main__":
    asyncio.run(main())
