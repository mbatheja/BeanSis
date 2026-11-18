"""Allowlisted A2A specialist addresses used by the orchestrator and registry endpoint."""
from . import config

LOCAL_SPECIALISTS = ("symptoms", "medications", "resources", "providers", "visit_prep")


def card_urls():
    base = config.A2A_API_BASE_URL.rstrip("/")
    return {name: f"{base}/a2a/agents/{name}/.well-known/agent-card.json"
            for name in LOCAL_SPECIALISTS} | {
                "research": f"{config.A2A_RESEARCH_URL.rstrip('/')}/.well-known/agent-card.json"}


def rpc_urls():
    base = config.A2A_API_BASE_URL.rstrip("/")
    return {name: f"{base}/a2a/agents/{name}/jsonrpc" for name in LOCAL_SPECIALISTS} | {
        "research": f"{config.A2A_RESEARCH_URL.rstrip('/')}/a2a/jsonrpc"}
