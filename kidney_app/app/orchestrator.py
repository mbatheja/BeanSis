"""
One chat turn, for any tab:

  safety gate -> load profile + thread memory -> tab agent (tool loop, parallel calls, capped rounds)
  -> citation grounding check -> persist messages + every source found -> maybe summarize old turns
"""
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from . import agents, config, grounding, presentation, safety, tools

_pool = ThreadPoolExecutor(max_workers=8)
TOOL_RESULT_CHARS = 8000   # cap per tool result to bound context size
log = logging.getLogger(__name__)


def _history(store, thread):
    """Rolling summary + last N messages, trimmed so it starts with a user turn."""
    msgs = store.get_messages(thread["thread_id"], start=thread.get("summarized_upto") or 0)
    msgs = msgs[-config.HISTORY_TURNS:]
    while msgs and msgs[0]["role"] != "user":
        msgs = msgs[1:]
    return [{"role": m["role"], "content": m["content"]} for m in msgs]


def _system_blocks(tab, profile, summary):
    static_parts, dynamic = agents.system_prompt(tab, profile, summary)
    # Static rules + tab prompt are identical across requests -> prompt-cache them (tools are cached too,
    # since they precede the system prompt). Profile/summary change per user, so they go after the breakpoint.
    blocks = [{"type": "text", "text": "\n\n".join(static_parts), "cache_control": {"type": "ephemeral"}}]
    if dynamic:
        blocks.append({"type": "text", "text": dynamic})
    return blocks


def run_agent(llm, tab, messages, system, ctx, max_rounds=None):
    """Tool-use loop. Returns (final_text, sources, tool_calls, messages_so_far).
    messages_so_far includes tool calls/results, so a revision can continue with the evidence in context."""
    max_rounds = config.MAX_TOOL_ROUNDS if max_rounds is None else max_rounds
    tool_schemas = tools.schemas(agents.TABS[tab]["tools"])
    sources, calls = {}, []
    usage = ctx.setdefault("usage", {})
    for round_no in range(max_rounds + 1):
        last_round = round_no == max_rounds
        kwargs = dict(model=config.MODEL_MAIN, max_tokens=config.MAX_TOKENS, system=system,
                      messages=messages)
        if last_round:
            # Some OpenAI-compatible models ignore tool_choice='none'. Removing tools guarantees that
            # the final round writes an answer from evidence already gathered instead of spending the
            # last round on another tool call and returning an empty response.
            instruction = {"type": "text", "text": "The tool-call limit has been reached. Answer now using only the tool results already in the conversation; do not call tools."}
            if isinstance(system, list):
                kwargs["system"] = [*system, instruction]
            else:
                kwargs["system"] = f"{system or ''}\n\n{instruction['text']}"
        else:
            kwargs["tools"] = tool_schemas
        resp = llm.messages.create(**kwargs)
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            usage[k] = usage.get(k, 0) + (getattr(getattr(resp, "usage", None), k, 0) or 0)
        usage["llm_calls"] = usage.get("llm_calls", 0) + 1
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        text = "".join(b.text for b in resp.content if b.type == "text")
        if not text.strip() and not tool_uses:
            log.warning("Model returned no answer text for tab=%s (stop_reason=%s); retrying once", tab, resp.stop_reason)
            retry_instruction = ("The previous output contained no user-facing answer. Reply now in concise plain language "
                                 "without showing internal reasoning. Use available tools only if needed; if retrieved "
                                 "evidence is insufficient, say so plainly.")
            retry_system = ([*kwargs["system"], {"type": "text", "text": retry_instruction}]
                            if isinstance(kwargs["system"], list)
                            else f"{kwargs.get('system') or ''}\n\n{retry_instruction}")
            retry_kwargs = {**kwargs, "system": retry_system, "max_tokens": max(4096, config.MAX_TOKENS * 3)}
            resp = llm.messages.create(**retry_kwargs)
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                usage[k] = usage.get(k, 0) + (getattr(getattr(resp, "usage", None), k, 0) or 0)
            usage["llm_calls"] = usage.get("llm_calls", 0) + 1
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not tool_uses:
            text = "".join(b.text for b in resp.content if b.type == "text")
            if not text.strip():
                log.warning("Model returned no answer text for tab=%s (stop_reason=%s)", tab, resp.stop_reason)
                text = "I couldn't generate an answer this time. Please try again."
            return text, list(sources.values()), calls, messages

        # Independent tool calls in the same round run in parallel.
        futures = {b.id: _pool.submit(tools.run, b.name, b.input, ctx) for b in tool_uses}
        results = []
        for b in tool_uses:
            out = futures[b.id].result()
            call = {"tool": b.name, "input": b.input}
            if ctx.get("debug"):   # eval mode: keep what the model saw, so a judge can check citations
                call["output"] = json.dumps(out.get("results"), default=str)[:TOOL_RESULT_CHARS]
            calls.append(call)
            for s in out.get("sources", []):
                sources.setdefault(s["source_id"], s)
            results.append({"type": "tool_result", "tool_use_id": b.id,
                            "content": json.dumps(out.get("results"), default=str)[:TOOL_RESULT_CHARS]})
        messages = messages + [{"role": "assistant", "content": resp.content},
                               {"role": "user", "content": results}]
    log.warning("Tool loop ended without an answer for tab=%s after %s rounds", tab, max_rounds)
    return "I couldn't generate an answer this time. Please try again.", list(sources.values()), calls, messages


