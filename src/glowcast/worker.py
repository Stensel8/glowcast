"""One background thread for slow bridge work, so the window never blocks."""

import threading
from collections.abc import Callable

from glowcast.utils.logging import timed_print


class Worker:
    """Runs jobs in order on a single thread.

    Submitting a job under a key that is still waiting replaces it (and moves
    it to the back), so a burst of clicks or slider moves collapses into the
    newest one.
    """

    def __init__(self):
        self._jobs: dict[str, Callable[[], None]] = {}
        self._busy = False
        self._condition = threading.Condition()
        threading.Thread(target=self._run, daemon=True, name="GlowcastWorker").start()

    def submit(self, key: str, job: Callable[[], None]) -> None:
        with self._condition:
            self._jobs.pop(key, None)
            self._jobs[key] = job
            self._condition.notify_all()

    def is_pending(self, key: str | None = None) -> bool:
        """Whether a job (with this key) is waiting to run."""
        with self._condition:
            return bool(self._jobs) if key is None else key in self._jobs

    def wait_idle(self, timeout: float) -> bool:
        """Wait until all jobs have run; False on timeout."""
        with self._condition:
            return self._condition.wait_for(lambda: not self._jobs and not self._busy, timeout)

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._jobs)
                key = next(iter(self._jobs))
                job = self._jobs.pop(key)
                self._busy = True
            try:
                job()
            except Exception as e:
                timed_print(f"Background job '{key}' failed: {e}")
            finally:
                with self._condition:
                    self._busy = False
                    self._condition.notify_all()
