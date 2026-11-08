"""
LLM client factory.

get_client() returns something with `.messages.create(**kwargs)` that takes Anthropic-shaped arguments and
returns Anthropic-shaped responses. Nebius Token Factory is the default provider; it translates to Nebius's
OpenAI-compatible Chat Completions API. Anthropic is available only when explicitly selected with
LLM_PROVIDER=anthropic. Call sites never see the difference. Nothing else should construct a client.
"""
import json
import logging
import re
from types import SimpleNamespace as NS

from . import config

log = logging.getLogger(__name__)

_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_TEMPERATURE = 0.2   # extraction and JSON-only tasks need low temperature


def get_client(provider=None):
    provider = (provider or config.LLM_PROVIDER).lower()
    if provider == "anthropic":
        import anthropic
        return anthropic.Anthropic()   # reads ANTHROPIC_API_KEY
    if provider == "nebius":
        return NebiusClient()
    raise ValueError(f"unknown LLM_PROVIDER {provider!r} (use 'anthropic' or 'nebius')")


# ------------------------------------------------------------------ helpers
def _get(obj, key, default=None):
    """Blocks arrive as SDK objects, dicts, or SimpleNamespace: read any of them."""
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _system_text(system):
    if not system:
        return None
    if isinstance(system, str):
        return system
    return "\n\n".join(_get(b, "text", "") for b in system if _get(b, "text"))   # cache_control dropped


def _tool_content(content):
    if isinstance(content, str):
        return content
    return "".join(_get(b, "text", "") for b in content) if isinstance(content, list) else json.dumps(content)


def _convert_messages(messages):
    out = []
    for m in messages:
        role, content = m["role"], m["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            texts = [_get(b, "text", "") for b in content if _get(b, "type") == "text"]
            calls = [{"id": _get(b, "id"), "type": "function",
                      "function": {"name": _get(b, "name"), "arguments": json.dumps(_get(b, "input") or {})}}
                     for b in content if _get(b, "type") == "tool_use"]
            msg = {"role": "assistant", "content": "".join(texts) or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:   # user turn made of blocks: tool results become one "tool" message each, in order
            texts = []
            for b in content:
                if _get(b, "type") == "tool_result":
                    out.append({"role": "tool", "tool_call_id": _get(b, "tool_use_id"),
                                "content": _tool_content(_get(b, "content", ""))})
                elif _get(b, "type") == "text":
                    texts.append(_get(b, "text", ""))
            if texts:
                out.append({"role": "user", "content": "".join(texts)})
    return out


def _convert_tools(tools):
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t["input_schema"]}} for t in tools]


def _parse_args(raw, name):
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw or "{}")
        return val if isinstance(val, dict) else {}
    except (TypeError, ValueError):
        log.warning("tool call %s returned invalid JSON arguments; using {}", name)
        return {}


def _convert_response(resp):
    choice = resp.choices[0]
    msg = choice.message
    blocks = []
    text = _THINK.sub("", msg.content or "").strip()
    if "<think>" in text:                       # unterminated reasoning block: drop it
        text = text.split("<think>")[0].strip()
    if text:
        blocks.append(NS(type="text", text=text))
    for tc in getattr(msg, "tool_calls", None) or []:
        blocks.append(NS(type="tool_use", id=tc.id, name=tc.function.name,
                         input=_parse_args(tc.function.arguments, tc.function.name)))
    has_tools = any(b.type == "tool_use" for b in blocks)
    reason = choice.finish_reason
    stop = "tool_use" if (reason == "tool_calls" or has_tools) else "max_tokens" if reason == "length" else "end_turn"
    u = getattr(resp, "usage", None)
    details = getattr(u, "prompt_tokens_details", None)
    usage = NS(input_tokens=getattr(u, "prompt_tokens", 0) or 0,
               output_tokens=getattr(u, "completion_tokens", 0) or 0,
               cache_read_input_tokens=(getattr(details, "cached_tokens", 0) or 0) if details else 0,
               cache_creation_input_tokens=0)
    return NS(content=blocks, stop_reason=stop, usage=usage)


# ------------------------------------------------------------------ the adapter
class _Messages:
    def __init__(self, client):
        self._c = client

    def create(self, *, model, messages, max_tokens=1024, system=None, tools=None, tool_choice=None, **_ignored):
        msgs = _convert_messages(messages)
        sys_text = _system_text(system)
        kwargs = dict(model=model, max_tokens=max_tokens, temperature=_TEMPERATURE)
        no_tools = bool(tool_choice) and _get(tool_choice, "type") == "none"
        if tools:
            kwargs["tools"] = _convert_tools(tools)
            kwargs["tool_choice"] = "none" if no_tools else "auto"

        def call(system_text):
            full = ([{"role": "system", "content": system_text}] if system_text else []) + msgs
            return self._c.chat.completions.create(messages=full, **kwargs)

        try:
            resp = call(sys_text)
        except Exception as e:
            if not (no_tools and "tool_choice" in str(e)):
                raise
            # Server rejects tool_choice "none": keep tools defined (history holds tool messages) and say so.
            kwargs.pop("tool_choice")
            resp = call((sys_text + "\n\n" if sys_text else "") + "Answer now without calling tools.")
        return _convert_response(resp)


class NebiusClient:
    """Anthropic-shaped facade over Nebius Token Factory (OpenAI-compatible)."""

    def __init__(self, api_key=None, base_url=None):
        from openai import OpenAI
        key = api_key or config.NEBIUS_API_KEY
        if not key:
            raise RuntimeError("NEBIUS_API_KEY is not set")
        self.messages = _Messages(OpenAI(base_url=base_url or config.NEBIUS_BASE_URL, api_key=key))
