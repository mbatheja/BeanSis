"""A2A endpoints for the app's non-research specialists.

Each specialist wraps its existing scoped tool set. The A2A request carries a user ID in message
metadata; the host loads that user's profile and app state locally. No profile is returned to callers.
"""
import asyncio
import logging

from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value
from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import (AgentCapabilities, AgentCard, AgentInterface, AgentSkill, Part, Task, TaskState,
                       TaskStatus)

from . import agents, grounding, presentation, safety
from .orchestrator import _system_blocks, generate_with_revision
from .you import visit_prep

log = logging.getLogger(__name__)

SPECIALISTS = {
    "symptoms": ("Symptoms & kidney health", "Answer kidney disease symptom and management questions using the research toolset.",
                 ["symptoms", "CKD", "dialysis", "transplant", "pregnancy"]),
    "medications": ("Medication analysis", "Analyze medication labels, reports, reviews, interactions, and request research context.",
                    ["medication", "FDA labels", "FAERS", "interactions", "kidney disease"]),
    "resources": ("Resources and coverage", "Find support resources, plan information, rides, and clinical trials.",
                  ["coverage", "community resources", "rides", "clinical trials"]),
    "providers": ("Provider directory", "Find providers from loaded insurer directories and NPPES.",
                  ["nephrology", "OB/GYN", "maternal fetal medicine", "insurance networks"]),
    "visit_prep": ("Visit preparation", "Prepare a patient for a selected clinician visit from her saved app context.",
                   ["visit summary", "questions for clinician", "appointment preparation"]),
}


def _metadata(message):
    raw = getattr(message, "metadata", None)
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return MessageToDict(raw, preserving_proto_field_name=True)
    except Exception:
        try:
            return dict(raw)
        except Exception:
            return {}


class SpecialistExecutor(AgentExecutor):
    def __init__(self, name, store, llm):
        self.name, self.store, self.llm = name, store, llm

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id, context_id = context.task_id, context.context_id
        message = context.message
        question = context.get_user_input()
        if not task_id or not context_id or not message or not question:
            return
        updater = TaskUpdater(event_queue=event_queue, task_id=task_id, context_id=context_id)
        await event_queue.enqueue_event(Task(id=task_id, context_id=context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED)))
        await updater.start_work()
        try:
            meta = _metadata(message)
            user_id = meta.get("user_id")
            if not user_id:
                raise ValueError("A user_id in A2A message metadata is required")
            # The authenticated in-process orchestrator already ran the LLM safety check.
            # Always repeat deterministic red-flag rules; run the model check for standalone A2A callers.
            urgent = (safety.rule_check(question) if meta.get("safety_checked")
                      else safety.check(question, self.llm))
            if urgent:
                payload = {"answer": urgent, "urgent": True, "sources": [], "tool_calls": []}
            elif self.name == "visit_prep":
                result = await asyncio.to_thread(visit_prep.generate, self.store, self.llm, user_id,
                    meta.get("visit_type", "other"), concerns=question,
                    days=max(1, min(int(meta.get("days", 30)), 90)),
                    n_min=max(5, min(int(meta.get("n_min", 8)), 15)),
                    n_max=max(7, min(int(meta.get("n_max", 12)), 20)),
                    visit_type_other=meta.get("visit_type_other"))
                payload = {"answer": result.get("markdown", ""), "visit_prep": result,
                           "sources": result.get("citations", [])}
            else:
                tab = "doctors" if self.name == "providers" else self.name
                profile = await asyncio.to_thread(self.store.get_profile, user_id)
                history = meta.get("history") if isinstance(meta.get("history"), list) else []
                messages = history[-12:] + [{"role": "user", "content": question}]
                ctx = {"user_id": user_id, "profile": profile, "store": self.store,
                       "llm": self.llm, "tab": tab, "debug": False, "research_delegations": 0,
                       "usage": {}, "timings": []}
                answer, report, sources, calls, revision = await asyncio.to_thread(
                    generate_with_revision, self.llm, tab, messages,
                    _system_blocks(tab, profile, meta.get("thread_summary")), ctx)
                shown = presentation.build(tab, answer, report, sources, calls, grounding.CLINICAL_TABS)
                payload = {"answer": answer, "sources": sources, "citations": shown["citations"],
                           "notices": shown["notices"], "guardrails": shown["guardrails"],
                           "tool_calls": calls, "revision": revision, "route": self.name,
                           "provider_search": ctx.get("provider_search"), "usage": ctx.get("usage", {}),
                           "timings": ctx.get("timings", [])}
            part = Part(data=ParseDict(payload, Value()))
            await updater.add_artifact(parts=[part], name=f"{self.name}-response", last_chunk=True)
            await updater.complete()
        except Exception as exc:
            log.exception("A2A %s specialist failed", self.name)
            await updater.failed(message=updater.new_agent_message(parts=[Part(text=str(exc)[:500])]))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.task_id:
            updater = TaskUpdater(event_queue=event_queue, task_id=context.task_id,
                                  context_id=context.context_id or "")
            await updater.cancel()


def _agent_app(name, store, llm, base_url):
    title, description, tags = SPECIALISTS[name]
    base = f"{base_url.rstrip('/')}/a2a/agents/{name}"
    card = AgentCard(name=f"Bean Sis {title}", description=description, version="1.0.0",
        capabilities=AgentCapabilities(streaming=False), default_input_modes=["text"],
        default_output_modes=["application/json"],
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", protocol_version="1.0",
                                               url=f"{base}/jsonrpc")],
        skills=[AgentSkill(id=name, name=title, description=description, tags=tags,
            examples=["Help me with a kidney care question."], input_modes=["text"],
            output_modes=["application/json"])])
    handler = DefaultRequestHandler(agent_executor=SpecialistExecutor(name, store, llm),
                                    task_store=InMemoryTaskStore(), agent_card=card)
    subapp = FastAPI(title=f"Bean Sis {title} A2A")
    add_a2a_routes_to_fastapi(subapp, agent_card_routes=create_agent_card_routes(agent_card=card),
        jsonrpc_routes=create_jsonrpc_routes(request_handler=handler, rpc_url="/jsonrpc"))
    return subapp, card


def mount_specialists(app, store, llm, base_url):
    """Mount an independent Agent Card and A2A JSON-RPC endpoint for every local specialist."""
    cards = {}
    for name in SPECIALISTS:
        subapp, card = _agent_app(name, store, llm, base_url)
        app.mount(f"/a2a/agents/{name}", subapp, name=f"a2a-{name}")
        cards[name] = card
    return cards


def card_summary(name, card, card_url):
    return {"id": name, "name": card.name, "description": card.description,
            "agent_card_url": card_url, "skills": [s.model_dump(mode="json") for s in card.skills]}
