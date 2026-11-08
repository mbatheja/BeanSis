"""
drug_signal_profile: one side-by-side view of a drug's side-effect signals across sources.

For each theme (tremor, hair loss, swelling...):
    patient reviews   how many reviewers mention it              (DrugRev, filtered to her condition)
    FAERS             reports for women listing a matching term  (FDA adverse event reports)
    FDA label         whether the label's adverse reactions / warnings mention it, with the sentence
Then each theme is classified, in code:
    on_label_and_reported   label lists it and patients or FAERS report it   -> best-supported
    reported_not_on_label   patients/FAERS report it, the label doesn't       -> evidence gap; PubMed checked
    label_only              label lists it, nobody here mentions it            -> shown last
Matching uses fixed vocabularies (review phrasing + MedDRA/label terms), never the model, so the same
drug always gives the same table and the eval can score it.

None of these sources measures how often an effect happens in women like her. The table shows where
signals agree and where the evidence is thin; the label stays the authority.
"""
import re
from concurrent.futures import ThreadPoolExecutor

from .. import config
from ..cache import cached_get_json
from . import external, reviews

# Clinical vocabulary per theme: matched against FAERS MedDRA terms and label text, and used for PubMed.
# Keys must match reviews.THEMES.
CLINICAL_TERMS = {
    "nausea or vomiting": ["nausea", "vomiting"],
    "diarrhea": ["diarrhoea", "diarrhea"],
    "constipation": ["constipation"],
    "headache": ["headache", "migraine"],
    "tiredness or fatigue": ["fatigue", "asthenia", "lethargy"],
    "dizziness": ["dizziness", "vertigo"],
    "tremor or shaking": ["tremor"],
    "hair loss": ["alopecia", "hair loss"],
    "swelling": ["oedema", "edema", "swelling"],
    "weight gain": ["weight increased", "weight gain"],
    "sleep problems": ["insomnia", "sleep disorder"],
    "mood changes": ["depression", "anxiety", "mood", "irritability"],
    "cough": ["cough"],
    "rash or itching": ["rash", "pruritus", "urticaria"],
    "muscle or joint pain": ["myalgia", "arthralgia", "muscle spasms", "muscle cramp"],
    "infections": ["infection", "candida"],
    "blood sugar changes": ["hyperglycaemia", "hyperglycemia", "diabetes mellitus", "blood glucose"],
    "blood pressure too low": ["hypotension"],
    "heart racing or palpitations": ["palpitations", "tachycardia"],
    "vision changes": ["vision blurred", "visual impairment", "blurred vision"],
    "kidney function concerns": ["nephrotoxicity", "renal impairment", "acute kidney injury", "renal failure",
                                 "blood creatinine increased"],
}
_CLIN_RE = {k: re.compile("|".join(re.escape(t) for t in v), re.IGNORECASE) for k, v in CLINICAL_TERMS.items()}
LABEL_SECTIONS = ["boxed_warning", "warnings_and_cautions", "warnings", "adverse_reactions", "precautions"]
MIN_REVIEWS_FOR_GAP = 2       # patient-only signal needs at least this many reviewers to count
MIN_FAERS_FOR_GAP = 20        # or this many FAERS reports among the top terms
MAX_RESEARCHED = 2            # PubMed lookups per call (latency budget)

NOTICE = ("Signals compared across patient reviews, FDA adverse event reports, and the FDA label. None of these "
          "shows how often an effect happens; 'not on label' means the evidence is thin, not that the effect is "
          "proven or ruled out.")


def _label_hits(ingredient):
    lab = external.drug_label(ingredient, sections=LABEL_SECTIONS, max_chars=60000)
    text = " ".join(lab["results"].get("sections", {}).values())
    hits = {}
    for theme, rx in _CLIN_RE.items():
        m = rx.search(text)
        if m:
            start = text.rfind(".", 0, m.start()) + 1
            end = text.find(".", m.end())
            hits[theme] = text[start: end + 1 if end != -1 else m.end() + 150].strip()[:300]
    return hits, (lab["sources"][0] if lab["sources"] else None), bool(text)


def _faers_by_theme(ingredient):
    res = external.faers_summary(ingredient, female_only=True, top_n=100)
    counts = {}
    for r in res["results"].get("top_reactions", []):
        for theme, rx in _CLIN_RE.items():
            if rx.search(r["reaction"]):
                counts.setdefault(theme, {"reports": 0, "terms": []})
                counts[theme]["reports"] += r["reports"]
                counts[theme]["terms"].append(r["reaction"].lower())
    return counts, res["results"].get("total_reports", 0), (res["sources"][0] if res["sources"] else None)


