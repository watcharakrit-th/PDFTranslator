"""GPU temperature guard.

The user's RTX 2070 SUPER reaches over 100 °C at its *hot spot* under full load,
while the core temperature that NVML reports stays about 25 °C lower (11 °C at
idle). So every limit here applies to the hot spot, read through NVAPI on Windows
(the sensor HWiNFO and GPU-Z show). Where it cannot be read, it is estimated as
the core temperature plus ``hotspot_margin_c``.

- ``check()`` / ``checkpoint()`` run before each piece of GPU work: while the hot
  spot is at or above ``pause_at_c`` they wait until it cools to ``resume_at_c``.
- ``watch()`` samples the hot spot four times a second during the work and raises
  a flag at ``abort_at_c``, so a translation request can be stopped mid-way.
- ``rest()`` limits the duty cycle, and the peak is recorded for the log.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field


class GpuTooHot(RuntimeError):
    """The hot spot reached the abort limit during a request, which was stopped."""

    def __init__(self, peak_c: int, limit_c: int) -> None:
        super().__init__(f"GPU hot spot reached {peak_c}°C (limit {limit_c}°C)")
        self.peak_c = peak_c
        self.limit_c = limit_c


@dataclass
class GpuStatus:
    temperature_c: int  # core (NVML)
    hotspot_c: int | None  # measured hot spot, None when it cannot be read
    utilization_pct: int
    power_w: float
    power_limit_w: float
    memory_used_mb: int
    memory_total_mb: int
    fan_pct: int | None


@dataclass
class Watch:
    """What ``GpuGuard.watch()`` saw while a piece of work ran."""

    peak_c: int
    hot: threading.Event = field(default_factory=threading.Event)

    def too_hot(self) -> bool:
        return self.hot.is_set()


class _NvapiSensors:
    """GPU thermal sensors through NVAPI (Windows). ``NvAPI_GPU_ThermalGetSensors`` is
    undocumented but stable; HWiNFO, GPU-Z and LibreHardwareMonitor use it. Read-only."""

    INIT, ENUM_GPUS, THERMAL_SENSORS = 0x0150E828, 0xE5AC921F, 0x65FE3AAD

    def __init__(self, index: int) -> None:
        import ctypes
        from ctypes import POINTER, c_int, c_uint, c_void_p

        class Sensors(ctypes.Structure):
            _fields_ = [
                ("version", c_uint),
                ("mask", c_uint),
                ("reserved", c_int * 8),
                ("temps", c_int * 32),  # 1/256 °C
            ]

        dll = ctypes.WinDLL("nvapi64.dll")
        query = dll.nvapi_QueryInterface
        query.restype = c_void_p
        query.argtypes = [c_uint]

        def function(fid: int, *argtypes):
            ptr = query(fid)
            if not ptr:
                raise OSError(f"NVAPI function {fid:#x} is not available")
            return ctypes.CFUNCTYPE(c_int, *argtypes)(ptr)

        if function(self.INIT)() != 0:
            raise OSError("NVAPI did not initialize")
        handles, count = (c_void_p * 64)(), c_uint(0)
        enum_gpus = function(self.ENUM_GPUS, POINTER(c_void_p * 64), POINTER(c_uint))
        if enum_gpus(ctypes.byref(handles), ctypes.byref(count)) != 0 or count.value <= index:
            raise OSError("NVAPI found no GPU")
        self._ctypes = ctypes
        self._struct = Sensors
        self._version = ctypes.sizeof(Sensors) | (2 << 16)
        self._gpu = handles[index]
        self._get = function(self.THERMAL_SENSORS, c_void_p, POINTER(Sensors))
        self._mask = 0
        for bit in range(32):  # sensors are numbered from 0 up to the first missing one
            if self._raw(1 << bit) is None:
                break
            self._mask |= 1 << bit
        if not self._mask:
            raise OSError("NVAPI reports no thermal sensors")

    def _raw(self, mask: int):
        sensors = self._struct(version=self._version, mask=mask)
        if self._get(self._gpu, self._ctypes.byref(sensors)) != 0:
            return None
        return sensors

    def hottest(self) -> int | None:
        """The hottest sensor (index 1 is the hot spot; it is normally the maximum)."""
        sensors = self._raw(self._mask)
        if sensors is None:
            return None
        values = [sensors.temps[i] for i in range(32) if self._mask >> i & 1]
        return round(max(values) / 256) if values else None


class GpuGuard:
    def __init__(
        self,
        pause_at_c: int = 75,
        resume_at_c: int = 65,
        abort_at_c: int = 90,
        cooldown_s: float = 2.0,
        hotspot_margin_c: int = 25,
        index: int = 0,
        log: Callable[[str], None] = print,
    ) -> None:
        if not resume_at_c < pause_at_c < abort_at_c:
            raise ValueError("GPU limits must satisfy resume_at_c < pause_at_c < abort_at_c")
        self.pause_at_c = pause_at_c
        self.resume_at_c = resume_at_c
        self.abort_at_c = abort_at_c
        self.cooldown_s = cooldown_s
        self.hotspot_margin_c = hotspot_margin_c
        self.log = log
        self.peak_c = 0
        self.paused_s = 0.0
        self.rested_s = 0.0
        self.stopped = 0  # requests stopped at the abort limit
        self._lock = threading.Lock()
        self._nv = None
        self._handle = None
        self._nvapi: _NvapiSensors | None = None
        try:
            import pynvml

            pynvml.nvmlInit()
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            self._nv = pynvml
        except Exception as exc:  # noqa: BLE001 - any NVML failure means no monitoring
            self.log(f"GPU guard: NVML unavailable ({exc}); temperature not monitored")
            return
        if sys.platform == "win32":
            try:
                self._nvapi = _NvapiSensors(index)
            except OSError as exc:
                self.log(f"GPU guard: hot spot not readable ({exc}); estimating it")

    @classmethod
    def from_settings(cls, settings, log: Callable[[str], None] = print) -> GpuGuard:
        return cls(
            settings.gpu_pause_at_c,
            settings.gpu_resume_at_c,
            settings.gpu_abort_at_c,
            settings.gpu_cooldown_s,
            settings.gpu_hotspot_margin_c,
            log=log,
        )

    @property
    def available(self) -> bool:
        return self._handle is not None

    @property
    def measures_hotspot(self) -> bool:
        return self._nvapi is not None

    def describe(self) -> str:
        how = "measured" if self.measures_hotspot else f"core + {self.hotspot_margin_c}°C"
        return (
            f"GPU hot spot ({how}): wait at {self.pause_at_c}°C until "
            f"{self.resume_at_c}°C, stop a request at {self.abort_at_c}°C"
        )

    def core_temperature(self) -> int | None:
        if not self.available:
            return None
        with self._lock:
            return int(
                self._nv.nvmlDeviceGetTemperature(self._handle, self._nv.NVML_TEMPERATURE_GPU)
            )

    def measured_hotspot(self) -> int | None:
        if self._nvapi is None:
            return None
        with self._lock:
            return self._nvapi.hottest()

    def temperature(self) -> int | None:
        """The hot spot: every limit applies to this value."""
        core = self.core_temperature()
        if core is None:
            return None
        hot = self.measured_hotspot()
        return max(hot, core) if hot else core + self.hotspot_margin_c

    def status(self) -> GpuStatus | None:
        if not self.available:
            return None
        nv, h = self._nv, self._handle
        with self._lock:
            mem = nv.nvmlDeviceGetMemoryInfo(h)
            try:
                fan = int(nv.nvmlDeviceGetFanSpeed(h))
            except nv.NVMLError:  # fanless or not reported
                fan = None
            utilization = int(nv.nvmlDeviceGetUtilizationRates(h).gpu)
            power = nv.nvmlDeviceGetPowerUsage(h) / 1000.0
            limit = nv.nvmlDeviceGetEnforcedPowerLimit(h) / 1000.0
        return GpuStatus(
            temperature_c=self.core_temperature() or 0,
            hotspot_c=self.measured_hotspot(),
            utilization_pct=utilization,
            power_w=power,
            power_limit_w=limit,
            memory_used_mb=int(mem.used / 2**20),
            memory_total_mb=int(mem.total / 2**20),
            fan_pct=fan,
        )

    def power_limit_w(self) -> float | None:
        if not self.available:
            return None
        with self._lock:
            return self._nv.nvmlDeviceGetEnforcedPowerLimit(self._handle) / 1000.0

    def power_problem(self, max_w: int) -> str | None:
        """Why GPU work should not start: the card's power limit is above ``max_w``
        (it resets to the default after a reboot). None when fine or not checked."""
        limit = self.power_limit_w() if max_w else None
        if limit is None or limit <= max_w + 0.5:
            return None
        return (
            f"The GPU power limit is {limit:.0f} W, above the {max_w} W set in .env "
            f"(PDFTR_GPU_MAX_POWER_W). It resets after a reboot. In an Administrator "
            f"PowerShell run:  nvidia-smi -pl {max_w}"
        )

    def cool_down(self, reason: str) -> None:
        """Wait until the hot spot is back at ``resume_at_c``."""
        temp = self.temperature()
        self.log(f"{reason}: waiting until the GPU hot spot cools to {self.resume_at_c}°C")
        started = time.monotonic()
        while temp is not None and temp > self.resume_at_c:
            time.sleep(2)
            temp = self.temperature()
        waited = time.monotonic() - started
        self.paused_s += waited
        self.log(f"GPU hot spot at {temp}°C after {waited:.0f}s; resuming")

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
        self.cool_down(f"GPU hot spot at {temp}°C (limit {self.pause_at_c}°C)")

    def check(self) -> None:
        """Wait while the GPU is too hot (no fixed cool-down). Quiet when it is cool."""
        temp = self.temperature()
        if temp is None:
            return
        self.peak_c = max(self.peak_c, temp)
        if temp >= self.pause_at_c:
            self.cool_down(f"GPU hot spot at {temp}°C (limit {self.pause_at_c}°C)")

    def rest(self, busy_s: float, ratio: float) -> None:
        """Duty-cycle limit: after ``busy_s`` seconds of GPU work, rest ``busy_s * ratio``."""
        if ratio > 0 and busy_s > 0:
            pause = min(busy_s * ratio, 30.0)
            time.sleep(pause)
            self.rested_s += pause

    @contextmanager
    def watch(self, interval_s: float = 0.25):
        """Sample the hot spot in the background while a piece of work runs.

        Yields a ``Watch``: ``peak_c`` holds the highest reading and ``too_hot()``
        turns true once a reading reaches ``abort_at_c``.
        """
        result = Watch(peak_c=self.temperature() or 0)
        if not self.available:
            yield result
            return
        stop = threading.Event()

        def sample() -> None:
            while not stop.wait(interval_s):
                t = self.temperature() or 0
                result.peak_c = max(result.peak_c, t)
                if t >= self.abort_at_c:
                    result.hot.set()

        thread = threading.Thread(target=sample, daemon=True)
        thread.start()
        try:
            yield result
        finally:
            stop.set()
            thread.join(timeout=2 * interval_s + 1)
            self.peak_c = max(self.peak_c, result.peak_c)
