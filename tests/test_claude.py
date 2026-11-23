"""The Claude engine (with a fake API client: no network, no cost) and parallel runs."""

import threading
from types import SimpleNamespace

import pytest
from conftest import FakeEngine, chunk_by_text

from pdftr.translate import memory
from pdftr.translate.claude import FALLBACK_BETA, ClaudeEngine
from pdftr.translate.engine import Result, make_engine
from pdftr.translate.rules import load_glossary
from pdftr.translate.runner import PROMPT_VERSION, Glossary, translate_document, translate_text

QUIET = {"log": lambda _m: None}


class FakeMessages:
    def __init__(self, answer="คำแปล", stop_reason="end_turn"):
        self.answer, self.stop_reason, self.calls = answer, stop_reason, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = [
            SimpleNamespace(type="thinking", thinking=""),  # display "omitted": empty
            SimpleNamespace(type="text", text=f"<text>\n{self.answer}\n</text>"),
        ]
        usage = SimpleNamespace(input_tokens=120, output_tokens=30)
        return SimpleNamespace(content=content, stop_reason=self.stop_reason, usage=usage)


def fake_client(**kw):
    messages = FakeMessages(**kw)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def test_request_shape_and_answer():
    client, messages = fake_client(answer="เชลล์อ่านคำสั่ง")
    engine = ClaudeEngine("claude-opus-5-5", client=client)
    result = engine.translate("The shell reads commands.", ["Use formal Thai.", "Keep it."])
    assert result.text == "เชลล์อ่านคำสั่ง" and result.flags == ()
    call = messages.calls[0]
    prompt = call["messages"][0]["content"]
    assert "1. Use formal Thai.\n2. Keep it." in prompt
    assert prompt.endswith("<text>\nThe shell reads commands.\n</text>")
    assert call["model"] == "claude-opus-5-5" and call["output_config"] == {"effort": "low"}
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert "temperature" not in call  # rejected by current models
    assert engine.name == "claude/claude-opus-5-5"


def test_haiku_has_no_server_side_fallback():
    client, messages = fake_client()
    ClaudeEngine("claude-haiku-5-5", client=client).translate("Hello there.", [])
    assert "fallbacks" not in messages.calls[0] and "betas" not in messages.calls[0]


def test_a_refusal_keeps_the_english_and_is_not_remembered(conn, sample_doc):
    client, messages = fake_client(stop_reason="refusal")
    engine = ClaudeEngine("claude-opus-5-5", client=client)
    glossary = Glossary(load_glossary(conn, sample_doc))
    thai, key, outcome = translate_text(conn, engine, glossary, "A port scanner probes hosts.")
    assert "refused" in outcome.flags and thai == "A port scanner probes hosts."
    assert memory.mt_get(conn, key) is None  # the next run asks again
    assert len(messages.calls) == 1 and engine.usage["refused"] == 1


def test_usage_and_cost():
    client, _messages = fake_client()
    engine = ClaudeEngine("claude-opus-5-5", client=client)
    for _ in range(10):
        engine.translate("Some text to translate.", [])
    assert engine.usage["input_tokens"] == 1200 and engine.usage["output_tokens"] == 300
    assert engine.cost() == pytest.approx((1200 * 4 + 300 * 20) / 1e6)
    assert "10 requests" in engine.summary()


def test_make_engine_builds_claude_from_settings():
    from pdftr.settings import Settings

    s = Settings(translate_engine="claude", claude_model="claude-sonnet-5-5", claude_workers=6,
                 ANTHROPIC_API_KEY="sk-test-not-real")  # fmt: skip
    engine = make_engine(s)
    assert isinstance(engine, ClaudeEngine) and engine.workers == 6
    assert engine.name == "claude/claude-sonnet-5-5"


class ThreadSafeFake(FakeEngine):
    """The fake translator, safe for several threads, running two requests at a time."""

    workers = 2

    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.threads = set()

    def translate(self, text, rules, *, retry=False, stop=None):
        with self.lock:
            self.threads.add(threading.get_ident())
        result = super().translate(text, rules, retry=retry)
        return Result(result.text, result.seconds, result.tokens)


def test_parallel_run_matches_a_sequential_one(conn, sample_doc, test_db_url):
    parallel = ThreadSafeFake()
    summary = translate_document(conn, sample_doc, parallel, database_url=test_db_url, **QUIET)
    conn.commit()
    first = {
        r["id"]: r["text_th"]
        for r in conn.execute("SELECT id, text_th FROM chunks WHERE doc_id=%s", (sample_doc,))
    }
    assert summary["mt"] > 1 and all(first.values())
    assert parallel.threads and threading.get_ident() not in parallel.threads  # in workers
    again = translate_document(conn, sample_doc, FakeEngine(), force=True, **QUIET)
    conn.commit()
    second = {
        r["id"]: r["text_th"]
        for r in conn.execute("SELECT id, text_th FROM chunks WHERE doc_id=%s", (sample_doc,))
    }
    assert again["mt"] == summary["mt"] and first == second


def test_switching_engines_keeps_thai_approved_in_r4(conn, sample_doc, fake_engine):
    translate_document(conn, sample_doc, fake_engine, **QUIET)
    db = chunk_by_text(conn, sample_doc, "A database")
    conn.execute("UPDATE chunks SET review_th='approved' WHERE id=%s", (db["id"],))
    conn.commit()
    other = FakeEngine()
    other.name = "other-engine"  # different translation-memory keys, like a new engine
    summary = translate_document(conn, sample_doc, other, **QUIET)
    assert summary["approved_kept"] == 1
    kept = chunk_by_text(conn, sample_doc, "A database")
    assert kept["text_th"] == db["text_th"] and kept["review_th"] == "approved"
    assert memory.key_for("other-engine", PROMPT_VERSION, "x") != memory.key_for(
        "fake", PROMPT_VERSION, "x"
    )
