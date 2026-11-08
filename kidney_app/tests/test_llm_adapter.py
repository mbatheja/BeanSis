"""Offline tests for app/llm.py: Anthropic-shaped requests -> OpenAI shape, and back."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import llm  # noqa: E402


class FakeOpenAI:
    def __init__(self, responses, reject_none=False):
        self.responses, self.calls, self.reject_none = list(responses), [], reject_none
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        if self.reject_none and kw.get("tool_choice") == "none":
            raise ValueError("unsupported value for tool_choice")
        return self.responses.pop(0)


def reply(content="hi", tool_calls=None, finish="stop", usage=None):
    msg = NS(content=content, tool_calls=tool_calls)
    return NS(choices=[NS(message=msg, finish_reason=finish)],
              usage=usage or NS(prompt_tokens=10, completion_tokens=5, prompt_tokens_details=None))


def make(responses, **kw):
    fake = FakeOpenAI(responses, **kw)
    return llm._Messages(fake), fake


def test_system_blocks_joined_and_cache_control_dropped():
    m, fake = make([reply()])
    m.create(model="m", max_tokens=5, messages=[{"role": "user", "content": "q"}],
             system=[{"type": "text", "text": "A", "cache_control": {"type": "ephemeral"}},
                     {"type": "text", "text": "B"}])
    sent = fake.calls[0]["messages"]
    assert sent[0] == {"role": "system", "content": "A\n\nB"} and sent[1]["content"] == "q"


def test_tools_mapped_and_tool_choice():
    tools = [{"name": "t", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    m, fake = make([reply(), reply()])
    m.create(model="m", messages=[{"role": "user", "content": "q"}], tools=tools)
    m.create(model="m", messages=[{"role": "user", "content": "q"}], tools=tools, tool_choice={"type": "none"})
    assert fake.calls[0]["tools"] == [{"type": "function", "function": {
        "name": "t", "description": "d", "parameters": {"type": "object", "properties": {}}}}]
    assert fake.calls[0]["tool_choice"] == "auto" and fake.calls[1]["tool_choice"] == "none"


def test_tool_choice_none_fallback_when_rejected():
    tools = [{"name": "t", "description": "d", "input_schema": {}}]
    m, fake = make([reply("done")], reject_none=True)
    r = m.create(model="m", messages=[{"role": "user", "content": "q"}], tools=tools,
                 system="SYS", tool_choice={"type": "none"})
    retry = fake.calls[-1]
    assert "tool_choice" not in retry and "tools" in retry
    assert "Answer now without calling tools." in retry["messages"][0]["content"]
    assert r.content[0].text == "done"


def test_assistant_tool_use_blocks_objects_and_dicts_and_two_results():
    obj_block = NS(type="tool_use", id="c1", name="a", input={"x": 1})
    dict_block = {"type": "tool_use", "id": "c2", "name": "b", "input": {"y": 2}}
    msgs = [{"role": "user", "content": "q"},
            {"role": "assistant", "content": [NS(type="text", text="hm"), obj_block, dict_block]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "R1"},
                                         {"type": "tool_result", "tool_use_id": "c2", "content": "R2"}]},
            {"role": "assistant", "content": "plain"}]
    out = llm._convert_messages(msgs)
    a = out[1]
    assert a["content"] == "hm" and [c["id"] for c in a["tool_calls"]] == ["c1", "c2"]
    assert json.loads(a["tool_calls"][1]["function"]["arguments"]) == {"y": 2}
    assert out[2] == {"role": "tool", "tool_call_id": "c1", "content": "R1"}
    assert out[3] == {"role": "tool", "tool_call_id": "c2", "content": "R2"}
    assert out[4] == {"role": "assistant", "content": "plain"}


def test_assistant_with_only_tool_calls_has_null_content():
    out = llm._convert_messages([{"role": "assistant", "content": [NS(type="tool_use", id="1", name="a", input={})]}])
    assert out[0]["content"] is None


def test_response_tool_calls_become_tool_use():
    tc = NS(id="c1", function=NS(name="search", arguments='{"q": "ckd"}'))
    r = llm._convert_response(reply(content="", tool_calls=[tc], finish="tool_calls"))
    assert r.stop_reason == "tool_use"
    assert r.content[0].type == "tool_use" and r.content[0].input == {"q": "ckd"} and r.content[0].id == "c1"


def test_tool_calls_with_finish_stop_still_tool_use():
    tc = NS(id="c1", function=NS(name="s", arguments="{}"))
    assert llm._convert_response(reply(content="x", tool_calls=[tc], finish="stop")).stop_reason == "tool_use"


def test_malformed_arguments_become_empty_dict():
    tc = NS(id="c1", function=NS(name="s", arguments="{not json"))
    r = llm._convert_response(reply(content=None, tool_calls=[tc], finish="tool_calls"))
    assert r.content[0].input == {}


def test_finish_reasons():
    assert llm._convert_response(reply(finish="length")).stop_reason == "max_tokens"
    assert llm._convert_response(reply(finish="stop")).stop_reason == "end_turn"


def test_think_stripped():
    r = llm._convert_response(reply(content='<think>plan\nmore</think>\n{"urgent": false}'))
    assert r.content[0].text == '{"urgent": false}'
    r2 = llm._convert_response(reply(content="answer<think>cut off"))
    assert r2.content[0].text == "answer"


def test_usage_mapped():
    u = NS(prompt_tokens=100, completion_tokens=20, prompt_tokens_details=NS(cached_tokens=64))
    r = llm._convert_response(reply(usage=u))
    assert (r.usage.input_tokens, r.usage.output_tokens) == (100, 20)
    assert r.usage.cache_read_input_tokens == 64 and r.usage.cache_creation_input_tokens == 0


def test_get_client_unknown_provider():
    try:
        llm.get_client("nope")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
