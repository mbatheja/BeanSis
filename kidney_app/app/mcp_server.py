"""MCP boundary for public evidence and directory tools.

Run with ``python -m app.mcp_server``. Patient-specific state is intentionally not exposed here;
tools accept only the minimum query/location arguments required for retrieval.
"""
import os

from mcp.server.fastmcp import FastMCP

from . import refdb
from .llm import get_client
from .tools import external, local, reviews

mcp = FastMCP("bean-sis-domain-tools")


def _public_document(row: dict) -> dict:
    """Keep provenance useful to clients without exposing local absolute filesystem paths."""
    fields = ("chunk_id", "doc_id", "collection", "title", "url", "year", "pub_type", "tags", "text",
              "pmid", "doi", "authors", "author_keywords", "mesh_terms", "abstract", "topics", "full_text_status",
              "source_type", "source_uri", "source_file", "source_hash", "ingest_version", "ingested_at",
              "page_number", "chunk_index")
    return {key: row.get(key) for key in fields if key in row}


@mcp.tool()
def search_research(query: str, min_year: int | None = None, limit: int = 6) -> list[dict]:
    """Search the curated kidney health research and guideline corpus (BM25)."""
    return [_public_document(row) for row in refdb.search_documents(
        query, collection="research", min_year=min_year, limit=max(1, min(limit, 12)))]


@mcp.tool()
def search_pubmed(question: str, pubmed_query: str | None = None, min_year: int | None = None) -> dict:
    """Search PubMed and the curated corpus, returning a cited evidence synthesis."""
    result = local.deep_research({"question": question, "pubmed_query": pubmed_query,
                                  "min_year": min_year},
                                 {"llm": get_client(), "tab": "symptoms", "profile": {}})
    result["sources"] = [{k: v for k, v in source.items() if k != "source_file"}
                         for source in result.get("sources", [])]
    return result


@mcp.tool()
def search_support_resources(query: str, zip_code: str | None = None,
                             state: str | None = None, limit: int = 8) -> list[dict]:
    """Find community and practical support resources, preferring matching ZIP/state tags."""
    return [_public_document(row) for row in refdb.search_resources(
        query, zip_code=zip_code, state=state, limit=max(1, min(limit, 12)))]


@mcp.tool()
def find_in_network_providers(specialty: str, zip_code: str, plan_id: str | None = None,
                              radius_miles: int = 25, limit: int = 10) -> dict | list[dict]:
    """Search loaded insurer directory listings near a ZIP; results are not quality-ranked."""
    return refdb.find_providers(specialty, zip_code, plan_id=plan_id,
                                radius_miles=max(1, min(radius_miles, 100)),
                                limit=max(1, min(limit, 25)))


@mcp.tool()
def medication_label(name: str) -> dict:
    """Resolve a medication and retrieve official FDA label information, including labeled uses."""
    normalized = external.normalize_drug(name)
    ingredients = normalized.get("results", {}).get("ingredients", [])
    ingredient = ingredients[0] if ingredients else name
    label = external.drug_label(ingredient, sections=["indications_and_usage", "pregnancy",
        "pregnancy_or_breast_feeding", "lactation", "use_in_specific_populations",
        "warnings_and_cautions", "adverse_reactions"])
    return {"normalized": normalized.get("results"), "label": label.get("results"),
            "sources": normalized.get("sources", []) + label.get("sources", [])}


@mcp.tool()
def medication_faers(generic_name: str, female_only: bool = True) -> dict:
    """Retrieve FDA adverse-event report counts; counts are not rates and do not prove causation."""
    return external.faers_summary(generic_name, female_only=female_only)


@mcp.tool()
def medication_patient_experiences(drug: str, condition: str | None = None) -> dict:
    """Search self-selected patient review themes, not evidence of frequency or efficacy."""
    return reviews.patient_reviews({"drug": drug, "condition": condition}, {})


def main():
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        mcp.settings.host = os.getenv("MCP_HOST", "127.0.0.1")
        mcp.settings.port = int(os.getenv("MCP_PORT", "8001"))
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
