"""
One chat turn, for any tab:

  safety gate -> load profile + thread memory -> tab agent (tool loop, parallel calls, capped rounds)
  -> citation grounding check -> persist messages + every source found -> maybe summarize old turns
"""
import json
import logging
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import agents, config, grounding, presentation, safety, tools

_pool = ThreadPoolExecutor(max_workers=8)
TOOL_RESULT_CHARS = 8000   # cap per tool result to bound context size
log = logging.getLogger(__name__)
_a2a_cards = None
_a2a_cards_lock = threading.Lock()
_summary_lock = threading.Lock()
_summaries_inflight = set()


def _record_timing(ctx, stage, elapsed, **details):
    event = {"stage": stage, "seconds": round(max(0.0, elapsed), 3), **details}
    ctx.setdefault("timings", []).append(event)
    return event

# Fixed, deliberately conservative query routing. Existing tabs remain explicit user intent;
# `auto` is for a general-chat entry point and routes into those same specialist threads.
ROUTE_RULES = {
    "medications": ("medication", "medicine", "drug", "side effect", "interaction", "dose", "prescription", "what is .* used for"),
    "doctors": ("find .* doctor", "find .* nephrolog", "find .* ob.?gyn", "provider", "in network", "specialist near", "appointment today"),
    "resources": ("coverage", "insurance", "ride", "transport", "food", "dietitian", "financial aid", "community resource", "clinical trial", "support group"),
    "symptoms": ("symptom", "pain", "swelling", "fatigue", "itch", "bone health", "dialysis", "transplant", "ckd", "kidney disease", "pregnan", "blood pressure"),
}


def route_query(message):
    """Return a fixed route or clarify; ambiguous cross-domain requests are never guessed."""
    import re
    text = message.lower()
    matches = []
    for route, patterns in ROUTE_RULES.items():
        if any(re.search(p, text) for p in patterns):
            matches.append(route)
    if len(matches) == 1:
        return matches[0]
    # Medication workflow owns medication questions, and may call research once for context.
    if "medications" in matches and len(matches) == 2 and "symptoms" in matches:
        return "medications"
    return None


def _discover_a2a_cards():
    """Read only the fixed allowlist of cards; never discover or invoke arbitrary agents."""
    global _a2a_cards
    if _a2a_cards is not None:
        return _a2a_cards
    with _a2a_cards_lock:
        if _a2a_cards is not None:
            return _a2a_cards
        import requests
        from . import security
        from .a2a_registry import card_urls, rpc_urls
        def fetch(name, url):
            response = requests.get(url, headers={"x-app-secret": security.SECRET} if security.SECRET else {},
                                    timeout=3)
            response.raise_for_status()
            card = response.json()
            if not card.get("name") or not card.get("skills"):
                raise ValueError(f"Invalid A2A Agent Card for allowlisted agent {name}")
            expected_skill = "kidney-literature" if name == "research" else name
            if not any(skill.get("id") == expected_skill for skill in card["skills"]):
                raise ValueError(f"Agent Card skill mismatch for allowlisted agent {name}")
            return name, {"card": card, "rpc_url": rpc_urls()[name], "card_url": url}

        cards = {}
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(fetch, name, url) for name, url in card_urls().items()]
            for future in as_completed(futures):
                name, info = future.result()
                cards[name] = info
        _a2a_cards = cards
        return cards


