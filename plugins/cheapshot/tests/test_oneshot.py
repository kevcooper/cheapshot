import asyncio
import json
import subprocess
import sys
import time

import pytest

from cheapshot import server
from cheapshot.cache import Cache


@pytest.fixture
def calls(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_cache", Cache(tmp_path / "cache.sqlite3"))
    monkeypatch.setattr(server, "_limiter", None)
    monkeypatch.setattr(server, "POLL_INTERVAL", 0.01)
    monkeypatch.delenv("CHEAPSHOT_MODEL", raising=False)
    monkeypatch.delenv("CHEAPSHOT_MAX_CONCURRENCY", raising=False)
    recorded = []

    async def fake_infer(model, prompt, system, effort, output_schema):
        recorded.append((model, prompt, system, effort, output_schema))
        await asyncio.sleep(0.05)
        if output_schema is not None:
            return json.dumps({"n": len(recorded)}), model
        return f"answer #{len(recorded)}", model

    monkeypatch.setattr(server, "infer", fake_infer)
    return recorded


SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"], "additionalProperties": False}


def run(**kwargs):
    return asyncio.run(server.oneshot(**kwargs))


def test_second_identical_request_is_served_from_cache(calls):
    first = run(prompt="hi")
    second = run(prompt="hi")
    assert first.cached is False and second.cached is True
    assert second.text == first.text == "answer #1"
    assert len(calls) == 1


def test_default_model_and_explicit_default_share_a_key(calls):
    run(prompt="hi")
    assert run(prompt="hi", model=server.DEFAULT_MODEL).cached is True


@pytest.mark.parametrize("change", [{"prompt": "bye"}, {"system": "be terse"}, {"model": "claude-haiku-4-5"}, {"effort": "low"}, {"output_schema": SCHEMA}])
def test_any_input_change_misses(calls, change):
    run(prompt="hi")
    assert run(**({"prompt": "hi"} | change)).cached is False
    assert len(calls) == 2


def test_refresh_bypasses_and_overwrites(calls):
    run(prompt="hi")
    refreshed = run(prompt="hi", refresh=True)
    assert refreshed.cached is False and refreshed.text == "answer #2"
    assert run(prompt="hi").text == "answer #2"


def test_failures_are_not_cached(calls, monkeypatch):
    async def boom(*_):
        raise RuntimeError("refused")

    monkeypatch.setattr(server, "infer", boom)
    with pytest.raises(RuntimeError):
        run(prompt="hi")
    assert server.get_cache().get(server.cache_key(model=server.DEFAULT_MODEL, prompt="hi", system=None, effort=None)) is None


def test_output_schema_is_parsed_into_data_on_miss_and_hit(calls):
    first = run(prompt="hi", output_schema=SCHEMA)
    second = run(prompt="hi", output_schema=SCHEMA)
    assert first.data == second.data == {"n": 1}
    assert second.cached is True and calls[0][4] == SCHEMA


def test_schema_key_order_does_not_matter(calls):
    run(prompt="hi", output_schema=SCHEMA)
    assert run(prompt="hi", output_schema=dict(reversed(SCHEMA.items()))).cached is True


def test_no_schema_keeps_pre_schema_cache_keys(calls):
    assert run(prompt="hi").cache_key == server.cache_key(
        model=server.DEFAULT_MODEL, prompt="hi", system=None, effort=None
    )


def key_for(prompt):
    return server.cache_key(model=server.DEFAULT_MODEL, prompt=prompt, system=None, effort=None)


def other_session(tmp_path):
    """A second connection to the same file, standing in for another session's server."""
    return Cache(tmp_path / "cache.sqlite3")


async def gather(*kwargs):
    return await asyncio.gather(*(server.oneshot(**k) for k in kwargs))


def test_concurrent_identical_requests_run_once(calls):
    first, second = asyncio.run(gather({"prompt": "hi"}, {"prompt": "hi"}))
    assert len(calls) == 1
    assert first.text == second.text == "answer #1"
    assert sorted([first.cached, second.cached]) == [False, True]


def test_waits_for_another_sessions_answer(calls, tmp_path):
    other = other_session(tmp_path)
    token = other.claim(key_for("hi"))

    async def scenario():
        task = asyncio.create_task(server.oneshot(prompt="hi"))
        await asyncio.sleep(0.05)
        assert not task.done()
        other.put(key_for("hi"), {}, "theirs", "m")
        other.release(key_for("hi"), token)
        return await task

    result = asyncio.run(scenario())
    assert result.text == "theirs" and result.cached is True
    assert calls == []


def test_takes_over_when_other_session_fails(calls, tmp_path):
    other = other_session(tmp_path)
    token = other.claim(key_for("hi"))

    async def scenario():
        task = asyncio.create_task(server.oneshot(prompt="hi"))
        await asyncio.sleep(0.05)
        other.release(key_for("hi"), token)  # gave up without storing an answer
        return await task

    result = asyncio.run(scenario())
    assert result.cached is False and len(calls) == 1


def test_claim_from_dead_process_is_ignored(calls, tmp_path):
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    other = other_session(tmp_path)
    other._db.execute("INSERT INTO pending VALUES (?, 'x', ?, ?)", (key_for("hi"), int(dead.stdout), time.time()))
    other._db.commit()
    assert run(prompt="hi").cached is False


def test_concurrency_is_capped(calls, monkeypatch):
    monkeypatch.setenv("CHEAPSHOT_MAX_CONCURRENCY", "2")
    running = peak = 0

    async def tracked(*args):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return "ok", "m"

    monkeypatch.setattr(server, "infer", tracked)
    asyncio.run(gather(*({"prompt": str(i)} for i in range(6))))
    assert peak == 2
