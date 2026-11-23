"""Translation engines: Typhoon on this PC (via Ollama) and Claude (Anthropic API).

Every engine has ``name`` (part of each translation-memory key), ``workers`` (how many
requests may run at once: 1 for the GPU) and ``translate(text, rules)``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from pdftr.gpu import GpuGuard, GpuTooHot
from pdftr.settings import Settings
from pdftr.translate.ollama import OllamaClient, RequestStopped


@dataclass
class Result:
    text: str
    seconds: float
    tokens: int  # output tokens
    flags: tuple[str, ...] = ()  # e.g. ("refused",): the engine declined this text
    input_tokens: int = 0


class Engine(Protocol):
    name: str
    workers: int

    def translate(
        self,
        text: str,
        rules: list[str],
        *,
        retry: bool = False,
        stop: Callable[[], bool] | None = None,
    ) -> Result:
        """``stop`` is checked while the answer is generated; when it turns true the
        engine abandons the request and raises ``RequestStopped``."""
        ...


THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
THAI_LETTER_RE = re.compile("[" + chr(0x0E01) + "-" + chr(0x0E5B) + "]")
NEWLINE = chr(10)
PREFIX_RE = re.compile(r"^(?:thai translation|translation|คำแปล)\s*:\s*", re.IGNORECASE)


def drop_echo(source: str, thai: str) -> str:
    """For a one-line source, drop extra output lines that have no Thai: the model
    sometimes repeats its rules ("revision number") after the translation."""
    if NEWLINE in source or NEWLINE not in thai:
        return thai
    first, *rest = [ln for ln in thai.split(NEWLINE) if ln.strip()] or [""]
    kept = [ln for ln in rest if THAI_LETTER_RE.search(ln)]
    return NEWLINE.join([first, *kept])


def clean_output(text: str) -> str:
    text = PREFIX_RE.sub("", THINK_RE.sub("", text).strip())
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
        text = text[1:-1].strip()
    return text


class TyphoonEngine:
    """Typhoon Translate 1.5 with the model card's prompt: one user message with the
    instruction, numbered rules and the source text."""

    INSTRUCTION = (
        "Translate the following English text into Thai, strictly following the rules "
        "below and return only the translated text."
    )

    workers = 1  # one GPU

    def __init__(self, client: OllamaClient, model: str) -> None:
        self.client = client
        self.model = model
        self.name = f"typhoon/{model}"

    def prompt(self, text: str, rules: list[str]) -> str:
        lines = [self.INSTRUCTION, "Rules:"]
        lines += [f"{i}.  {rule}" for i, rule in enumerate(rules, 1)]
        lines += ["Source Text:", text]
        return "\n".join(lines)

    def translate(
        self,
        text: str,
        rules: list[str],
        *,
        retry: bool = False,
        stop: Callable[[], bool] | None = None,
    ) -> Result:
        started = time.monotonic()
        response = self.client.chat(
            self.model,
            self.prompt(text, rules),
            temperature=0.5 if retry else 0.2,  # a retry should not repeat the same output
            num_ctx=8192,  # the model card recommends staying under 8192 tokens
            num_predict=min(6144, 512 + 2 * len(text)),
            stop=stop,
        )
        return Result(
            text=clean_output(response["message"]["content"]),
            seconds=time.monotonic() - started,
            tokens=int(response.get("eval_count", 0)),
        )


class GuardedEngine:
    """Runs every request of a local engine under the GPU guard:

    1. waits while the GPU hot spot is at or above the pause limit;
    2. stops the request the moment the hot spot reaches the abort limit, waits
       for it to cool and raises ``GpuTooHot`` (the caller retries in smaller pieces);
    3. rests afterwards in proportion to the work (duty-cycle limit).
    """

    workers = 1  # the guard watches one request at a time

    def __init__(self, inner: Engine, guard: GpuGuard, rest_ratio: float = 1.0) -> None:
        self.inner = inner
        self.guard = guard
        self.rest_ratio = rest_ratio
        self.name = inner.name  # translation-memory keys stay the same

    def translate(
        self,
        text: str,
        rules: list[str],
        *,
        retry: bool = False,
        stop: Callable[[], bool] | None = None,
    ) -> Result:
        self.guard.check()
        started = time.monotonic()
        with self.guard.watch() as watch:

            def halt() -> bool:
                return watch.too_hot() or (stop is not None and stop())

            try:
                result = self.inner.translate(text, rules, retry=retry, stop=halt)
            except RequestStopped:
                if not watch.too_hot():
                    raise
                result = None
        busy = time.monotonic() - started
        if result is None:
            self.guard.stopped += 1
            self.guard.cool_down(
                f"GPU hot spot reached {watch.peak_c}°C (limit {self.guard.abort_at_c}°C): "
                "request stopped"
            )
            raise GpuTooHot(watch.peak_c, self.guard.abort_at_c)
        self.guard.rest(busy, self.rest_ratio)
        return result


ENGINES = ("typhoon", "claude")


def make_engine(
    settings: Settings,
    client: OllamaClient | None = None,
    guard: GpuGuard | None = None,
    *,
    name: str | None = None,
) -> Engine:
    """The configured engine (``name`` overrides the setting).

    Typhoon needs the Ollama ``client``; with a ``guard`` every request runs under it.
    Claude runs remotely, so it needs neither.
    """
    name = name or settings.translate_engine
    if name == "claude":
        from pdftr.translate.claude import ClaudeEngine

        key = settings.anthropic_api_key
        return ClaudeEngine(
            settings.claude_model,
            effort=settings.claude_effort,
            workers=settings.claude_workers,
            api_key=key.get_secret_value() if key else None,
        )
    if name != "typhoon":
        raise ValueError(f"Unknown translation engine {name!r}: use one of {', '.join(ENGINES)}")
    if client is None:
        raise ValueError("The Typhoon engine needs an Ollama client")
    engine: Engine = TyphoonEngine(client, settings.translate_model)
    if guard is not None:
        engine = GuardedEngine(engine, guard, settings.gpu_rest_ratio)
    return engine