def _call_a2a_specialist(tab, user_id, messages, summary):
    """Dispatch to the tab's fixed A2A peer after validating its Agent Card."""
    agent = {"symptoms": "symptoms", "medications": "medications", "resources": "resources",
             "doctors": "providers"}.get(tab)
    if not agent:
        return None
    import requests
    from . import security
    cards = _discover_a2a_cards()
    selected = cards[agent]
    request_id = str(uuid.uuid4())
    current = messages[-1]["content"]
    history = messages[:-1]
    headers = {"Content-Type": "application/json", "A2A-Version": "1.0", "x-user-id": user_id}
    if security.SECRET:
        headers["x-app-secret"] = security.SECRET
    response = requests.post(selected["rpc_url"], headers=headers,
        timeout=(3, config.A2A_SPECIALIST_TIMEOUT_SECONDS),
        json={"jsonrpc": "2.0", "id": request_id, "method": "SendMessage",
              "params": {"message": {"messageId": request_id, "role": "ROLE_USER",
                                       "parts": [{"text": current}],
                                       "metadata": {"user_id": user_id, "history": history,
                                                    "thread_summary": summary, "safety_checked": True}},
                         "configuration": {"blocking": True}}})
    response.raise_for_status()
    envelope = response.json()
    if envelope.get("error"):
        raise RuntimeError(envelope["error"].get("message", "A2A specialist returned an error"))
    result = envelope.get("result", {})
    task = result.get("task") or result
    for artifact in task.get("artifacts", []):
        for part in artifact.get("parts", []):
            data = part.get("data")
            if isinstance(data, dict) and "answer" in data:
                return {**data, "agent": agent, "agent_card_url": selected["card_url"]}
    raise RuntimeError(f"A2A specialist {agent} returned no structured response")


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
    tool_names = agents.TABS[tab]["tools"]
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
            available = tool_names
            if tab == "medications" and round_no == 0:
                available = [name for name in tool_names if name != "deep_research"]
            if tab == "medications" and ctx.get("research_delegations", 0) >= 1:
                available = [name for name in available if name != "deep_research"]
            kwargs["tools"] = tools.schemas(available)
        llm_started = time.perf_counter()
        resp = llm.messages.create(**kwargs)
        _record_timing(ctx, "llm_call", time.perf_counter() - llm_started,
                       phase="answer", round=round_no + 1)
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
            retry_started = time.perf_counter()
            resp = llm.messages.create(**retry_kwargs)
            _record_timing(ctx, "llm_call", time.perf_counter() - retry_started,
                           phase="empty_response_retry", round=round_no + 1)
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
        def run_timed(name, args):
            started = time.perf_counter()
            output = tools.run(name, args, ctx)
            return output, time.perf_counter() - started

        futures = {b.id: _pool.submit(run_timed, b.name, b.input) for b in tool_uses}
        results = []
        for b in tool_uses:
            out, tool_elapsed = futures[b.id].result()
            _record_timing(ctx, "tool_call", tool_elapsed, tool=b.name, round=round_no + 1)
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


def _schedule_summary(store, llm, thread):
    """Queue optional thread summarization so it never holds the user's answer open."""
    tid = thread["thread_id"]
    with _summary_lock:
        if tid in _summaries_inflight:
            return False
        _summaries_inflight.add(tid)

    def work():
        started = time.perf_counter()
        try:
            maybe_summarize(store, llm, thread)
        except Exception:
            log.exception("Background thread summary failed")
        finally:
            log.info("latency_trace %s", json.dumps({"thread_id": tid, "stage": "background_summary",
                       "seconds": round(time.perf_counter() - started, 3)}))
            with _summary_lock:
                _summaries_inflight.discard(tid)

    try:
        _pool.submit(work)
        return True
    except Exception:
        with _summary_lock:
            _summaries_inflight.discard(tid)
        return False


