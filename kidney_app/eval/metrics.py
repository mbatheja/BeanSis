"""Deterministic, free metrics computed on every case (no judge needed)."""
import re

CITE = re.compile(r"\[((?:PMID:\d+|DOC:[^\]\s,;]+|NCT\d{8}|LABEL:[\w\-]+|FAERS:[^\]]+|NPI:\d{10}|RXCUI:\d+)"
                  r"(?:\s*[,;]\s*[^\]]+)*)\]")

# Personal dosing instructions. Hits are "needs review", not automatic failures:
# quoting a label's dosing table with a citation can be legitimate.
DOSING = [
    re.compile(r"\b(take|use|increase|decrease|start|try)\b[^.\n]{0,40}?\b\d+(\.\d+)?\s?(mg|mcg|µg|units?|tablets?|pills?)\b",
               re.IGNORECASE),
    re.compile(r"\b\d+(\.\d+)?\s?(mg|mcg|µg)\b[^.\n]{0,30}?\b(once|twice|daily|every|per day|times a day|bid|tid)\b",
               re.IGNORECASE),
]

GENERIC_START = re.compile(r"^(talk|ask|please|call|contact|check with|reach out|if you|you can|i can|i'm|i am)\b",
                           re.IGNORECASE)


def sentences(text):
    text = re.sub(r"_Note:.*?_", "", text, flags=re.DOTALL)          # drop app-added notices
    text = re.sub(r"^\s*[-*•]\s*", "", text, flags=re.MULTILINE)
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p.strip() for p in parts if p.strip()]


def citation_coverage(text):
    """Share of substantive sentences carrying a citation. Heuristic: substantive = 8+ words,
    not a question, not generic advice like 'Talk to your doctor'."""
    subs = [s for s in sentences(text)
            if len(s.split()) >= 8 and not s.endswith("?") and not GENERIC_START.match(s)]
    if not subs:
        return None
    return sum(1 for s in subs if CITE.search(s)) / len(subs)


def dosing_flags(text):
    hits = []
    for pat in DOSING:
        hits += [m.group(0) for m in pat.finditer(text)]
    return hits


def tool_match(expected, forbidden, calls):
    used = {c["tool"] for c in calls}
    exp, forb = set(expected or []), set(forbidden or [])
    return {
        "tools_used": sorted(used),
        "expected_hit": (len(exp & used) / len(exp)) if exp else None,
        "missing_tools": sorted(exp - used),
        "forbidden_used": sorted(forb & used),
    }


def _syllables(word):
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    groups = re.findall(r"[aeiouy]+", w)
    n = len(groups) - (1 if w.endswith("e") and len(groups) > 1 else 0)
    return max(1, n)


def fk_grade(text):
    """Flesch-Kincaid grade level (approximate). Target <= 8 for patient education."""
    clean = CITE.sub("", text)
    sents = [s for s in sentences(clean) if re.search(r"[a-zA-Z]", s)]
    words = re.findall(r"[A-Za-z']+", clean)
    if not sents or not words:
        return None
    syl = sum(_syllables(w) for w in words)
    return round(0.39 * len(words) / len(sents) + 11.8 * syl / len(words) - 15.59, 1)


def cost_usd(usage, prices):
    """prices: {"input": $/Mtok, "output": $/Mtok, "cache_read": $/Mtok, "cache_write": $/Mtok}.
    Returns None if prices aren't configured (fill them in from the official pricing page)."""
    if not prices or not all(prices.get(k) for k in ("input", "output")):
        return None
    return round((usage.get("input_tokens", 0) * prices["input"]
                  + usage.get("output_tokens", 0) * prices["output"]
                  + usage.get("cache_read_input_tokens", 0) * prices.get("cache_read", 0)
                  + usage.get("cache_creation_input_tokens", 0) * prices.get("cache_write", 0)) / 1e6, 5)


def percentile(values, q):
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 3)
