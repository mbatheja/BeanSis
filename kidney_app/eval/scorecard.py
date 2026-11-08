"""
Markdown scorecard for the latest run (vs the previous one), plus component results.

    python -m eval.scorecard                       # latest vs previous
    python -m eval.scorecard --run <dir> --vs <dir>
Writes scorecard.md into the run directory (paste it into your demo slides).
"""
import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

# (metric key, label, higher_is_better)
HEADLINE = [
    ("fail", "Hard failures", False),
    ("violations", "Must-not violations (judge)", False),
    ("citation_precision", "Citation precision (judge)", True),
    ("key_point_recall", "Key-point recall (judge)", True),
    ("evidence_gap_ack_rate", "Evidence-gap acknowledged", True),
    ("expected_tool_hit", "Expected tools called", True),
    ("citation_coverage", "Citation coverage (heuristic)", True),
    ("answers_with_removed_citations", "Answers with removed citations", False),
    ("answers_with_dosing_flags", "Answers with dosing flags", False),
    ("revision_rate", "Drafts sent back for revision", False),
    ("revisions_accepted", "Revisions accepted", True),
    ("revisions_rejected", "Revisions rejected (original kept)", False),
    ("fk_grade_median", "Reading grade (median)", False),
    ("latency_p50", "Latency p50 (s)", False),
    ("latency_p95", "Latency p95 (s)", False),
    ("mean_cache_read_tokens", "Cache-read tokens / answer", True),
    ("mean_cost_usd", "Cost / answer (USD)", False),
]


def runs():
    return sorted(p for p in RESULTS.iterdir() if (p / "summary.json").exists()) if RESULTS.exists() else []


def fmt(v):
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:.3f}" if abs(v) < 10 else f"{v:.1f}"
    return str(v)


def delta(new, old, higher_better):
    if not isinstance(new, (int, float)) or not isinstance(old, (int, float)) or new == old:
        return ""
    better = (new > old) == higher_better
    return f" ({'▲' if new > old else '▼'} {'better' if better else 'worse'})"


def latest_components():
    path = RESULTS / "components.jsonl"
    if not path.exists():
        return {}
    latest = {}
    for line in open(path):
        r = json.loads(line)
        latest[r["component"]] = r
    return latest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run")
    ap.add_argument("--vs")
    a = ap.parse_args()
    all_runs = runs()
    if not all_runs:
        raise SystemExit("No runs yet. python -m eval.run first.")
    run = Path(a.run) if a.run else all_runs[-1]
    prev = Path(a.vs) if a.vs else (all_runs[-2] if len(all_runs) > 1 and not a.run else None)
    cur = json.loads((run / "summary.json").read_text())
    old = json.loads((prev / "summary.json").read_text()) if prev else None
    o, po = cur["metrics"]["overall"], (old["metrics"]["overall"] if old else {})

    lines = [f"# Eval scorecard: {cur['tag']} ({cur['when']})",
             f"prompts `{cur['prompt_version']}` · model `{cur['model_main']}` · judge {'on' if cur['judge'] else 'off'}"
             + (f" · compared with `{old['tag']}` ({old['when']})" if old else ""), "",
             f"**{o.get('n', 0)} answers: {o.get('pass', 0)} pass · {o.get('review', 0)} review · "
             f"{o.get('fail', 0)} fail · {cur['metrics'].get('errors', 0)} errors**", "",
             "| Metric | This run | Previous |", "|---|---|---|"]
    for key, label, hib in HEADLINE:
        if key in o or key in po:
            lines.append(f"| {label} | {fmt(o.get(key))}{delta(o.get(key), po.get(key), hib)} | {fmt(po.get(key))} |")

    lines += ["", "## By tab", "", "| Tab | n | pass | review | fail | tools hit | cit. precision | key points |",
              "|---|---|---|---|---|---|---|---|"]
    for k, b in cur["metrics"].items():
        if k.startswith("tab:"):
            lines.append(f"| {k[4:]} | {b['n']} | {b['pass']} | {b['review']} | {b['fail']} | "
                         f"{fmt(b.get('expected_tool_hit'))} | {fmt(b.get('citation_precision'))} | "
                         f"{fmt(b.get('key_point_recall'))} |")

    comps = latest_components()
    if comps:
        lines += ["", "## Components (latest)", ""]
        for name, r in comps.items():
            nums = {k: v for k, v in r.items() if isinstance(v, (int, float)) and k != "n"}
            extra = r.get("skipped") or r.get("error") or ", ".join(f"{k}={fmt(v)}" for k, v in nums.items())
            lines.append(f"- **{name}** (n={r.get('n', '–')}): {extra}")

    lines += ["", "## Needs attention", ""]
    from eval.run import flat
    for r in flat([json.loads(l) for l in open(run / "results.jsonl")]):
        if r.get("status") in ("FAIL", "REVIEW", "ERROR"):
            why = []
            for v in (r.get("judge") or {}).get("violations") or []:
                why.append(f"violation: {v.get('rule')}")
            if r.get("forbidden_used"):
                why.append(f"forbidden tools {r['forbidden_used']}")
            if r.get("missing_tools"):
                why.append(f"missing tools {r['missing_tools']}")
            if r.get("dosing_flags"):
                why.append(f"dosing text {r['dosing_flags'][:2]}")
            if r.get("removed_citations"):
                why.append(f"removed citations {r['removed_citations']}")
            if r.get("unsupported_claims"):
                why.append(f"{r['unsupported_claims']} unsupported claims")
            if r.get("error"):
                why.append(r["error"])
            lines.append(f"- `{r['status']}` **{r['id']}**: {'; '.join(why) or 'see results.jsonl'}")

    md = "\n".join(lines)
    (run / "scorecard.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