def revision_feedback(tab, report):
    """Evaluator: deterministic citation check. Returns feedback for the agent, or None if the answer passes."""
    if report["removed"]:
        return ("Your answer cited these IDs, but none of them appear in the tool results from this conversation: "
                f"{', '.join(report['removed'])}. Rewrite the answer using only sources the tools returned, citing "
                "their exact IDs. You may call a tool again if you need evidence. If you can't find support for a "
                "claim, remove the claim or say plainly that you couldn't find a source.")
    if tab in grounding.CLINICAL_TABS and not report["cited"]:
        return ("Your answer makes health statements without any citations. Use the tools to find sources and cite "
                "them with their exact IDs, or say plainly that you couldn't find a source for this question.")
    return None


def _better(new, old):
    """Accept the revision only if it fixes what the evaluator flagged without making anything worse."""
    if len(new["removed"]) > len(old["removed"]):
        return False
    return len(new["removed"]) < len(old["removed"]) or (len(new["cited"]) > 0 and len(old["cited"]) == 0)


def generate_with_revision(llm, tab, messages, system, ctx):
    """Evaluator-optimizer: generate -> deterministic citation check -> at most one revision with feedback."""
    text, sources, calls, convo = run_agent(llm, tab, messages, system, ctx)
    answer, report = grounding.check(text, sources, tab)
    feedback = revision_feedback(tab, report) if config.MAX_REVISIONS > 0 else None
    revision = {"attempted": False, "accepted": False, "reason": None}
    if feedback:
        revision.update(attempted=True, reason="unverified_citations" if report["removed"] else "no_citations")
        convo = convo + [{"role": "assistant", "content": text}, {"role": "user", "content": feedback}]
        text2, sources2, calls2, _ = run_agent(llm, tab, convo, system, ctx, max_rounds=config.REVISION_TOOL_ROUNDS)
        merged = {s["source_id"]: s for s in sources + sources2}
        answer2, report2 = grounding.check(text2, list(merged.values()), tab)
        calls = calls + [{**c, "revision": True} for c in calls2]
        if text2.strip() and _better(report2, report):
            answer, report, sources = answer2, report2, list(merged.values())
            revision["accepted"] = True
    return answer, report, sources, calls, revision


