"""One lazily spawned, sequential PyMuPDF renderer, isolated from app threads."""
from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
import hashlib
import multiprocessing
import os
import threading
import time

from averon_import.services.ai_first.dispatch import RENDER_CAPACITY

_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()


def _watch_parent():
    parent = multiprocessing.parent_process()
    if parent is None:
        return

    def watch():
        while parent.is_alive():
            time.sleep(0.1)
        # The renderer owns no checkpoints or HTTP requests. Do not keep an
        # idle executor child alive after an abrupt application-process exit.
        os._exit(0)

    threading.Thread(target=watch, name="pdf-parent-watch", daemon=True).start()


def _render(pdf_path, page_number, dpi, config):
    from averon_import.services.ai_first.orchestration import FitZPageRenderer
    return FitZPageRenderer().render(pdf_path, page_number, dpi, config)


def _page_count(pdf_path):
    import fitz
    with fitz.open(pdf_path) as document:
        return document.page_count


def _region_points(pdf_path, regions):
    import fitz
    with fitz.open(pdf_path) as document:
        result = {}
        for number, (x, y, width, height) in regions.items():
            rect = document.load_page(number - 1).rect
            result[str(number)] = [rect.x0 + x * rect.width, rect.y0 + y * rect.height,
                                   rect.x0 + (x + width) * rect.width, rect.y0 + (y + height) * rect.height]
        return result


def _inspect_source(pdf_path):
    import fitz
    from pathlib import Path
    digest = hashlib.sha256()
    with Path(pdf_path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    with fitz.open(pdf_path) as document:
        page_count = document.page_count
    return digest.hexdigest(), page_count


def _get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"),
                                        initializer=_watch_parent)
        return _pool


def _discard_pool():
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown(wait=False, cancel_futures=True)
            _pool = None


def _submit(call, *args):
    # The slot is acquired before submission and released by the executor's
    # completion thread. Synchronous metadata inspection can safely wait even
    # when its event loop also owns a pending asynchronous render.
    try:
        future = _get_pool().submit(call, *args)
    except BaseException:
        RENDER_CAPACITY.release()
        raise
    future.add_done_callback(lambda _future: RENDER_CAPACITY.release())
    return future


def _call_sync(call, *args):
    RENDER_CAPACITY.acquire_sync()
    try:
        return _submit(call, *args).result()
    except BrokenProcessPool:
        _discard_pool()
        raise


def pdf_page_count(pdf_path):
    return _call_sync(_page_count, str(pdf_path))


def page_region_points(pdf_path, regions):
    return _call_sync(_region_points, str(pdf_path), regions)


async def render_page(pdf_path, page_number, dpi, config):
    await RENDER_CAPACITY.acquire()
    future = None
    try:
        future = asyncio.wrap_future(_submit(_render, str(pdf_path), page_number, dpi, config))
        return await asyncio.shield(future)
    except BrokenProcessPool:
        # Fail this page without retrying it. A later explicit run may
        # create a fresh worker after a native renderer crash. submit() itself
        # may raise if the worker died while it was idle.
        _discard_pool()
        raise
    except asyncio.CancelledError:
        # A running native operation cannot be cancelled safely. Drain it
        # before releasing capacity; never leave orphan renders on restart.
        while future is not None and not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except BrokenProcessPool:
                _discard_pool()
                break
            except Exception:
                break
        if future is not None and not future.cancelled():
            future.exception()
        raise


async def _call_async(call, *args):
    """Wait for isolated PDF work without blocking its caller's event loop."""
    await RENDER_CAPACITY.acquire()
    future = None
    try:
        future = asyncio.wrap_future(_submit(call, *args))
        return await asyncio.shield(future)
    except BrokenProcessPool:
        _discard_pool()
        raise
    except asyncio.CancelledError:
        # A process operation cannot be interrupted safely. Drain it before
        # surfacing cancellation; the done callback releases its slot.
        while future is not None and not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except BrokenProcessPool:
                _discard_pool()
                break
            except Exception:
                break
        if future is not None and not future.cancelled():
            future.exception()
        raise


async def inspect_source_async(pdf_path):
    return await _call_async(_inspect_source, str(pdf_path))


async def page_region_points_async(pdf_path, regions):
    return await _call_async(_region_points, str(pdf_path), regions)
