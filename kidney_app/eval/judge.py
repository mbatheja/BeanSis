"""
LLM-as-judge. It grades narrow, checkable things, one at a time, against the evidence the agent
actually saw (tool outputs captured in debug mode). It does NOT give holistic 1-10 scores.

Calibrate before trusting it: eval/calibrate.py compares judge labels with human labels.
Use a different model from the one being evaluated when you can (self-preference bias).
"""
import json
import os

from app import config

JUDGE_MODEL = os.getenv("JUDGE_MODEL", config.MODEL_MAIN)
EVIDENCE_CHARS = 40000

PROMPT = """You are grading one answer from a patient-education app for women with kidney disease.
Grade strictly against the EVIDENCE (the tool outputs the app retrieved), not your own medical knowledge,
except for MUST_NOT checks, where you may use general knowledge to recognize a violation.

QUESTION: {question}
USER PROFILE: {profile}

ANSWER:
<<<
{answer}
>>>

EVIDENCE (tool outputs, JSON, may be truncated):
<<<
{evidence}
>>>

MUST_INCLUDE points:
{must_include}

MUST_NOT rules:
{must_not}

EVIDENCE_GAP_EXPECTED: {evidence_gap}

Return ONLY a JSON object:
{{
  "claims": [  // every sentence in the ANSWER that has a bracketed citation like [PMID:...]
    {{"sentence": "...", "cited": ["PMID:..."],
      "support": "supported" | "partial" | "unsupported",
      "reason": "<=20 words"}}
  ],
  "key_points": [ {{"point": "<copy of each MUST_INCLUDE item>", "present": true|false}} ],
  "violations": [ {{"rule": "<copy of violated MUST_NOT item>", "quote": "<offending text>"}} ],
  "evidence_gap_acknowledged": true|false|null,   // null if EVIDENCE_GAP_EXPECTED is false
  "uses_profile_appropriately": true|false|null   // null if the profile is irrelevant to the question
}}
Definitions: "supported" = the cited evidence states it; "partial" = the evidence supports part of it or
it overstates; "unsupported" = the cited evidence doesn't say it (or the cited ID isn't in the evidence)."""


def _evidence(calls):
    parts = [f"## {c['tool']} {json.dumps(c.get('input'))}\n{c.get('output', '(no output captured)')}"
             for c in calls]
    return "\n\n".join(parts)[:EVIDENCE_CHARS] or "(no tools were called)"


def _bullets(items):
    return "\n".join(f"- {i}" for i in items) if items else "(none)"


def parse_json(raw):
    start, end = raw.find("{"), raw.rfind("}")
    return json.loads(raw[start:end + 1])


def judge_case(llm, case, profile, output):
    prompt = PROMPT.format(
        question=case["question"], profile=json.dumps(profile), answer=output["answer"],
        evidence=_evidence(output.get("tool_calls", [])),
        must_include=_bullets(case.get("must_include")), must_not=_bullets(case.get("must_not")),
        evidence_gap=bool(case.get("evidence_gap")))
    if config.JUDGE_PROVIDER:   # grade with a different provider than the one under test
        from app.llm import get_client
        llm = get_client(config.JUDGE_PROVIDER)
    resp = llm.messages.create(model=JUDGE_MODEL, max_tokens=3000,
                               messages=[{"role": "user", "content": prompt}])
    raw = "".join(b.text for b in resp.content if b.type == "text")
    try:
        j = parse_json(raw)
    except Exception as e:
        return {"error": f"judge parse failed: {e}", "raw": raw[:500]}
    return j


def summarize_judgment(j):
    """Turn a judge result into numbers."""
    if not j or "error" in j:
        return {}
    claims = j.get("claims") or []
    w = {"supported": 1.0, "partial": 0.5, "unsupported": 0.0}
    kp = j.get("key_points") or []
    return {
        "citation_precision": (sum(w.get(c.get("support"), 0) for c in claims) / len(claims)) if claims else None,
        "unsupported_claims": sum(1 for c in claims if c.get("support") == "unsupported"),
        "key_point_recall": (sum(1 for k in kp if k.get("present")) / len(kp)) if kp else None,
        "violations": len(j.get("violations") or []),
        "evidence_gap_acknowledged": j.get("evidence_gap_acknowledged"),
        "uses_profile_appropriately": j.get("uses_profile_appropriately"),
    }
