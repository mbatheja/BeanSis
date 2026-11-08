"""
Check the judge against people before trusting its numbers.

1. Export a labeling sheet from a judged run (judge labels hidden in a separate column):
       python -m eval.calibrate export eval/results/<run_dir> --n 40
2. Two teammates independently fill `human_label` in their own copy of calibration.csv
   (claims: supported | partial | unsupported; key points: true | false).
3. Score agreement:
       python -m eval.calibrate score eval/results/<run_dir>/calibration_alex.csv [second_labeler.csv]

Rough guide for Cohen's kappa: < 0.4 poor, 0.4-0.6 moderate, 0.6-0.8 good, > 0.8 excellent.
If judge-vs-human kappa is well below human-vs-human kappa, fix the judge prompt before using its scores.
"""
import argparse
import csv
import json
import random
from collections import Counter
from pathlib import Path

from eval.run import flat


def excerpt(calls, source_id, width=700):
    for c in calls:
        out = c.get("output") or ""
        key = source_id.split(":", 1)[-1]
        i = out.find(key)
        if i != -1:
            return out[max(0, i - 100): i + width]
    return "(cited ID not found in tool outputs)"


def export(run_dir, n, seed):
    rows = []
    for r in flat([json.loads(l) for l in open(Path(run_dir) / "results.jsonl")]):
        j = r.get("judge") or {}
        for c in j.get("claims") or []:
            cited = c.get("cited") or []
            rows.append({"kind": "claim", "case_id": r["id"], "text": c.get("sentence"),
                         "cited": "; ".join(cited),
                         "evidence_excerpt": "\n---\n".join(excerpt(r.get("tool_calls", []), s) for s in cited),
                         "judge_label": c.get("support"), "human_label": ""})
        for k in j.get("key_points") or []:
            rows.append({"kind": "key_point", "case_id": r["id"], "text": k.get("point"), "cited": "",
                         "evidence_excerpt": (r.get("answer") or "")[:1500],
                         "judge_label": str(bool(k.get("present"))).lower(), "human_label": ""})
    if not rows:
        raise SystemExit("No judge output in this run. Re-run with --judge.")
    random.Random(seed).shuffle(rows)
    rows = rows[:n]
    out = Path(run_dir) / "calibration.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows -> {out}. Give each labeler a copy; hide the judge_label column while labeling.")


def kappa(a, b):
    pairs = [(x, y) for x, y in zip(a, b) if x and y]
    if not pairs:
        return None, 0
    n = len(pairs)
    po = sum(x == y for x, y in pairs) / n
    ca, cb = Counter(x for x, _ in pairs), Counter(y for _, y in pairs)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return (round((po - pe) / (1 - pe), 3) if pe < 1 else 1.0), n


def _norm(v):
    return (v or "").strip().lower()


def score(files):
    sheets = [list(csv.DictReader(open(f))) for f in files]
    for kind in ("claim", "key_point"):
        rows0 = [r for r in sheets[0] if r["kind"] == kind]
        judge = [_norm(r["judge_label"]) for r in rows0]
        human0 = [_norm(r["human_label"]) for r in rows0]
        k, n = kappa(judge, human0)
        agree = sum(a == b for a, b in zip(judge, human0) if b) / max(1, sum(bool(b) for b in human0))
        print(f"{kind}: judge vs {Path(files[0]).name}: kappa={k} agreement={agree:.2f} (n={n})")
        if len(sheets) > 1:
            by_key = {(r["case_id"], r["text"]): _norm(r["human_label"]) for r in sheets[1] if r["kind"] == kind}
            human1 = [by_key.get((r["case_id"], r["text"]), "") for r in rows0]
            k2, n2 = kappa(human0, human1)
            print(f"{kind}: human vs human: kappa={k2} (n={n2})  <- the ceiling the judge should approach")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("run_dir")
    e.add_argument("--n", type=int, default=40)
    e.add_argument("--seed", type=int, default=7)
    s = sub.add_parser("score")
    s.add_argument("files", nargs="+")
    a = ap.parse_args()
    export(a.run_dir, a.n, a.seed) if a.cmd == "export" else score(a.files)


if __name__ == "__main__":
    main()
