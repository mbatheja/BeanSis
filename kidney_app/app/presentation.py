"""
Turns an agent answer into what the UI shows:
  - answer_display: inline IDs like [PMID:123] replaced by numbered markers [1]
  - citations: numbered reference list with title, link, and a plain-language source type
  - notices: caveats added deterministically based on which tools ran (not left to the model)
  - guardrails: which checks ran on this answer and what each one did
"""
import re

SOURCE_KINDS = {  # ID prefix -> label shown to users
    "PMID": "Research article (PubMed)",
    "NCT": "Clinical trial registry",
    "LABEL": "FDA drug label",
    "FAERS": "FDA adverse event reports",
    "NPI": "National provider registry",
    "PLAN": "Insurer provider search",
    "RXCUI": "Drug name lookup (RxNorm)",
    "FDC": "USDA FoodData Central",
    "REVIEWS": "Patient reviews (Drugs.com via DrugRev dataset, CC BY 4.0)",
}
DOC_KINDS = {"research": "Research / guideline", "coverage": "Insurance plan document",
             "resource": "Support resource"}

TOOL_NOTICES = {
    "faers_summary": ("FDA adverse event counts come from voluntary reports. They don't show how often a side "
                      "effect happens or prove the drug caused it."),
    "find_providers": ("Network status comes from insurer directories, which can be out of date. Confirm with "
                       "your insurer and the doctor's office before booking."),
    "search_trials": ("The 'excludes pregnancy' label is automated and approximate. Check each study's full "
                      "eligibility criteria on ClinicalTrials.gov."),
    "deep_research": "Research summaries can miss newer studies. Bring questions to your care team.",
    "check_my_interactions": ("This check only finds interactions that FDA labels name directly. No flag does "
                              "not mean no interaction. Review your full list with your pharmacist."),
    "drug_signal_profile": ("Signals are compared across patient reviews, FDA adverse event reports, and the FDA "
                            "label. None of these shows how often an effect happens; 'not on the label' means the "
                            "evidence is thin, not that the effect is proven or ruled out."),
    "patient_reviews": ("Patient reviews are personal experiences from self-selected Drugs.com reviewers (2008-2023). "
                        "They can't show how common a side effect is, and ratings aren't evidence a drug works."),
    "my_nutrition": ("Nutrient amounts are estimates from typical USDA values and guessed portions. Your "
                     "dietitian can tell you which numbers matter most for you."),
}
EDUCATION_NOTICE = "This is general health education, not medical advice for your situation."
DOSING_NOTICE = "This app can't recommend personal doses. Ask your doctor or pharmacist about dosing."

DOSING = [
    re.compile(r"\b(take|use|increase|decrease|start|try)\b[^.\n]{0,40}?\b\d+(\.\d+)?\s?(mg|mcg|µg|units?|tablets?|pills?)\b",
               re.IGNORECASE),
    re.compile(r"\b\d+(\.\d+)?\s?(mg|mcg|µg)\b[^.\n]{0,30}?\b(once|twice|daily|every|per day|times a day)\b",
               re.IGNORECASE),
]
BRACKET = re.compile(r"\[([^\[\]]{3,200})\]")


def source_kind(source):
    sid = source["source_id"]
    if sid.startswith("DOC:"):
        base = DOC_KINDS.get(source.get("collection"), "Library document")
        return f"{base} ({source['pub_type']})" if source.get("pub_type") else base
    if sid.startswith("NCT"):
        return SOURCE_KINDS["NCT"]
    return SOURCE_KINDS.get(sid.split(":", 1)[0], "Source")


def number_citations(text, sources):
    """Replace verified inline IDs with [1], [2]... in order of first appearance."""
    by_id = {s["source_id"]: s for s in sources}
    order = []

    def _sub(m):
        ids = [t.strip() for t in re.split(r"[;,]", m.group(1))]
        if not ids or not all(i in by_id for i in ids):
            return m.group(0)                     # not a (verified) citation; leave as written
        nums = []
        for i in ids:
            if i not in order:
                order.append(i)
            nums.append(order.index(i) + 1)
        return "[" + ", ".join(str(n) for n in nums) + "]"

    display = BRACKET.sub(_sub, text)
    citations = [{"n": k + 1, "source_id": sid, "title": by_id[sid].get("title"), "url": by_id[sid].get("url"),
                  "kind": source_kind(by_id[sid])} for k, sid in enumerate(order)]
    return display, citations


def dosing_language(text):
    return [m.group(0) for p in DOSING for m in p.finditer(text)]


def build(tab, answer, grounding_report, sources, tool_calls, clinical_tabs):
    display, citations = number_citations(answer, sources)
    used_tools = []
    for c in tool_calls:
        if c["tool"] not in used_tools:
            used_tools.append(c["tool"])

    notices = list(grounding_report.get("notices", []))
    notices += [TOOL_NOTICES[t] for t in used_tools if t in TOOL_NOTICES]
    dosing = dosing_language(answer)
    if dosing:
        notices.append(DOSING_NOTICE)
    if tab in clinical_tabs:
        notices.append(EDUCATION_NOTICE)

    removed = grounding_report.get("removed", [])
    cited = grounding_report.get("cited", [])
    if removed:
        cite_status, cite_detail = "modified", f"Removed {len(removed)} reference(s) that couldn't be verified."
    elif cited:
        cite_status, cite_detail = "passed", f"All {len(set(cited))} reference(s) matched sources retrieved for this answer."
    elif tab in clinical_tabs:
        cite_status, cite_detail = "warning", "No retrieved source backs this answer."
    else:
        cite_status, cite_detail = "not_applicable", "No health claims needing citations."

    guardrails = [
        {"name": "safety_check", "status": "passed",
         "detail": "Checked for emergency or crisis language before answering."},
        {"name": "citation_check", "status": cite_status, "detail": cite_detail},
        {"name": "dosing_check", "status": "flagged" if dosing else "passed",
         "detail": "Possible dosing language found; flagged for review." if dosing
         else "No personal dosing instructions detected."},
        {"name": "tool_caveats", "status": "applied" if any(t in TOOL_NOTICES for t in used_tools) else "not_applicable",
         "detail": ", ".join(t for t in used_tools if t in TOOL_NOTICES) or "None needed."},
    ]
    return {"answer_display": display, "citations": citations, "notices": notices, "guardrails": guardrails}


def revision_guardrail(revision):
    if not revision["attempted"]:
        return {"name": "self_correction", "status": "not_needed", "detail": "Citations passed on the first draft."}
    why = ("cited sources that weren't retrieved" if revision["reason"] == "unverified_citations"
           else "had no citations")
    if revision["accepted"]:
        return {"name": "self_correction", "status": "revised",
                "detail": f"The first draft {why}; it was rewritten using retrieved sources."}
    return {"name": "self_correction", "status": "kept_original",
            "detail": f"The first draft {why}; a rewrite didn't improve it, so unverified parts were removed instead."}


def urgent(message):
    return {"answer_display": message, "citations": [], "notices": [],
            "guardrails": [{"name": "safety_check", "status": "triggered",
                            "detail": "Emergency or crisis language detected; showing urgent-care guidance "
                                      "instead of an AI answer."}]}
