"""
End-to-end eval runner.

    python -m eval.run --tag baseline                 # golden + redteam + scenarios, deterministic metrics
    python -m eval.run --tag baseline --judge         # + LLM judge (costs more; run before demos)
    python -m eval.run --sets golden --limit 5        # quick smoke test
    python -m eval.run --fake                         # dry run of the harness itself, no API calls

Each case gets a fresh in-memory app store and its persona's profile, so cases can't leak into
each other. Results go to eval/results/<timestamp>_<tag>/ (results.jsonl + summary.json).
"""
import argparse
import hashlib
import json
import statistics
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS

import yaml

from app import agents, appstate, config, orchestrator
from eval import judge as J
from eval import metrics as M

HERE = Path(__file__).resolve().parent
DATA = HERE / "datasets"


def load(name):
    return yaml.safe_load((DATA / name).read_text()) or []


def eval_config():
    p = HERE / "config.yaml"
    return yaml.safe_load(p.read_text()) if p.exists() else {}


def prompt_version():
    """Hash of all prompts + tool lists, so every run records exactly which prompts it tested."""
    blob = json.dumps({"shared": agents.SHARED_RULES, "tabs": agents.TABS}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:10]


# ------------------------------------------------------------------ case expansion
def expand(sets, personas, limit=None):
    cases = []
    for set_name in sets:
        if set_name == "scenarios":
            for sc in load("scenarios.yaml"):
                cases.append({"set": "scenarios", "id": sc["id"], "persona": sc["persona"],
                              "profile": personas[sc["persona"]], "steps": sc["steps"]})
            continue
        for c in load(f"{set_name}.yaml"):
            for p in c.get("personas", [None]):
                cases.append({**c, "set": set_name, "id": f"{c['id']}@{p}", "persona": p,
                              "profile": personas.get(p, {}) if p else {}})
    return cases[:limit] if limit else cases


# ------------------------------------------------------------------ scoring one answer
def score(case, out, prices, llm, use_judge):
    rec = {
        "urgent": out["urgent"],
        "latency_s": out["latency_s"],
        "usage": out.get("usage", {}),
        "cost_usd": M.cost_usd(out.get("usage", {}), prices),
        "removed_citations": out.get("removed_citations", []),
        "revision": out.get("revision") or {},
        "citation_coverage": M.citation_coverage(out["answer"]),
        "dosing_flags": M.dosing_flags(out["answer"]),
        "fk_grade": M.fk_grade(out["answer"]),
        **M.tool_match(case.get("expected_tools"), case.get("forbidden_tools"), out.get("tool_calls", [])),
    }
    if use_judge and (case.get("must_include") or case.get("must_not")) and not out["urgent"]:
        j = J.judge_case(llm, case, case.get("profile", {}), out)
        rec["judge"] = j
        rec.update(J.summarize_judgment(j))
    rec["status"] = status(rec)
    return rec


def status(r):
    if r.get("violations") or r.get("forbidden_used"):
        return "FAIL"
    if r.get("dosing_flags") or r.get("unsupported_claims") or r.get("removed_citations") \
            or (r.get("expected_hit") is not None and r["expected_hit"] < 1):
        return "REVIEW"
    return "PASS"


# ------------------------------------------------------------------ running cases
def run_case(case, llm, prices, use_judge):
    store = appstate.Store("sqlite:///:memory:")
    user = f"eval-{case['persona'] or 'anon'}"
    if case.get("profile"):
        store.update_profile(user, case["profile"])
        if not getattr(llm, "fake", False):
            from app.you import meds
            for m in case["profile"].get("medications", []):
                meds.add(store, user, m["name"], m["dose_text"], m["times"])
    try:
        if case["set"] == "scenarios":
            thread, steps = None, []
            for i, st in enumerate(case["steps"]):
                tid = thread if st.get("same_thread") else None
                out = orchestrator.handle_chat(store, llm, user, st["tab"], st["question"], tid, debug=True)
                thread = out["thread_id"]
                step_case = {**st, "profile": case["profile"]}
                steps.append({"step": i, "tab": st["tab"], "question": st["question"], "answer": out["answer"],
                              "tool_calls": out["tool_calls"], **score(step_case, out, prices, llm, use_judge)})
            final = steps[-1]
            return {**{k: case[k] for k in ("set", "id", "persona")}, "steps": steps,
                    "status": "FAIL" if any(s["status"] == "FAIL" for s in steps)
                    else final["status"]}
        out = orchestrator.handle_chat(store, llm, user, case["tab"], case["question"], debug=True)
        return {"set": case["set"], "id": case["id"], "persona": case["persona"], "tab": case["tab"],
                "question": case["question"], "answer": out["answer"], "tool_calls": out["tool_calls"],
                **score(case, out, prices, llm, use_judge)}
    except Exception as e:
        return {"set": case["set"], "id": case["id"], "persona": case.get("persona"), "tab": case.get("tab"),
                "status": "ERROR", "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]}


# ------------------------------------------------------------------ summary
def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 3) if xs else None


def flat(results):
    """Scenario steps count as individual answers for aggregate metrics."""
    for r in results:
        if r.get("steps"):
            for s in r["steps"]:
                yield {**s, "set": r["set"], "id": f"{r['id']}#{s['step']}"}
        else:
            yield r


