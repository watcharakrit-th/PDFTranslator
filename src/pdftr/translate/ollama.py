"""Ollama: use a running server or start the portable one, and call its HTTP API.

Only the standard library is used, so no extra dependency is needed.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from pdftr.settings import Settings


class OllamaError(RuntimeError):
    pass


class RequestStopped(Exception):
    """The caller's ``stop()`` turned true; the request was abandoned mid-way."""


def _call(url: str, payload: dict | None = None, timeout: float = 900.0) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise OllamaError(f"Ollama answered {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise OllamaError(f"Cannot reach Ollama at {url}: {exc}") from exc


class OllamaClient:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")

    def alive(self) -> bool:
        try:
            _call(self.url + "/api/version", timeout=2)
            return True
        except OllamaError:
            return False

    def version(self) -> str:
        return _call(self.url + "/api/version", timeout=5).get("version", "?")

    def models(self) -> list[str]:
        tags = _call(self.url + "/api/tags", timeout=10).get("models", [])
        return [m.get("name", "") for m in tags]

    def has_model(self, model: str) -> bool:
        names = set(self.models())
        return model in names or f"{model}:latest" in names

    def pull(self, model: str, progress: Callable[[str], None] = print) -> None:
        req = urllib.request.Request(
            self.url + "/api/pull",
            data=json.dumps({"model": model, "stream": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        last = ""
        with urllib.request.urlopen(req, timeout=7200) as resp:
            for raw in resp:
                msg = json.loads(raw.decode("utf-8"))
                if "error" in msg:
                    raise OllamaError(msg["error"])
                status = msg.get("status", "")
                if msg.get("total"):
                    status += f" {100 * msg.get('completed', 0) / msg['total']:.0f}%"
                if status != last:
                    progress(status)
                    last = status

    def chat(
        self,
        model: str,
        prompt: str,
        *,
        temperature: float = 0.2,
        num_ctx: int = 8192,
        num_predict: int = 2048,
        keep_alive: str = "5m",
        stop: Callable[[], bool] | None = None,
        timeout: float = 900.0,
    ) -> dict:
        """One chat request, streamed so that ``stop()`` (checked at every token) can
        abandon it: closing the connection makes Ollama stop generating at once."""
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "options": {"temperature": temperature, "num_ctx": num_ctx, "num_predict": num_predict},
            "keep_alive": keep_alive,
        }
        req = urllib.request.Request(
            self.url + "/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        parts: list[str] = []
        final: dict = {}
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                for raw in resp:
                    if stop is not None and stop():
                        raise RequestStopped
                    if not raw.strip():
                        continue
                    msg = json.loads(raw.decode("utf-8"))
                    if "error" in msg:
                        raise OllamaError(f"Ollama: {msg['error']}")
                    parts.append((msg.get("message") or {}).get("content", ""))
                    if msg.get("done"):
                        final = msg
                        break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            raise OllamaError(f"Ollama answered {exc.code}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise OllamaError(f"Cannot reach Ollama at {self.url}: {exc}") from exc
        if not final:
            raise OllamaError("Ollama ended the answer early")
        final["message"] = {"role": "assistant", "content": "".join(parts)}
        return final

    def unload(self, model: str) -> None:
        """Free the GPU memory now instead of after keep_alive."""
        try:
            _call(self.url + "/api/generate", {"model": model, "keep_alive": 0}, timeout=60)
        except OllamaError:
            pass


class OllamaServer:
    """Context manager: reuse a running Ollama, or start the portable one for the job.

    A server started here is stopped again on exit (with its runner processes,
    which hold the GPU memory).
    """

    def __init__(self, settings: Settings, log: Callable[[str], None] = print) -> None:
        self.settings = settings
        self.client = OllamaClient(settings.ollama_url)
        self.log = log
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> OllamaClient:
        self.start()
        return self.client

    def __exit__(self, *exc) -> None:
        self.stop()

    def executable(self) -> Path:
        if self.settings.ollama_exe:
            return Path(self.settings.ollama_exe)
        name = "ollama.exe" if sys.platform == "win32" else "ollama"
        local = self.settings.cache_dir / "tools" / "ollama" / name
        if local.exists():
            return local
        found = shutil.which("ollama")
        if found:
            return Path(found)
        raise OllamaError("Ollama is not installed (see README: Translation) and none is running.")

    def start(self, wait_s: float = 120.0) -> None:
        if self.client.alive():
            return
        exe = self.executable()
        models = self.settings.ollama_models_dir
        models.mkdir(parents=True, exist_ok=True)
        host = urllib.parse.urlparse(self.settings.ollama_url).netloc or "127.0.0.1:11434"
        env = os.environ | {
            "OLLAMA_MODELS": str(models),
            "OLLAMA_HOST": host,
            "OLLAMA_KEEP_ALIVE": "5m",
        }
        logs = self.settings.data_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True
        with open(logs / "ollama-serve.log", "ab") as out:
            self.proc = subprocess.Popen(
                [str(exe), "serve"], env=env, stdout=out, stderr=subprocess.STDOUT, **kwargs
            )
        self.log(f"Started Ollama ({exe}), models in {models}")
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if self.client.alive():
                return
            if self.proc.poll() is not None:
                raise OllamaError(f"Ollama exited at start; see {logs / 'ollama-serve.log'}")
            time.sleep(0.5)
        raise OllamaError("Ollama did not start in time")

    def stop(self) -> None:
        if self.proc is None:
            return
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        else:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log("Stopped Ollama")
        self.proc = None
