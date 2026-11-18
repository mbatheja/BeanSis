"""A2A endpoint for the bounded, read-only literature specialist.

This can run as a separate service or be hosted by Agent Stack. It receives only a research
question and returns its cited synthesis; profiles, medications, and app memory stay in the host.
"""
import argparse
import asyncio
import logging

import uvicorn
from fastapi import FastAPI
from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.tasks.task_updater import TaskUpdater
from google.protobuf.struct_pb2 import Value
from google.protobuf.json_format import ParseDict
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, Part, Task, TaskState, TaskStatus

from . import config
from .llm import get_client
from .tools.local import deep_research

log = logging.getLogger(__name__)


class ResearchExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id, context_id = context.task_id, context.context_id
        question = context.get_user_input()
        if not task_id or not context_id or not question:
            return
        updater = TaskUpdater(event_queue=event_queue, task_id=task_id, context_id=context_id)
        await event_queue.enqueue_event(Task(id=task_id, context_id=context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED)))
        await updater.start_work()
        try:
            result = await asyncio.to_thread(deep_research,
                {"question": question, "max_papers": 6},
                {"llm": get_client(), "tab": "symptoms", "profile": {}})
            text = result.get("results", {}).get("synthesis") or "No relevant literature found."
            # Include source records as structured evidence for the caller to preserve citation provenance.
            artifact = Part(data=ParseDict({"synthesis": text, "sources": result.get("sources", []),
                "evidence_gap": result.get("results", {}).get("evidence_gap", False)}, Value()))
            await updater.add_artifact(parts=[artifact], name="research-evidence", last_chunk=True)
            await updater.complete()
        except Exception as exc:
            log.exception("A2A research task failed")
            await updater.failed(message=updater.new_agent_message(parts=[Part(text=str(exc)[:500])]))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.task_id:
            updater = TaskUpdater(event_queue=event_queue, task_id=context.task_id,
                                  context_id=context.context_id or "")
            await updater.cancel()


def create_app() -> FastAPI:
    url = config.A2A_RESEARCH_URL
    card = AgentCard(name="Bean Sis Research", description="Read-only kidney health literature search and synthesis.",
        version="1.0.0", capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["text"], default_output_modes=["text", "application/json"],
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", protocol_version="1.0",
                                               url=f"{url}/a2a/jsonrpc")],
        skills=[AgentSkill(id="kidney-literature", name="Kidney literature research",
            description="Search PubMed and the curated research library; return a cited synthesis and sources.",
            tags=["kidney", "CKD", "dialysis", "transplant", "pregnancy", "bone health"],
            examples=["Summarize evidence on CKD and pregnancy outcomes."],
            input_modes=["text"], output_modes=["text", "application/json"])])
    handler = DefaultRequestHandler(agent_executor=ResearchExecutor(), task_store=InMemoryTaskStore(),
                                    agent_card=card)
    app = FastAPI(title="Bean Sis Research A2A")
    @app.get("/healthz")
    def healthz():
        return {"ok": True, "agent": "research"}

    add_a2a_routes_to_fastapi(app, agent_card_routes=create_agent_card_routes(agent_card=card),
                              jsonrpc_routes=create_jsonrpc_routes(request_handler=handler,
                                                                   rpc_url="/a2a/jsonrpc"))
    return app


app = create_app()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8002)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