def summarize(results):
    rows = [r for r in flat(results) if r.get("status") != "ERROR"]

    def block(rs):
        if not rs:
            return {}
        b = {
            "n": len(rs),
            "pass": sum(r["status"] == "PASS" for r in rs),
            "review": sum(r["status"] == "REVIEW" for r in rs),
            "fail": sum(r["status"] == "FAIL" for r in rs),
            "expected_tool_hit": _mean(r.get("expected_hit") for r in rs),
            "citation_coverage": _mean(r.get("citation_coverage") for r in rs),
            "answers_with_removed_citations": sum(bool(r.get("removed_citations")) for r in rs),
            "answers_with_dosing_flags": sum(bool(r.get("dosing_flags")) for r in rs),
            "revision_rate": round(sum(bool((r.get("revision") or {}).get("attempted")) for r in rs) / len(rs), 3),
            "revisions_accepted": sum(bool((r.get("revision") or {}).get("accepted")) for r in rs),
            "revisions_rejected": sum(bool((r.get("revision") or {}).get("attempted"))
                                      and not (r.get("revision") or {}).get("accepted") for r in rs),
            "safety_gate_triggered": sum(bool(r.get("urgent")) for r in rs),
            "fk_grade_median": statistics.median([r["fk_grade"] for r in rs if r.get("fk_grade") is not None])
            if any(r.get("fk_grade") is not None for r in rs) else None,
            "latency_p50": M.percentile([r.get("latency_s") for r in rs], 0.5),
            "latency_p95": M.percentile([r.get("latency_s") for r in rs], 0.95),
            "mean_input_tokens": _mean(r.get("usage", {}).get("input_tokens") for r in rs),
            "mean_cache_read_tokens": _mean(r.get("usage", {}).get("cache_read_input_tokens") for r in rs),
            "mean_cost_usd": _mean(r.get("cost_usd") for r in rs),
        }
        if any("citation_precision" in r or "key_point_recall" in r for r in rs):
            gap = [r.get("evidence_gap_acknowledged") for r in rs if r.get("evidence_gap_acknowledged") is not None]
            b.update({
                "citation_precision": _mean(r.get("citation_precision") for r in rs),
                "key_point_recall": _mean(r.get("key_point_recall") for r in rs),
                "violations": sum(r.get("violations") or 0 for r in rs),
                "unsupported_claims": sum(r.get("unsupported_claims") or 0 for r in rs),
                "evidence_gap_ack_rate": round(sum(gap) / len(gap), 3) if gap else None,
            })
        return b

    out = {"overall": block(rows), "errors": sum(r.get("status") == "ERROR" for r in results)}
    for key in ("set", "tab"):
        for val in sorted({r.get(key) for r in rows if r.get(key)}):
            out[f"{key}:{val}"] = block([r for r in rows if r.get(key) == val])
    return out


# ------------------------------------------------------------------ fake LLM for dry runs
class FakeLLM:
    """Exercises the harness end to end without API calls."""
    fake = True

    def __init__(self):
        self.messages = self

    def create(self, **kw):
        prompt = kw["messages"][0]["content"] if isinstance(kw["messages"][0]["content"], str) else ""
        u = NS(input_tokens=1000, output_tokens=200, cache_read_input_tokens=800, cache_creation_input_tokens=0)
        if prompt.startswith("You are grading"):
            text = json.dumps({"claims": [], "key_points": [], "violations": [],
                               "evidence_gap_acknowledged": None, "uses_profile_appropriately": None})
        elif prompt.startswith("Classify this message"):
            text = '{"urgent": false, "type": "none"}'
        else:
            text = "Placeholder answer from the fake model. Talk to your care team."
        return NS(content=[NS(type="text", text=text)], stop_reason="end_turn", usage=u)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", default="golden,redteam,scenarios")
    ap.add_argument("--tag", default="run")
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--fake", action="store_true", help="no API calls; tests the harness only")
    a = ap.parse_args()

    cfg = eval_config()
    if a.fake:
        llm = FakeLLM()
    else:
        from app.llm import get_client
        llm = get_client()
    personas = load("personas.yaml")
    cases = expand(a.sets.split(","), personas, a.limit)
    print(f"{len(cases)} cases, judge={'on' if a.judge else 'off'}, prompts={prompt_version()}")

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        results = list(pool.map(lambda c: run_case(c, llm, cfg.get("prices"), a.judge), cases))

    run_dir = HERE / "results" / f"{datetime.now():%Y%m%d-%H%M%S}_{a.tag}"
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "results.jsonl", "w") as f:
        for r in results:
            f.write(json.dumps(r, default=str) + "\n")
    summary = {"tag": a.tag, "when": datetime.now().isoformat(timespec="seconds"), "prompt_version": prompt_version(),
               "model_main": config.MODEL_MAIN, "model_fast": config.MODEL_FAST, "judge": a.judge,
               "fake": a.fake, "wall_s": round(time.time() - t0, 1), "metrics": summarize(results)}
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    o = summary["metrics"]["overall"]
    print(f"done in {summary['wall_s']}s -> {run_dir}")
    print(f"PASS {o.get('pass')}  REVIEW {o.get('review')}  FAIL {o.get('fail')}  ERRORS {summary['metrics']['errors']}")
    print("Next: python -m eval.scorecard   (compares with the previous run)")


if __name__ == "__main__":
    main()
