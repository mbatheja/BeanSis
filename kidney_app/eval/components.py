"""
Component evals with ground-truth labels. Cheap and deterministic; run them on every change.

    python -m eval.components safety            # rules only (free, offline)
    python -m eval.components safety --llm      # rules + fast-model check (what production runs)
    python -m eval.components retrieval --k 5   # needs reference.duckdb
    python -m eval.components drugs             # needs network (RxNav)
    python -m eval.components pregnancy_flag    # needs reference.duckdb + labeled CSV
    python -m eval.components directory         # labeled CSV only
    python -m eval.components all
Results are appended to eval/results/components.jsonl so they show up in the scorecard.
"""
import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
DATA = HERE / "datasets"


def _yaml(name):
    return yaml.safe_load((DATA / name).read_text()) or []


def _prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    return p, r


def eval_safety(use_llm=False):
    from app import safety
    llm = None
    if use_llm:
        from app.llm import get_client
        llm = get_client()
    rows = _yaml("safety.yaml")
    tp = fp = fn = tn = 0
    misses, false_alarms = [], []
    for r in rows:
        pred = bool(safety.check(r["text"], llm) if use_llm else safety.rule_check(r["text"]))
        if pred and r["urgent"]:
            tp += 1
        elif pred:
            fp += 1
            false_alarms.append(r["text"])
        elif r["urgent"]:
            fn += 1
            misses.append(f"[{r['category']}] {r['text']}")
        else:
            tn += 1
    p, rec = _prf(tp, fp, fn)
    return {"component": "safety_llm" if use_llm else "safety_rules", "n": len(rows),
            "urgent_recall": rec, "precision": p, "false_positive_rate": fp / (fp + tn) if fp + tn else None,
            "missed_urgent": misses, "false_alarms": false_alarms}


def eval_retrieval(k=5):
    from app import refdb
    rows = [r for r in _yaml("retrieval.yaml") if "REPLACE_ME" not in r["relevant_doc_ids"]]
    if not rows:
        return {"component": "retrieval", "skipped": "fill relevant_doc_ids in retrieval.yaml"}
    hits, rr, misses = 0, [], []
    for r in rows:
        res = refdb.search_documents(r["query"], collection=r.get("collection"), limit=k)
        ranks = [i + 1 for i, d in enumerate(res) if d["doc_id"] in set(r["relevant_doc_ids"])]
        hits += bool(ranks)
        rr.append(1 / ranks[0] if ranks else 0)
        if not ranks:
            misses.append({"query": r["query"], "got": [d["doc_id"] for d in res]})
    return {"component": "retrieval", "n": len(rows), f"recall@{k}": hits / len(rows),
            "mrr": sum(rr) / len(rr), "misses": misses}


def eval_drugs():
    from app.tools.external import normalize_drug
    rows = _yaml("drugs.yaml")
    ok, wrong = 0, []
    for r in rows:
        try:
            ings = [i.lower() for i in normalize_drug(r["input"])["results"].get("ingredients", [])]
        except Exception as e:
            ings = [f"error: {e}"]
        if r["expected"].lower() in ings:
            ok += 1
        else:
            wrong.append({"input": r["input"], "expected": r["expected"], "got": ings})
    return {"component": "drug_normalization", "n": len(rows), "accuracy": ok / len(rows), "wrong": wrong}


def eval_pregnancy_flag():
    from app import refdb
    path = DATA / "pregnancy_flag_labels.csv"
    labels = [r for r in csv.DictReader(open(path)) if r["nct_id"] != "NCT00000000"]
    if not labels:
        return {"component": "pregnancy_flag", "skipped": f"label trials in {path.name}"}
    cur = refdb.cursor()
    tp = fp = fn = tn = unknown = 0
    errors = []
    for r in labels:
        cur.execute("SELECT excludes_pregnancy FROM trials WHERE nct_id = ?", [r["nct_id"]])
        row = cur.fetchone()
        pred, truth = (row[0] if row else None), r["human_excludes_pregnancy"].strip() == "1"
        if pred is None:
            unknown += 1
            continue
        tp += pred and truth
        fp += pred and not truth
        fn += (not pred) and truth
        tn += (not pred) and not truth
        if pred != truth:
            errors.append({"nct_id": r["nct_id"], "pred": pred, "human": truth})
    p, rec = _prf(tp, fp, fn)
    n = tp + fp + fn + tn
    return {"component": "pregnancy_flag", "n": n, "accuracy": (tp + tn) / n if n else None,
            "precision": p, "recall": rec, "no_flag_computed": unknown,
            "human_rate_excluding": (tp + fn) / n if n else None, "disagreements": errors}


def eval_directory():
    path = DATA / "directory_match_labels.csv"
    rows = [r for r in csv.DictReader(open(path)) if r["source_file"] != "example.pdf"]
    if not rows:
        return {"component": "directory_match", "skipped": f"label matches in {path.name}"}
    correct = sum(r["human_correct"].strip() == "1" for r in rows)
    return {"component": "directory_match", "n": len(rows), "match_precision": correct / len(rows),
            "wrong": [r for r in rows if r["human_correct"].strip() != "1"]}


RUNNERS = {
    "safety": lambda a: [eval_safety(False)] + ([eval_safety(True)] if a.llm else []),
    "retrieval": lambda a: [eval_retrieval(a.k)],
    "drugs": lambda a: [eval_drugs()],
    "pregnancy_flag": lambda a: [eval_pregnancy_flag()],
    "directory": lambda a: [eval_directory()],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("which", choices=list(RUNNERS) + ["all"])
    ap.add_argument("--llm", action="store_true", help="safety: also run the fast-model check")
    ap.add_argument("--k", type=int, default=5)
    a = ap.parse_args()
    names = list(RUNNERS) if a.which == "all" else [a.which]
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    for name in names:
        try:
            results = RUNNERS[name](a)
        except Exception as e:
            results = [{"component": name, "error": f"{type(e).__name__}: {e}"}]
        for r in results:
            r["when"] = datetime.now().isoformat(timespec="seconds")
            print(json.dumps(r, indent=2, default=str))
            with open(out / "components.jsonl", "a") as f:
                f.write(json.dumps(r, default=str) + "\n")


if __name__ == "__main__":
    main()