def _pubmed_check(ingredient, theme):
    """Fast, deterministic literature check for an unlabeled signal: hit count + top 3 papers."""
    terms = " OR ".join(f'"{t}"[tiab]' for t in CLINICAL_TERMS[theme])
    query = f'"{ingredient}"[tiab] AND ({terms})'
    count = int(cached_get_json(f"{external.EUTILS}/esearch.fcgi", external._ncbi_params(
        {"db": "pubmed", "term": query, "retmax": 0, "retmode": "json"}), ttl=config.TTL["pubmed"])
        .get("esearchresult", {}).get("count", 0))
    top = external.pubmed_search(query, max_results=3) if count else {"results": [], "sources": []}
    return {"theme": theme, "pubmed_query": query, "pubmed_hits": count,
            "top_papers": [{"pmid": a["pmid"], "title": a["title"], "year": a["year"],
                            "cite_as": f"PMID:{a['pmid']}"} for a in top["results"]]}, top["sources"]


def build_profile(ingredient, review_rows, condition=None, research=True):
    sel = [r for r in reviews.dedupe(review_rows)
           if not condition or condition.lower() in r["condition"].lower()]
    n_rev = len(sel)
    rev_counts = {t: sum(1 for r in sel if rx.search(r["review"])) for t, rx in reviews._THEME_RE.items()}
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_label, f_faers = pool.submit(_label_hits, ingredient), pool.submit(_faers_by_theme, ingredient)
        label_hits, label_src, label_found = f_label.result()
        faers, faers_total, faers_src = f_faers.result()

    rows = []
    for theme in CLINICAL_TERMS:
        rv, fa = rev_counts.get(theme, 0), faers.get(theme, {}).get("reports", 0)
        on_label = theme in label_hits
        if not (rv or fa or on_label):
            continue
        reported = rv >= MIN_REVIEWS_FOR_GAP or fa >= MIN_FAERS_FOR_GAP
        status = ("on_label_and_reported" if on_label and (rv or fa) else
                  "reported_not_on_label" if (not on_label and reported and label_found) else
                  "label_only" if on_label else "weak_signal")
        rows.append({"theme": theme, "status": status,
                     "reviews_mentioning": rv, "reviews_in_group": n_rev,
                     "faers_reports_women": fa, "faers_terms": faers.get(theme, {}).get("terms", [])[:3],
                     "on_label": on_label, "label_sentence": label_hits.get(theme)})
    order = {"on_label_and_reported": 0, "reported_not_on_label": 1, "label_only": 2, "weak_signal": 3}
    rows.sort(key=lambda r: (order[r["status"]], -r["reviews_mentioning"], -r["faers_reports_women"]))

    sources = [s for s in (label_src, faers_src) if s]
    research_out = []
    gaps = [r["theme"] for r in rows if r["status"] == "reported_not_on_label"][:MAX_RESEARCHED]
    if research and gaps:
        with ThreadPoolExecutor(max_workers=MAX_RESEARCHED) as pool:
            for res, srcs in pool.map(lambda t: _pubmed_check(ingredient, t), gaps):
                research_out.append(res)
                sources += srcs
    return {
        "drug": ingredient, "condition_filter": condition,
        "sample": {"reviews_in_group": n_rev, "faers_reports_women_total": faers_total,
                   "label_found": label_found,
                   "faers_note": "FAERS isn't filtered by condition; it covers every use of the drug."},
        "signals": [r for r in rows if r["status"] != "weak_signal"][:12],
        "summary": {
            "on_label_and_reported": [r["theme"] for r in rows if r["status"] == "on_label_and_reported"],
            "reported_not_on_label": [r["theme"] for r in rows if r["status"] == "reported_not_on_label"],
            "label_only": [r["theme"] for r in rows if r["status"] == "label_only"],
        },
        "research_on_unlabeled": research_out,
        "cite": {"label": label_src["source_id"] if label_src else None,
                 "faers": faers_src["source_id"] if faers_src else None},
        "notice": NOTICE,
        "how_to_present": ("Lead with on_label_and_reported (cite the label). Then reported_not_on_label as 'some "
                           "patients report X; the label doesn't list it' with what PubMed shows (or that little was "
                           "found). Use counts like '5 of 37 reviewers', never percentages as frequency."),
    }, sources


def drug_signal_profile(args, ctx):
    names, ingredient = reviews.drug_names(args["drug"])
    ingredient = ingredient or args["drug"].lower()
    try:
        rows = reviews.reviews_for(names)
    except Exception:
        rows = []          # reviews table not built yet: compare the FDA label and FAERS only
    profile, sources = build_profile(ingredient, rows, args.get("condition"), args.get("research", True))
    if rows:
        sources.append({"source_id": f"REVIEWS:{ingredient.replace(' ', '-')}",
                        "title": f"Drugs.com patient reviews: {ingredient} (DrugRev dataset)",
                        "url": reviews.SOURCE_URL})
        profile["cite"]["reviews"] = sources[-1]["source_id"]
    return {"results": profile, "sources": sources}
