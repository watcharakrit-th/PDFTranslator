"""GPU guard, guarded engine and stoppable Ollama requests (no real GPU work)."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pdftr import gpu
from pdftr.gpu import GpuGuard, GpuTooHot
from pdftr.translate.engine import GuardedEngine, Result
from pdftr.translate.ollama import OllamaClient, RequestStopped


@pytest.fixture
def sensor(monkeypatch):
    """A fake hot spot: tests set ``sensor["now"]``; sleeping cools it by 10 °C."""
    state = {"now": 60, "slept": []}

    def fake_sleep(seconds):
        state["slept"].append(seconds)
        state["now"] = max(55, state["now"] - 10)

    monkeypatch.setattr(GpuGuard, "available", property(lambda self: True))
    monkeypatch.setattr(GpuGuard, "core_temperature", lambda self: state["now"] - 20)
    monkeypatch.setattr(GpuGuard, "measured_hotspot", lambda self: state["now"])
    monkeypatch.setattr(gpu.time, "sleep", fake_sleep)
    return state


def make_guard():
    logs: list[str] = []
    return GpuGuard(pause_at_c=75, resume_at_c=65, abort_at_c=90, log=logs.append), logs


class SlowEngine:
    """Generates 'tokens' until told to stop; heats the fake GPU while it runs."""

    name = "slow"

    def __init__(self, sensor, heat_to=None):
        self.sensor = sensor
        self.heat_to = heat_to

    def translate(self, text, rules, *, retry=False, stop=None):
        if self.heat_to is not None:
            self.sensor["now"] = self.heat_to
        deadline = time.monotonic() + (5 if self.heat_to else 0.05)
        while time.monotonic() < deadline:
            if stop is not None and stop():
                self.sensor["now"] = 60  # stopping lets it cool
                raise RequestStopped
            threading.Event().wait(0.01)
        return Result("แปลแล้ว", 0.05, 3)


def test_limits_must_be_ordered():
    with pytest.raises(ValueError):
        GpuGuard(pause_at_c=80, resume_at_c=65, abort_at_c=80, log=lambda _m: None)


def test_hot_spot_is_estimated_when_it_cannot_be_read(monkeypatch, sensor):
    monkeypatch.setattr(GpuGuard, "measured_hotspot", lambda self: None)
    guard, _logs = make_guard()
    assert guard.temperature() == sensor["now"] - 20 + guard.hotspot_margin_c


def test_waits_while_hot_then_resumes(sensor):
    guard, logs = make_guard()
    sensor["now"] = 82
    guard.check()
    assert sensor["now"] <= 65 and guard.peak_c == 82
    assert any("waiting until the GPU hot spot cools to 65°C" in m for m in logs)


def test_watch_raises_the_flag_at_the_abort_limit(sensor):
    guard, _logs = make_guard()
    with guard.watch(interval_s=0.02) as watch:
        sensor["now"] = 93
        deadline = time.monotonic() + 2
        while not watch.too_hot() and time.monotonic() < deadline:
            threading.Event().wait(0.01)
    assert watch.too_hot() and watch.peak_c == 93 and guard.peak_c == 93


def test_guarded_engine_stops_a_request_that_gets_too_hot(sensor, monkeypatch):
    monkeypatch.setattr(GpuGuard, "watch", _fast_watch(GpuGuard.watch))
    guard, logs = make_guard()
    engine = GuardedEngine(SlowEngine(sensor, heat_to=95), guard, rest_ratio=1.0)
    with pytest.raises(GpuTooHot) as info:
        engine.translate("text", [])
    assert info.value.peak_c >= 90 and guard.stopped == 1
    assert any("request stopped" in m for m in logs)


def test_guarded_engine_rests_after_a_normal_request(sensor):
    guard, _logs = make_guard()
    engine = GuardedEngine(SlowEngine(sensor), guard, rest_ratio=1.0)
    assert engine.translate("text", []).text == "แปลแล้ว"
    assert engine.name == "slow" and guard.rested_s > 0 and sensor["slept"]


def _fast_watch(original):
    def watch(self, interval_s=0.25):
        return original(self, interval_s=0.02)

    return watch


# ------------------------------------------------------------------ Ollama streaming
class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        try:
            for word in ["สวัสดี", "ครับ", "โลก"] * 50:
                line = {"message": {"role": "assistant", "content": word}, "done": False}
                self.wfile.write(json.dumps(line).encode() + b"\n")
                self.wfile.flush()
                time.sleep(0.002)
            self.wfile.write(json.dumps({"done": True, "eval_count": 150}).encode() + b"\n")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_ollama():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_streamed_chat_collects_the_answer(fake_ollama):
    reply = OllamaClient(fake_ollama).chat("m", "hi")
    assert reply["message"]["content"].startswith("สวัสดีครับโลก") and reply["eval_count"] == 150


def test_streamed_chat_can_be_stopped(fake_ollama):
    seen = {"n": 0}

    def stop():
        seen["n"] += 1
        return seen["n"] > 5

    with pytest.raises(RequestStopped):
        OllamaClient(fake_ollama).chat("m", "hi", stop=stop)
    assert seen["n"] == 6
