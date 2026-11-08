"""
Patient reviews from the DrugRev dataset (Drugs.com reviews 2008-2023, CC BY 4.0; see ingest/reviews.py).

What this tool returns for a drug:
  - review counts split BY CONDITION (one drug can mean very different patients: topical tacrolimus for
    eczema vs oral tacrolimus after transplant), so the agent can pick the relevant group
  - side-effect THEMES: how many reviews mention each, as a share of that group (deterministic regex)
  - reviews mentioning pregnancy or breastfeeding (thin but directly relevant here)
  - a few short excerpts (most-liked first), for the agent to paraphrase
What it must never be used for: how common a side effect is, or whether a drug works. Reviewers are
self-selected, unverified, and sex isn't recorded.
"""
import re
from collections import Counter

from .. import config
from ..cache import cached_get_json
from .external import RXNAV

SOURCE_URL = "https://doi.org/10.17632/64cc5w5dxy.1"
CAVEAT = ("Personal experiences from self-selected Drugs.com reviewers (2008-2023). They can't show how common "
          "a side effect is, ratings aren't evidence a drug works, and reviewers' sex isn't recorded.")
EXCERPT_CHARS = 220

THEMES = {
    "nausea or vomiting": r"nause|vomit|throw(ing)? up",
    "diarrhea": r"diarrh",
    "constipation": r"constipat",
    "headache": r"headache|migraine",
    "tiredness or fatigue": r"tired|fatigue|exhaust|lethargic|no energy",
    "dizziness": r"dizz|lightheaded|light-headed|vertigo",
    "tremor or shaking": r"tremor|shak(y|ing|es)",
    "hair loss": r"hair (loss|fall|thinning)|losing (my )?hair|hair (is )?falling",
    "swelling": r"swell|swollen|edema|oedema",
    "weight gain": r"weight gain|gained \d+|gain(ed)? weight",
    "sleep problems": r"insomnia|can'?t sleep|trouble sleeping|couldn'?t sleep",
    "mood changes": r"depress|anxi|mood|irritab|panic",
    "cough": r"\bcough",
    "rash or itching": r"\brash|itch|hives",
    "muscle or joint pain": r"muscle (pain|ache|cramp)|cramp|joint pain|aches",
    "infections": r"infection|uti\b|yeast",
    "blood sugar changes": r"blood sugar|glucose|a1c",
    "blood pressure too low": r"low blood pressure|bp (dropped|too low)|hypotension",
    "heart racing or palpitations": r"palpitat|heart rac|racing heart|pounding heart",
    "vision changes": r"blurr|vision",
    # specific problems only: transplant reviewers mention "my kidney transplant" constantly
    "kidney function concerns": (r"kidney (damage|function (dropped|went down|declin|worse)|problems?|failure|issues?)"
                                 r"|creatinine (went up|rose|increased|high|is up)|nephrotox|egfr (dropped|went down|declin)"),
}
_THEME_RE = {k: re.compile(v, re.IGNORECASE) for k, v in THEMES.items()}
_PREG_RE = re.compile(r"pregnan|breastfe|breast-fe|nursing|postpartum|trimester|my baby|"
                      r"(gave|given|giving) birth|conceiv|miscarr", re.IGNORECASE)


def drug_names(drug):
    """Ingredient + its brand names via RxNorm, so 'Prograf' and 'tacrolimus' reviews are found together."""
    names = {drug.strip().lower()}
    approx = cached_get_json(f"{RXNAV}/approximateTerm.json", {"term": drug, "maxEntries": 1}, ttl=config.TTL["rxnav"])
    cands = approx.get("approximateGroup", {}).get("candidate", [])
    if not cands:
        return sorted(names), None
    rel = cached_get_json(f"{RXNAV}/rxcui/{cands[0]['rxcui']}/related.json", {"tty": "IN"}, ttl=config.TTL["rxnav"])
    ins = [c for g in rel.get("relatedGroup", {}).get("conceptGroup", []) or []
           for c in g.get("conceptProperties", []) or []]
    ingredient = ins[0]["name"].lower() if ins else None
    for ing in ins:
        names.add(ing["name"].lower())
        bn = cached_get_json(f"{RXNAV}/rxcui/{ing['rxcui']}/related.json", {"tty": "BN"}, ttl=config.TTL["rxnav"])
        names |= {c["name"].lower() for g in bn.get("relatedGroup", {}).get("conceptGroup", []) or []
                  for c in g.get("conceptProperties", []) or []}
    return sorted(names), ingredient


def reviews_for(names, con=None):
    from .. import refdb
    cur = con or refdb.cursor()
    cur.execute(f"SELECT medicine_name, condition, review_date, rating, likes, review FROM reviews "
                f"WHERE name_key IN ({', '.join('?' for _ in names)})", list(names))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _excerpt(text):
    return text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS].rsplit(" ", 1)[0] + "…"


def dedupe(rows):
    """Drugs.com lists one review under brand and generic pages; searching both names double-counts it."""
    seen, out = set(), []
    for r in rows:
        key = re.sub(r"\W+", " ", r["review"].lower()).strip()[:300]
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def summarize(rows, condition=None, n_examples=3, min_theme_reviews=3):
    rows = dedupe(rows)
    by_cond = Counter(r["condition"] for r in rows)
    groups = [{"condition": c, "reviews": n,
               "avg_rating": round(sum(r["rating"] for r in rows if r["condition"] == c) / n, 1)}
              for c, n in by_cond.most_common(8)]
    sel = [r for r in rows if not condition or condition.lower() in r["condition"].lower()]
    out = {"total_reviews_all_conditions": len(rows), "by_condition": groups,
           "condition_filter": condition, "reviews_in_selection": len(sel)}
    if not sel:
        return out
    dates = sorted(r["review_date"] for r in sel if r["review_date"] and r["review_date"] != "NaT")
    themes = []
    for name, rx in _THEME_RE.items():
        n = sum(1 for r in sel if rx.search(r["review"]))
        if n >= min_theme_reviews:
            themes.append({"theme": name, "reviews_mentioning": n, "share_of_reviews": round(n / len(sel), 3)})
    themes.sort(key=lambda t: -t["reviews_mentioning"])
    top = sorted(sel, key=lambda r: (-r["likes"], -len(r["review"])))
    preg = [r for r in top if _PREG_RE.search(r["review"])]
    out.update({
        "date_range": [dates[0], dates[-1]] if dates else None,
        "themes": themes[:10],
        "mentions_pregnancy_or_breastfeeding": len(preg),
        "pregnancy_excerpts": [_excerpt(r["review"]) for r in preg[:2]],
        "excerpts": [{"condition": r["condition"], "rating": r["rating"], "text": _excerpt(r["review"])}
                     for r in top[:n_examples]],
        "caveat": CAVEAT,
        "how_to_use": "Paraphrase; don't quote at length. Theme shares describe these reviews only, not patients overall.",
    })
    return out


def patient_reviews(args, ctx):
    names, ingredient = drug_names(args["drug"])
    rows = reviews_for(names)
    res = summarize(rows, condition=args.get("condition"))
    res["drug"], res["names_searched"] = ingredient or args["drug"], names
    if not rows:
        return {"results": {**res, "note": "No reviews found for this drug in the dataset."}, "sources": []}
    sid = f"REVIEWS:{(ingredient or args['drug']).lower().replace(' ', '-')}"
    return {"results": {**res, "cite_as": sid},
            "sources": [{"source_id": sid, "title": f"Drugs.com patient reviews: {ingredient or args['drug']} "
                                                    f"(DrugRev dataset)", "url": SOURCE_URL}]}
