"""Run independent model calls at the same time.

One shared pool for the process (CHECK_WORKERS threads; 1 or less runs everything inline, as before).
Each task runs in a copy of the caller's context, so the per-question meter still counts its calls and
enforces the budget. Only the main generator thread ever submits work, so tasks never wait on each other.
"""
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context

from .. import config

_pool: ThreadPoolExecutor | None = None


def enabled() -> bool:
    return config.CHECK_WORKERS > 1


def submit(fn, *args) -> Future:
    """Start fn(*args) in the pool (or run it now when parallel checking is off). Errors surface on .result()."""
    global _pool
    if not enabled():
        f: Future = Future()
        try:
            f.set_result(fn(*args))
        except Exception as e:  # noqa: BLE001  (re-raised by .result(), like a pooled task)
            f.set_exception(e)
        return f
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=config.CHECK_WORKERS, thread_name_prefix="verify")
    return _pool.submit(copy_context().run, fn, *args)