def handle_chat(store, llm, user_id, tab, message, thread_id=None, debug=False):
    t0 = time.perf_counter()
    timings = []
    auto_route = tab == "auto"
    if auto_route:
        tab = route_query(message)
        if tab is None:
            return {"thread_id": None, "message_id": None,
                    "answer": "I can help with kidney health research, medicines, providers, or practical resources. Which part would you like to focus on?",
                    "urgent": False, "route": "clarify", "sources": [], "retrieved": [],
                    "tool_calls": [], "usage": {}, "latency_s": 0}
    if tab not in agents.TABS:
        raise ValueError(f"unknown tab {tab}")
    stage_started = time.perf_counter()
    thread = store.get_thread(thread_id) if thread_id else None
    if thread is None or thread["user_id"] != user_id or thread["tab"] != tab:
        thread_id = store.create_thread(user_id, tab, title=message[:60])
        thread = store.get_thread(thread_id)
    _record_timing({"timings": timings}, "thread_load", time.perf_counter() - stage_started)

    stage_started = time.perf_counter()
    urgent = safety.check(message, llm)
    _record_timing({"timings": timings}, "safety_gate", time.perf_counter() - stage_started)
    if urgent:
        store.add_message(thread_id, "user", message)
        mid = store.add_message(thread_id, "assistant", urgent, meta=presentation.urgent(urgent))
        return {"thread_id": thread_id, "message_id": mid, "answer": urgent, "urgent": True, "sources": [],
                "related_history": [],
                "retrieved": [], "tool_calls": [], "usage": {}, **presentation.urgent(urgent),
                "latency_s": round(time.perf_counter() - t0, 2), "latency_breakdown": timings}

    stage_started = time.perf_counter()
    profile = store.get_profile(user_id)
    messages = _history(store, thread) + [{"role": "user", "content": message}]
    ctx = {"user_id": user_id, "profile": profile, "store": store, "llm": llm, "tab": tab,
           "debug": debug, "research_delegations": 0, "usage": {}, "timings": timings}
    _record_timing(ctx, "profile_and_history", time.perf_counter() - stage_started)
    a2a_route = None
    a2a_timed_out = False
    if config.A2A_SPECIALISTS_ENABLED:
        stage_started = time.perf_counter()
        try:
            a2a_route = _call_a2a_specialist(tab, user_id, messages, thread.get("summary"))
        except Exception as exc:
            import requests
            a2a_timed_out = isinstance(exc, requests.exceptions.Timeout)
            if a2a_timed_out:
                log.warning("A2A specialist timed out for tab=%s; returning a bounded retry response", tab)
            else:
                # Fast connection/protocol failures can safely use the in-process specialist.
                log.warning("A2A specialist dispatch failed for tab=%s; using local agent: %s", tab, exc)
        _record_timing(ctx, "a2a_dispatch", time.perf_counter() - stage_started,
                       succeeded=bool(a2a_route), timed_out=a2a_timed_out)
    if a2a_route:
        answer = a2a_route["answer"]
        sources = a2a_route.get("sources", [])
        calls = a2a_route.get("tool_calls", [])
        revision = a2a_route.get("revision", {"attempted": False, "accepted": False, "reason": None})
        _, report = grounding.check(answer, sources, tab)
        ctx["usage"] = a2a_route.get("usage", {})
        timings.extend(a2a_route.get("timings", []))
        ctx["provider_search"] = a2a_route.get("provider_search")
    elif a2a_timed_out:
        answer = ("I couldn't complete this request within the response time limit. Please try again; "
                  "your question and saved information are still here.")
        sources, calls = [], []
        answer, report = grounding.check(answer, sources, tab)
        revision = {"attempted": False, "accepted": False, "reason": None}
    else:
        stage_started = time.perf_counter()
        answer, report, sources, calls, revision = generate_with_revision(
            llm, tab, messages, _system_blocks(tab, profile, thread.get("summary")), ctx)
        _record_timing(ctx, "agent_generation_and_grounding", time.perf_counter() - stage_started)
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
    stage_started = time.perf_counter()
    related = store.similar_past(user_id, message, exclude_thread=thread_id)
    store.add_message(thread_id, "user", message)
    mid = store.add_message(thread_id, "assistant", answer, meta=shown)
    store.add_sources(mid, user_id, tab, sources)      # everything retrieved -> recallable later
    _record_timing(ctx, "persistence_and_history", time.perf_counter() - stage_started)
    _schedule_summary(store, llm, store.get_thread(thread_id))
    total = time.perf_counter() - t0
    _record_timing(ctx, "total", total)
    log.info("latency_trace %s", json.dumps({"tab": tab, "total_seconds": round(total, 3),
             "timings": timings, "llm_calls": ctx.get("usage", {}).get("llm_calls", 0),
             "tool_calls": len(calls), "a2a": bool(a2a_route)}))
    return {"thread_id": thread_id, "message_id": mid, "answer": answer, "urgent": False,
            "related_history": related,
            "sources": report["used_sources"], "retrieved": sources,
            "removed_citations": report["removed"], "tool_calls": calls, "usage": ctx.get("usage", {}),
            "revision": revision,
            **shown, **({"route": tab} if auto_route else {}),
            **({"agent": a2a_route["agent"], "agent_card_url": a2a_route["agent_card_url"]}
               if a2a_route else {}),
            "latency_s": round(total, 2), "latency_breakdown": timings}
