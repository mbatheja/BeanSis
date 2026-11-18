"""Compare existing BM25 retrieval with persisted semantic + BM25 RRF retrieval.

Example:
  python -m eval.retrieval_benchmark --db data/reference_candidate.rag-eval.duckdb --k 8
"""
import argparse
import json
import math
import os
import statistics
import time
from datetime import datetime
from pathlib import Path

import duckdb
import yaml

HERE = Path(__file__).resolve().parent


def _load_workspace_env():
    """Load evaluation-only provider config without ever echoing secret values."""
    os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")
    env_path = HERE.parent / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass


def _percentile(values, p):
    ordered = sorted(values)
    if not ordered:
        return None
    i = (len(ordered) - 1) * p
    lo, hi = math.floor(i), math.ceil(i)
    return ordered[lo] if lo == hi else ordered[lo] * (hi - i) + ordered[hi] * (i - lo)


def _retrieval_stats(rows, relevant_ids, k):
    ids = [row["doc_id"] for row in rows[:k]]
    relevant = set(relevant_ids)
    hits = [i + 1 for i, doc_id in enumerate(ids) if doc_id in relevant]
    dcg = sum(1 / math.log2(rank + 1) for rank in hits)
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(k, len(relevant)) + 1))
    return {"hit_at_k": float(bool(hits)), "recall_at_k": len(hits) / len(relevant) if relevant else 0.0,
            "precision_at_k": len(hits) / k if k else 0.0, "mrr": 1 / hits[0] if hits else 0.0,
            "ndcg_at_k": dcg / ideal if ideal else 0.0, "retrieved_doc_ids": ids}


def _ragas_scores(cases, output_rows):
    """Compute RAGAS IDBasedContextPrecision/Recall exactly, without LLM judging."""
    out = {}
    for mode in ("bm25", "hybrid"):
        p_scores, r_scores = [], []
        for case, results in zip(cases, output_rows[mode]):
            retrieved = set(map(str, results["retrieved_doc_ids"]))
            reference = set(map(str, case["relevant_doc_ids"]))
            hits = len(retrieved & reference)
            p_scores.append(hits / len(retrieved) if retrieved else 0.0)
            r_scores.append(hits / len(reference) if reference else 0.0)
        out[mode] = {"ragas_id_context_precision": statistics.mean(p_scores),
                     "ragas_id_context_recall": statistics.mean(r_scores)}
    return out


def run(db_path, k=8, repeats=3, use_ragas=True):
    _load_workspace_env()
    from app import refdb, semantic

    cases = yaml.safe_load((HERE / "datasets" / "retrieval.yaml").read_text(encoding="utf-8")) or []
    if not cases:
        raise RuntimeError("No labeled retrieval cases found")
    con = duckdb.connect(str(db_path))
    con.execute("LOAD fts")
    known = {r[0] for r in con.execute("SELECT DISTINCT doc_id FROM chunks").fetchall()}
    unknown = sorted({doc for case in cases for doc in case["relevant_doc_ids"] if doc not in known})
    if unknown:
        raise ValueError(f"Gold labels refer to missing document IDs: {unknown}")

    started = time.perf_counter()
    index = semantic.ensure_index(con)
    index_seconds = time.perf_counter() - started

    output_rows = {"bm25": [], "hybrid": []}
    latency = {"bm25": [], "hybrid": [], "query_embedding": [], "hybrid_bm25": [],
               "hybrid_vector": [], "hybrid_fusion": []}
    for case in cases:
        query = case["query"]
        collection = case.get("collection")
        bm_times = []
        bm_results = None
        for _ in range(max(1, repeats)):
            t0 = time.perf_counter()
            bm_results = refdb.search_documents(query, collection=collection, limit=k, con=con)
            bm_times.append(time.perf_counter() - t0)
        latency["bm25"].append(statistics.median(bm_times))
        output_rows["bm25"].append(_retrieval_stats(bm_results, case["relevant_doc_ids"], k))

        timings = {}
        t0 = time.perf_counter()
        hybrid_results = refdb.search_documents_hybrid(query, collection=collection, limit=k,
                                                        con=con, timings=timings)
        total = time.perf_counter() - t0
        latency["hybrid"].append(total)
        latency["query_embedding"].append(timings["embedding_seconds"])
        latency["hybrid_bm25"].append(timings["bm25_seconds"])
        latency["hybrid_vector"].append(timings["semantic_seconds"])
        latency["hybrid_fusion"].append(timings["fusion_seconds"])
        output_rows["hybrid"].append(_retrieval_stats(hybrid_results, case["relevant_doc_ids"], k))

    metrics = {}
    for mode in ("bm25", "hybrid"):
        mode_rows = output_rows[mode]
        metrics[mode] = {key: statistics.mean(row[key] for row in mode_rows)
                         for key in ("hit_at_k", "recall_at_k", "precision_at_k", "mrr", "ndcg_at_k")}
        metrics[mode]["latency_seconds"] = {
            "p50": statistics.median(latency[mode]), "p95": _percentile(latency[mode], .95),
            "mean": statistics.mean(latency[mode]),
        }
    metrics["hybrid"]["latency_breakdown_seconds_mean"] = {
        key: statistics.mean(values) for key, values in latency.items() if key not in ("bm25", "hybrid")
    }
    if use_ragas:
        metrics["ragas_id_based"] = _ragas_scores(cases, output_rows)

    report = {"created_at": datetime.now().isoformat(timespec="seconds"), "database": str(db_path),
              "corpus": {"documents": len(known), "chunks": con.execute("SELECT count(*) FROM chunks").fetchone()[0]},
              "query_count": len(cases), "k": k, "embedding_index": index,
              "embedding_index_build_seconds": index_seconds, "metrics": metrics,
              "cases": [{"query": case["query"], "collection": case.get("collection"),
                         "relevant_doc_ids": case["relevant_doc_ids"], "bm25": output_rows["bm25"][i],
                         "hybrid": output_rows["hybrid"][i]} for i, case in enumerate(cases)]}
    con.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Writable evaluation copy of the reference DuckDB")
    parser.add_argument("--k", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--no-ragas", action="store_true", help="Run deterministic metrics only")
    args = parser.parse_args()
    report = run(Path(args.db), k=args.k, repeats=args.repeats, use_ragas=not args.no_ragas)
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"retrieval-comparison-{datetime.now():%Y%m%d-%H%M%S}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["metrics"], indent=2))
    print(f"Full per-query report: {out_path}")


if __name__ == "__main__":
    main()
