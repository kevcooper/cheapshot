import asyncio
import json
import subprocess
import sys
import time

import pytest

from cheapshot import server
from cheapshot import cache as cache_module
from cheapshot.cache import Cache


@pytest.fixture
def calls(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_cache", Cache(tmp_path / "cache.sqlite3"))
    monkeypatch.setattr(server, "_limiter", None)
    monkeypatch.setattr(server, "POLL_INTERVAL", 0.01)
    monkeypatch.delenv("CHEAPSHOT_MODEL", raising=False)
    monkeypatch.delenv("CHEAPSHOT_MAX_CONCURRENCY", raising=False)
    recorded = []

    async def fake_infer(model, prompt, system, effort, output_schema, attachments):
        recorded.append((model, prompt, system, effort, output_schema, attachments))
        await asyncio.sleep(0.05)
        if output_schema is not None:
            return json.dumps({"n": len(recorded)}), model, fake_response(len(recorded))
        return f"answer #{len(recorded)}", model, fake_response(len(recorded))

    monkeypatch.setattr(server, "infer", fake_infer)
    return recorded


def fake_response(n):
    return {"result": f"answer #{n}", "usage": {"output_tokens": 10 * n}, "total_cost_usd": 0.01}


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
    other._db.execute("INSERT INTO pending (key, token, pid, started_at) VALUES (?, 'x', ?, ?)", (key_for("hi"), int(dead.stdout), time.time()))
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
        return "ok", "m", {}

    monkeypatch.setattr(server, "infer", tracked)
    asyncio.run(gather(*({"prompt": str(i)} for i in range(6))))
    assert peak == 2


def test_files_are_inlined_and_keyed_by_content(calls, tmp_path):
    doc = tmp_path / "doc.txt"
    doc.write_text("version one")
    first = run(prompt="summarize", files=[str(doc)])
    assert calls[0][1] == "summarize"
    assert calls[0][5] == [f'<file path="{doc}">\nversion one\n</file>']
    assert run(prompt="summarize", files=[str(doc)]).cached is True

    doc.write_text("version two")
    edited = run(prompt="summarize", files=[str(doc)])
    assert edited.cached is False and edited.cache_key != first.cache_key


def test_stored_request_has_hashes_not_contents(calls, tmp_path):
    doc = tmp_path / "doc.txt"
    doc.write_text("secret contents")
    run(prompt="summarize", files=[str(doc)])
    (stored,) = server.get_cache()._db.execute("SELECT request FROM results").fetchone()
    assert "secret contents" not in stored and json.loads(stored)["files"][0]["path"] == str(doc)


@pytest.mark.parametrize(
    "setup, message",
    [
        (lambda d: "relative/path.txt", "must be absolute"),
        (lambda d: str(d / "missing.txt"), "Not a file"),
        (lambda d: (d / "bin").write_bytes(b"\xff\xfe") and str(d / "bin"), "Not a UTF-8"),
    ],
)
def test_bad_files_raise_readable_errors(calls, tmp_path, setup, message):
    with pytest.raises(server.ToolError, match=message):
        run(prompt="summarize", files=[setup(tmp_path)])
    assert calls == []


def test_file_size_cap(calls, tmp_path, monkeypatch):
    monkeypatch.setenv("CHEAPSHOT_MAX_FILE_BYTES", "10")
    doc = tmp_path / "doc.txt"
    doc.write_text("x" * 11)
    with pytest.raises(server.ToolError, match="exceed 10 bytes"):
        run(prompt="summarize", files=[str(doc)])


def row(key, *columns):
    return server.get_cache()._db.execute(
        f"SELECT {', '.join(columns)} FROM results WHERE key = ?", (key,)
    ).fetchone()


def test_hits_are_counted_including_waiters(calls):
    first = run(prompt="hi")
    assert row(first.cache_key, "hits", "last_hit_at") == (0, None)
    run(prompt="hi")
    asyncio.run(gather({"prompt": "hi"}, {"prompt": "hi"}))
    assert row(first.cache_key, "hits")[0] == 3


def test_refresh_keeps_hit_count(calls):
    first = run(prompt="hi")
    run(prompt="hi")
    run(prompt="hi", refresh=True)
    assert row(first.cache_key, "hits")[0] == 1


def test_raw_response_is_stored_without_the_answer(calls):
    first = run(prompt="hi")
    stored = json.loads(row(first.cache_key, "response")[0])
    assert stored == {"usage": {"output_tokens": 10}, "total_cost_usd": 0.01}


def test_waiters_get_the_claimants_error(calls, tmp_path):
    other = other_session(tmp_path)
    token = other.claim(key_for("hi"))

    async def scenario():
        task = asyncio.create_task(server.oneshot(prompt="hi"))
        await asyncio.sleep(0.05)
        other.release(key_for("hi"), token, error="Request was refused")
        return await task

    with pytest.raises(server.ToolError, match="Request was refused .from a concurrent"):
        asyncio.run(scenario())
    assert calls == []


def test_concurrent_identical_failures_run_once(calls, monkeypatch):
    attempts = []

    async def refuse(*args):
        attempts.append(1)
        await asyncio.sleep(0.05)
        raise server.ToolError("Request was refused")

    monkeypatch.setattr(server, "infer", refuse)

    async def scenario():
        return await asyncio.gather(
            server.oneshot(prompt="hi"), server.oneshot(prompt="hi"), return_exceptions=True
        )

    results = asyncio.run(scenario())
    assert len(attempts) == 1
    assert all(isinstance(r, server.ToolError) for r in results)


def test_a_new_call_retries_after_an_old_failure(calls, tmp_path):
    other = other_session(tmp_path)
    other.release(key_for("hi"), other.claim(key_for("hi")), error="Request was refused")
    assert run(prompt="hi").cached is False and len(calls) == 1


def test_databases_from_older_versions_are_upgraded(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE results (key TEXT PRIMARY KEY, request TEXT NOT NULL, text TEXT NOT NULL,"
        " model TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    db.execute("INSERT INTO results VALUES ('k', '{}', 'old answer', 'm', 1.0)")
    db.commit()
    db.close()

    cache = Cache(path)
    assert cache.get("k").text == "old answer"
    cache.record_hit("k")
    assert cache._db.execute("SELECT hits, response FROM results").fetchone() == (1, None)


def test_new_databases_record_the_schema_version(tmp_path):
    cache = Cache(tmp_path / "db.sqlite3")
    assert cache._version() == cache_module.SCHEMA_VERSION


def test_opening_a_newer_database_fails_clearly(tmp_path):
    Cache(tmp_path / "db.sqlite3")._db.execute("PRAGMA user_version = 99")
    with pytest.raises(cache_module.SchemaTooNew, match="restart this session"):
        Cache(tmp_path / "db.sqlite3")


def test_upgrade_by_another_session_fails_writes_clearly(calls, tmp_path):
    run(prompt="hi")
    other_session(tmp_path)._db.execute("PRAGMA user_version = 99")
    with pytest.raises(server.ToolError, match="upgraded by a newer version"):
        run(prompt="something new")


def mode(path):
    return path.stat().st_mode & 0o777


def test_new_cache_is_private(tmp_path):
    path = tmp_path / "data" / "cache.sqlite3"
    cache = Cache(path)
    cache.put("k", {}, "answer", "m")  # makes SQLite create the -wal and -shm files
    assert mode(path.parent) == 0o700
    for name in ("cache.sqlite3", "cache.sqlite3-wal", "cache.sqlite3-shm"):
        assert mode(path.parent / name) == 0o600, name


def test_existing_loose_permissions_are_tightened(tmp_path):
    path = tmp_path / "data" / "cache.sqlite3"
    Cache(path).put("k", {}, "answer", "m")
    path.parent.chmod(0o755)
    for file in path.parent.iterdir():
        file.chmod(0o644)
    Cache(path)
    assert mode(path.parent) == 0o700
    assert {mode(f) for f in path.parent.iterdir()} == {0o600}
