"""Claude (Anthropic API) as the translation engine: same contract as Typhoon, no GPU.

One request per piece of text: a fixed system prompt, then one user message with the
numbered rules and the text. Requests run in parallel (``workers``); the SDK retries
rate limits and server errors itself. On models that support it, a refused request is
re-run server-side on a fallback model; a text that is still declined comes back
unchanged and flagged ``refused`` (it is not stored in the translation memory).

Credentials: ``ANTHROPIC_API_KEY`` (in .env or the environment), or an ``ant auth login``
profile, which the SDK finds by itself.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable

from pdftr.translate.engine import Result, clean_output

# List prices in US$ per million tokens (input, output), November 2026.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
# Models that accept the server-side refusal fallback (Haiku has none).
FALLBACK_MODELS = {
    "claude-fable-5-2",
    "claude-fable-5-1",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5-5",
}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
SYSTEM = (
    "You translate English IT textbook text into Thai for a bilingual study edition. "
    "Follow every numbered rule in the user's message. Keep the meaning, the order and the "
    "line breaks of the text. Return only the Thai translation of the text inside <text>: "
    "no notes, no quotation marks, no tags, and not the English."
)
TAG_RE = re.compile(r"</?text>")
RETRY_RULE = (
    "Copy every placeholder such as ⟦1⟧ exactly once and unchanged; "
    "the previous answer lost some of them."
)


class ClaudeEngine:
    def __init__(
        self,
        model: str = "claude-opus-5-5",
        *,
        effort: str = "low",
        workers: int = 4,
        api_key: str | None = None,
        max_retries: int = 6,
        client=None,
    ) -> None:
        if client is None:
            import anthropic

            options = {"max_retries": max_retries}
            if api_key:
                options["api_key"] = api_key
            client = anthropic.Anthropic(**options)
        self.client = client
        self.model = model
        self.name = f"claude/{model}"
        self.effort = effort
        self.workers = max(1, workers)
        self._lock = threading.Lock()
        self.usage = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "refused": 0}

    def prompt(self, text: str, rules: list[str], retry: bool = False) -> str:
        rules = [*rules, RETRY_RULE] if retry else list(rules)
        lines = ["Rules:", *(f"{i}. {rule}" for i, rule in enumerate(rules, 1))]
        return "\n".join(lines) + f"\n\n<text>\n{text}\n</text>"

    def translate(
        self,
        text: str,
        rules: list[str],
        *,
        retry: bool = False,
        stop: Callable[[], bool] | None = None,  # nothing to stop: no local GPU work
    ) -> Result:
        started = time.monotonic()
        extra = {}
        if self.model in FALLBACK_MODELS:
            extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": self.prompt(text, rules, retry)}],
            output_config={"effort": self.effort},
            **extra,
        )
        usage = response.usage
        refused = response.stop_reason == "refusal"
        with self._lock:
            self.usage["requests"] += 1
            self.usage["input_tokens"] += usage.input_tokens or 0
            self.usage["output_tokens"] += usage.output_tokens or 0
            self.usage["refused"] += refused
        seconds = time.monotonic() - started
        if refused:  # declined even after the fallback: keep the English, flag it for R4
            return Result(text, seconds, usage.output_tokens or 0, ("refused",), usage.input_tokens)
        thai = "".join(block.text for block in response.content if block.type == "text")
        return Result(
            clean_output(TAG_RE.sub("", thai)),
            seconds,
            usage.output_tokens or 0,
            input_tokens=usage.input_tokens or 0,
        )

    def cost(self) -> float | None:
        """Estimated spend so far at list prices (None for a model not in the table)."""
        price = PRICES.get(self.model)
        if price is None:
            return None
        return (
            self.usage["input_tokens"] * price[0] + self.usage["output_tokens"] * price[1]
        ) / 1e6

    def summary(self) -> str:
        u = self.usage
        cost = self.cost()
        return (
            f"Claude ({self.model}): {u['requests']} requests, {u['input_tokens']:,} input + "
            f"{u['output_tokens']:,} output tokens"
            + (f", about ${cost:.2f} at list prices" if cost is not None else "")
            + (f"; {u['refused']} declined" if u["refused"] else "")
        )
