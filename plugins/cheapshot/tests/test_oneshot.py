import asyncio
import json

import pytest

from cheapshot import server
from cheapshot.cache import Cache


@pytest.fixture
def calls(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_cache", Cache(tmp_path / "cache.sqlite3"))
    monkeypatch.delenv("CHEAPSHOT_MODEL", raising=False)
    recorded = []

    async def fake_infer(model, prompt, system, effort, output_schema):
        recorded.append((model, prompt, system, effort, output_schema))
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
