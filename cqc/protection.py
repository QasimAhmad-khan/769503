"""Continuous protection loop, independent of inference.

Runs `PaperRuntime.protect` on a timer (default 1 s) and immediately on market/account events,
in its own thread. It takes only the account lock — never the Phi inference queue — so a slow,
stuck or crashed Phi call cannot delay reconciliation, stops or emergency reduce-only exits.
"""
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timedelta

from .util import UTC


class ProtectionLoop(threading.Thread):
    def __init__(self, runtime, interval_s: float = 1.0, clock=None):
        super().__init__(name="protection-loop", daemon=True)
        self.runtime = runtime
        self.interval = interval_s
        self.clock = clock or (lambda: datetime.now(UTC))
        self.events: queue.Queue = queue.Queue()
        self._halt = threading.Event()
        self.runs = 0
        self.errors: list[str] = []
        self.reactions: list[dict] = []  # event -> protective action latency (wall clock)

    def notify(self, event: str = "market_update"):
        self.events.put((event, time.perf_counter()))

    def stop(self):
        self._halt.set()
        self.events.put(("stop", time.perf_counter()))

    def run(self):
        while not self._halt.is_set():
            try:
                event, t_event = self.events.get(timeout=self.interval)
            except queue.Empty:
                event, t_event = "timer", time.perf_counter()
            if event == "stop":
                break
            try:
                actions = self.runtime.protect(self.clock())
                self.runs += 1
                if actions:
                    self.reactions.append({"event": event, "actions": [a["type"] for a in actions],
                                           "latency_ms": round((time.perf_counter() - t_event) * 1000, 3)})
            except Exception as exc:  # the loop must survive; failures are recorded for operators
                self.errors.append(f"{type(exc).__name__}: {exc}")


def sim_clock(start: datetime):
    """Simulation clock advancing with wall time from a fixed simulated start."""
    t0 = time.monotonic()
    return lambda: start + timedelta(seconds=time.monotonic() - t0)
