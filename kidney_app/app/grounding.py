"""
Post-generation citation check (cheap, deterministic).

- Every bracketed ID the model cites must appear in a tool result from this turn.
  Unverifiable IDs (hallucinated or remembered from training) are removed and flagged.
- Clinical tabs that answer without any tool evidence get a visible "not grounded" notice.
A stronger next step: an entailment check that each sentence is supported by its cited chunk.
"""
import re

ID_TOKEN = re.compile(r"^(PMID:\d+|DOC:[\w.\-:/]+|NCT\d{8}|LABEL:[\w\-]+|FAERS:[\w\-:. ]+|NPI:\d{10}|RXCUI:\d+|FDC:\d+|REVIEWS:[\w\-]+)$")
BRACKET = re.compile(r"\[([^\[\]]{3,200})\]")

CLINICAL_TABS = {"symptoms", "medications"}

NO_EVIDENCE_NOTICE = ("This answer isn't backed by a source the app retrieved, so treat it as general "
                      "information and check with your care team.")
REMOVED_NOTICE = "Some references couldn't be verified and were removed."


def check(text, sources, tab):
    known = {s["source_id"] for s in sources}
    cited, removed = [], []

    def _fix(m):
        tokens = [t.strip() for t in re.split(r"[;,]", m.group(1))]
        if not tokens or not all(ID_TOKEN.match(t) for t in tokens):
            return m.group(0)                    # ordinary brackets, not a citation
        keep = [t for t in tokens if t in known]
        removed.extend(t for t in tokens if t not in known)
        cited.extend(keep)
        return f"[{', '.join(keep)}]" if keep else ""

    out = BRACKET.sub(_fix, text)
    notices = []                     # shown by the UI next to the answer (see app/presentation.py)
    if removed:
        notices.append(REMOVED_NOTICE)
    if tab in CLINICAL_TABS and not cited:
        notices.append(NO_EVIDENCE_NOTICE)
    used = [s for s in sources if s["source_id"] in set(cited)]
    return out, {"cited": sorted(set(cited)), "removed": removed, "used_sources": used, "notices": notices}
