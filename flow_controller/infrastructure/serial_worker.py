"""Dedicated asynchronous worker for serial communication."""

import asyncio
import threading


class SerialIOWorker:
    """Own the single asyncio event loop used for all serial communication."""

    def __init__(self):
        self._loop = None
        self._ready = threading.Event()
        self._startup_error = None
        self._thread = threading.Thread(
            target=self._run, name="alicat-serial-io", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=3.0):
            raise RuntimeError("Serial I/O worker did not start")
        if self._startup_error is not None:
            raise RuntimeError("Serial I/O worker failed to start") from self._startup_error

    def _run(self):
        loop = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
        except Exception as exc:
            self._startup_error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            self._loop = None

    def submit(self, coroutine):
        loop = self._loop
        if loop is None or not loop.is_running():
            coroutine.close()
            raise RuntimeError("Serial I/O worker is not running")
        return asyncio.run_coroutine_threadsafe(coroutine, loop)

    def shutdown(self, timeout=2.0):
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=timeout)
