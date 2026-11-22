"""GPU temperature guard.

The user's RTX 2070 SUPER can overheat under sustained full load, so GPU work
is split into small batches and ``GpuGuard.checkpoint()`` runs between them:
it waits a short cool-down, and pauses while the GPU is hotter than the
configured limit. A background sampler records the peak temperature reached
during each batch so the log shows what really happened.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass
class GpuStatus:
    temperature_c: int
    utilization_pct: int
    power_w: float
    memory_used_mb: int
    memory_total_mb: int
    fan_pct: int | None


class GpuGuard:
    def __init__(
        self,
        pause_at_c: int = 75,
        resume_at_c: int = 65,
        cooldown_s: float = 2.0,
        index: int = 0,
        log: Callable[[str], None] = print,
    ) -> None:
        if resume_at_c >= pause_at_c:
            raise ValueError("resume_at_c must be lower than pause_at_c")
        self.pause_at_c = pause_at_c
        self.resume_at_c = resume_at_c
        self.cooldown_s = cooldown_s
        self.log = log
        self.peak_c = 0
        self.paused_s = 0.0
        self._nv = None
        self._handle = None
        try:
            import pynvml

            pynvml.nvmlInit()
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            self._nv = pynvml
        except Exception as exc:  # no NVIDIA driver / no GPU
            self.log(f"GPU guard: NVML unavailable ({exc}); temperature not monitored")

    @property
    def available(self) -> bool:
        return self._handle is not None

    def temperature(self) -> int | None:
        if not self.available:
            return None
        return int(self._nv.nvmlDeviceGetTemperature(self._handle, self._nv.NVML_TEMPERATURE_GPU))

    def status(self) -> GpuStatus | None:
        if not self.available:
            return None
        nv, h = self._nv, self._handle
        mem = nv.nvmlDeviceGetMemoryInfo(h)
        try:
            fan = int(nv.nvmlDeviceGetFanSpeed(h))
        except Exception:
            fan = None
        return GpuStatus(
            temperature_c=self.temperature() or 0,
            utilization_pct=int(nv.nvmlDeviceGetUtilizationRates(h).gpu),
            power_w=nv.nvmlDeviceGetPowerUsage(h) / 1000.0,
            memory_used_mb=int(mem.used / 2**20),
            memory_total_mb=int(mem.total / 2**20),
            fan_pct=fan,
        )

    def checkpoint(self) -> None:
        """Call between batches: cool down briefly, and wait while too hot."""
        temp = self.temperature()
        if temp is None:
            return
        self.peak_c = max(self.peak_c, temp)
        if temp < self.pause_at_c:
            if self.cooldown_s > 0:
                time.sleep(self.cooldown_s)
            return
        self.log(
            f"GPU at {temp}°C (limit {self.pause_at_c}°C): pausing until it cools "
            f"to {self.resume_at_c}°C"
        )
        started = time.monotonic()
        while temp is not None and temp > self.resume_at_c:
            time.sleep(5)
            temp = self.temperature()
        waited = time.monotonic() - started
        self.paused_s += waited
        self.log(f"GPU cooled to {temp}°C after {waited:.0f}s; resuming")

    @contextmanager
    def watch(self, interval_s: float = 1.0):
        """Sample the temperature in the background while a batch runs.

        Yields a dict that ends up holding ``peak_c`` for the batch.
        """
        result = {"peak_c": self.temperature() or 0}
        if not self.available:
            yield result
            return
        stop = threading.Event()

        def sample() -> None:
            while not stop.wait(interval_s):
                t = self.temperature() or 0
                result["peak_c"] = max(result["peak_c"], t)

        thread = threading.Thread(target=sample, daemon=True)
        thread.start()
        try:
            yield result
        finally:
            stop.set()
            thread.join(timeout=2 * interval_s)
            self.peak_c = max(self.peak_c, result["peak_c"])
