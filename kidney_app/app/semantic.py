"""Persistent chunk embeddings served through the configured OpenAI-compatible provider."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from . import config

EMBEDDING_BATCH_SIZE = 128


def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not config.NEBIUS_API_KEY:
        raise RuntimeError("NEBIUS_API_KEY is required for semantic search")
    from openai import OpenAI
    client = OpenAI(api_key=config.NEBIUS_API_KEY, base_url=config.NEBIUS_BASE_URL,
                    timeout=90.0, max_retries=2)
    vectors = []
    batches = range(0, len(texts), EMBEDDING_BATCH_SIZE)
    batch_count = (len(texts) + EMBEDDING_BATCH_SIZE - 1) // EMBEDDING_BATCH_SIZE
    for batch_number, start in enumerate(batches, 1):
        batch = texts[start:start + EMBEDDING_BATCH_SIZE]
        response = client.embeddings.create(model=config.EMBEDDING_MODEL, input=batch)
        vectors.extend(item.embedding for item in sorted(response.data, key=lambda item: item.index))
        if batch_number == 1 or batch_number % 10 == 0 or batch_number == batch_count:
            print(f"embedding progress: {len(vectors)}/{len(texts)} texts", flush=True)
    if len(vectors) != len(texts) or any(len(v) != config.EMBEDDING_DIMENSIONS for v in vectors):
        raise RuntimeError(f"Embedding model returned an unexpected vector shape; expected {config.EMBEDDING_DIMENSIONS} dimensions")
    return vectors


def has_index(con) -> bool:
    return bool(con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='chunk_embeddings'").fetchone()[0])


def create_index(con):
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS chunk_embeddings (
            chunk_id VARCHAR PRIMARY KEY,
            doc_id VARCHAR NOT NULL,
            text_sha256 VARCHAR NOT NULL,
            embedding_model VARCHAR NOT NULL,
            embedding FLOAT[{config.EMBEDDING_DIMENSIONS}] NOT NULL,
            embedded_at VARCHAR NOT NULL
        )
    """)


def prepare_missing(con, rows: list[dict]) -> list[tuple]:
    """Create vectors only for new/changed text; return DB-ready vector rows."""
    existing = {}
    if has_index(con):
        existing = {row[0]: (row[1], row[2]) for row in con.execute(
            "SELECT chunk_id, text_sha256, embedding_model FROM chunk_embeddings").fetchall()}
    pending = [row for row in rows if existing.get(row["chunk_id"]) !=
               (text_hash(row.get("text") or ""), config.EMBEDDING_MODEL)]
    vectors = embed_texts([row.get("text") or "" for row in pending]) if pending else []
    embedded_at = datetime.now(timezone.utc).isoformat()
    return [(row["chunk_id"], row["doc_id"], text_hash(row.get("text") or ""), config.EMBEDDING_MODEL,
             vector, embedded_at) for row, vector in zip(pending, vectors)]


def write_vectors(con, vectors: list[tuple]):
    create_index(con)
    if vectors:
        con.executemany(f"""
            INSERT OR REPLACE INTO chunk_embeddings
            VALUES (?, ?, ?, ?, ?::FLOAT[{config.EMBEDDING_DIMENSIONS}], ?)
        """, vectors)


def ensure_index(con, rows: list[dict] | None = None) -> dict:
    """Ensure a complete persistent index for current chunks (used by evaluation/bootstrap)."""
    if rows is None:
        rows = con.execute("SELECT chunk_id, doc_id, text FROM chunks ORDER BY chunk_id").fetchdf().to_dict("records")
    create_index(con)
    existing = {row[0]: (row[1], row[2]) for row in con.execute(
        "SELECT chunk_id, text_sha256, embedding_model FROM chunk_embeddings").fetchall()}
    pending = [row for row in rows if existing.get(row["chunk_id"]) !=
               (text_hash(row.get("text") or ""), config.EMBEDDING_MODEL)]
    for start in range(0, len(pending), EMBEDDING_BATCH_SIZE):
        batch = pending[start:start + EMBEDDING_BATCH_SIZE]
        vectors = embed_texts([row.get("text") or "" for row in batch])
        embedded_at = datetime.now(timezone.utc).isoformat()
        vector_rows = [(row["chunk_id"], row["doc_id"], text_hash(row.get("text") or ""),
                        config.EMBEDDING_MODEL, vector, embedded_at)
                       for row, vector in zip(batch, vectors)]
        write_vectors(con, vector_rows)
        print(f"semantic index progress: {min(start + len(batch), len(pending))}/{len(pending)} changed chunks", flush=True)
    con.execute("DELETE FROM chunk_embeddings WHERE chunk_id NOT IN (SELECT chunk_id FROM chunks)")
    return {"embedded": len(pending), "total": con.execute("SELECT count(*) FROM chunk_embeddings").fetchone()[0],
            "model": config.EMBEDDING_MODEL}


def embed_query(query: str) -> list[float]:
    return embed_texts([query])[0]