def maybe_summarize(store, llm, thread):
    """Fold messages older than the recent window into the thread's rolling summary (fast model)."""
    tid = thread["thread_id"]
    upto = thread.get("summarized_upto") or 0
    msgs = store.get_messages(tid, start=upto)
    if len(msgs) <= config.SUMMARIZE_AFTER:
        return
    old = msgs[: len(msgs) - config.HISTORY_TURNS]
    transcript = "\n".join(f"{m['role']}: {m['content'][:800]}" for m in old)
    prompt = (f"Existing summary: {thread.get('summary') or '(none)'}\n\nNew messages:\n{transcript}\n\n"
              "Update the summary in under 120 words: what the user asked about, key facts they shared "
              "about themselves, and what was found (keep source IDs). No advice.")
    resp = llm.messages.create(model=config.MODEL_FAST, max_tokens=250,
                               messages=[{"role": "user", "content": prompt}])
    summary = "".join(b.text for b in resp.content if b.type == "text")
    store.set_summary(tid, summary, upto + len(old))


def handle_chat(store, llm, user_id, tab, message, thread_id=None, debug=False):
    if tab not in agents.TABS:
        raise ValueError(f"unknown tab {tab}")
    t0 = time.time()
    thread = store.get_thread(thread_id) if thread_id else None
    if thread is None or thread["user_id"] != user_id or thread["tab"] != tab:
        thread_id = store.create_thread(user_id, tab, title=message[:60])
        thread = store.get_thread(thread_id)

    urgent = safety.check(message, llm)
    if urgent:
        store.add_message(thread_id, "user", message)
        mid = store.add_message(thread_id, "assistant", urgent, meta=presentation.urgent(urgent))
        return {"thread_id": thread_id, "message_id": mid, "answer": urgent, "urgent": True, "sources": [],
                "related_history": [],
                "retrieved": [], "tool_calls": [], "usage": {}, **presentation.urgent(urgent),
                "latency_s": round(time.time() - t0, 2)}

    profile = store.get_profile(user_id)
    messages = _history(store, thread) + [{"role": "user", "content": message}]
    ctx = {"user_id": user_id, "profile": profile, "store": store, "llm": llm, "tab": tab, "debug": debug}
    answer, report, sources, calls, revision = generate_with_revision(
        llm, tab, messages, _system_blocks(tab, profile, thread.get("summary")), ctx)
    provider_search = ctx.get("provider_search")
    if tab == "doctors" and provider_search and not provider_search.get("provider_count"):
        plan = provider_search.get("plan") or {}
        verify_url = provider_search.get("verify_url")
        if verify_url:
            specialty = provider_search.get("specialty", "provider").replace("_", " ")
            plan_name = plan.get("plan_name") or plan.get("insurer") or "your insurance plan"
            if provider_search.get("directory_loaded") is False:
                reason = "I couldn't verify local in-network listings because this plan's directory hasn't been loaded into Bean Sis yet."
            else:
                reason = f"I couldn't find a matching {specialty} in the local directory near ZIP code {provider_search.get('zip')}."
            answer = (f"{reason} Search {plan_name}'s current provider directory here: "
                      f"[PLAN:{plan.get('plan_id', '')}]. Please confirm the provider is in network and accepting "
                      "patients before booking.")
            report = grounding.check(answer, sources, tab)[1]
            revision = {"attempted": False, "accepted": False, "reason": None}
    shown = presentation.build(tab, answer, report, sources, calls, grounding.CLINICAL_TABS)
    shown["guardrails"].append(presentation.revision_guardrail(revision))

    # "You looked into this before": computed before saving so this question doesn't match itself
    related = store.similar_past(user_id, message, exclude_thread=thread_id)
    store.add_message(thread_id, "user", message)
    mid = store.add_message(thread_id, "assistant", answer, meta=shown)
    store.add_sources(mid, user_id, tab, sources)      # everything retrieved -> recallable later
    try:
        maybe_summarize(store, llm, store.get_thread(thread_id))
    except Exception:
        pass  # summarization is an optimization; never fail the turn over it
    return {"thread_id": thread_id, "message_id": mid, "answer": answer, "urgent": False,
            "related_history": related,
            "sources": report["used_sources"], "retrieved": sources,
            "removed_citations": report["removed"], "tool_calls": calls, "usage": ctx.get("usage", {}),
            "revision": revision,
            **shown,
            "latency_s": round(time.time() - t0, 2)}
