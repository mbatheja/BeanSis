"""Tools backed by the local reference DB, the user's own memory, and the research sub-agent."""
from .. import config, refdb
from .external import pubmed_search


def _doc_sources(rows):
    return [{"source_id": f"DOC:{r['doc_id']}", "title": r["title"], "url": r.get("url"),
             "collection": r.get("collection"), "pub_type": r.get("pub_type")} for r in rows]


def search_documents(args, ctx):
    rows = refdb.search_documents(args["query"], collection=args.get("collection"),
                                  min_year=args.get("min_year"), limit=args.get("limit", 6))
    for r in rows:
        r["cite_as"] = f"DOC:{r['doc_id']}"
    return {"results": rows, "sources": _doc_sources(rows)}


def search_trials(args, ctx):
    from ingest.clinicaltrials import search_trials as _search
    zip_code = args.get("zip") or ctx["profile"].get("zip")
    point = refdb.zip_to_point(zip_code) if zip_code else None
    rows = _search(args["query"], lat=point[0] if point else None, lon=point[1] if point else None,
                   radius_miles=args.get("radius_miles", 100),
                   hide_pregnancy_excluding=args.get("hide_pregnancy_excluding", False),
                   limit=args.get("limit", 8), con=refdb.cursor())
    return {"results": rows,
            "sources": [{"source_id": r["nct_id"], "title": r["title"], "url": r["url"]} for r in rows]}


def list_plans(args, ctx):
    rows = refdb.list_plans(state=args.get("state") or ctx["profile"].get("state"))
    return {"results": rows, "sources": []}


def find_providers(args, ctx):
    prof = ctx["profile"]
    zip_code = args.get("zip") or prof.get("zip")
    plan_id = args.get("plan_id") or prof.get("plan_id")
    plan = refdb.plan_by_id(plan_id) if plan_id else None
    verify_url = plan.get("find_doctor_url") if plan else None
    if not zip_code:
        return {"results": {"error": "Need a ZIP code. Ask the user for it.", "providers": [],
                            "plan": plan, "verify_url": verify_url},
                "sources": _plan_source(plan)}
    rows = refdb.find_providers(args["specialty"], zip_code, plan_id=plan_id,
                                radius_miles=args.get("radius_miles", 25), limit=args.get("limit", 10))
    if isinstance(rows, dict):
        ctx["provider_search"] = {"provider_count": 0, "plan": plan,
                                  "verify_url": verify_url, "zip": zip_code,
                                  "specialty": args["specialty"],
                                  "directory_loaded": rows.get("directory_loaded", True)}
        return {"results": {**rows, "plan": plan, "verify_url": verify_url},
                "sources": _plan_source(plan)}
    ctx["provider_search"] = {"provider_count": len(rows), "plan": plan,
                              "verify_url": verify_url, "zip": zip_code,
                              "specialty": args["specialty"]}
    return {"results": {
                "providers": rows,
                "plan": plan,
                # Guardrail: directories go stale. Always surface the date and the insurer's live tool.
                "verify_note": ("Network status comes from the insurer directory dated in each row. "
                                "Confirm with the insurer and the office before booking."),
                "verify_url": verify_url,
                "directory_loaded": refdb.table_exists("provider_network"),
                "fallback_message": ("No matching providers were found in the loaded directory. Use the insurer's "
                                     "official provider search to check current availability." if not rows and verify_url else None)},
            "sources": [{"source_id": f"NPI:{r['npi']}", "title": r["name"],
                         "url": f"https://npiregistry.cms.hhs.gov/provider-view/{r['npi']}"} for r in rows]
                       + _plan_source(plan)}


def _plan_source(plan):
    if not plan or not plan.get("find_doctor_url"):
        return []
    return [{"source_id": f"PLAN:{plan['plan_id']}",
             "title": f"{plan.get('insurer', 'Insurer')} provider search",
             "url": plan["find_doctor_url"]}]


def check_my_interactions(args, ctx):
    from ..you import meds
    res = meds.check_interactions(ctx["store"], ctx["user_id"], extra=args.get("drug"))
    srcs = res.pop("sources")
    return {"results": res, "sources": srcs}


def my_nutrition(args, ctx):
    from datetime import datetime, timedelta
    from ..you import meds, nutrition
    today = datetime.now(meds._tz(ctx["store"], ctx["user_id"])).date()
    days = max(1, min(int(args.get("days", 7)), 30))
    res = nutrition.day_summary(ctx["store"], ctx["user_id"], (today - timedelta(days=days - 1)).isoformat(),
                                today.isoformat())
    for d in res["days"]:
        d.pop("meals", None)                       # totals are enough for the model; keeps context small
    return {"results": res, "sources": []}


def recall_memory(args, ctx):
    """Search this user's saved items, previously found sources, and past questions across all tabs."""
    rows = ctx["store"].recall(ctx["user_id"], args["query"], limit=args.get("limit", 6))
    return {"results": rows, "sources": []}   # already-known sources; not new evidence


def deep_research(args, ctx):
    """
    Shared research sub-agent, callable from any tab. Pulls live PubMed abstracts plus the
    curated local corpus, then has the main model write a cited synthesis that names evidence gaps.
    """
    q = args["question"]
    pm = pubmed_search(args.get("pubmed_query") or q, max_results=args.get("max_papers", 6),
                       min_year=args.get("min_year"))
    local = refdb.search_documents(q, collection="research", limit=4)
    evidence = []
    for a in pm["results"]:
        evidence.append(f"[PMID:{a['pmid']}] {a['title']} ({a['year']}; {', '.join(a['pub_types'][:2])})\n"
                        f"{a['abstract']}")
    for d in local:
        evidence.append(f"[DOC:{d['doc_id']}] {d['title']} ({d.get('year')}; {d.get('pub_type')})\n{d['text']}")
    if not evidence:
        return {"results": {"synthesis": "No relevant literature found.", "evidence_gap": True}, "sources": []}

    prompt = (
        f"Question: {q}\n\nEvidence:\n\n" + "\n\n".join(evidence) +
        "\n\nWrite a synthesis of at most 250 words for a patient-education app. Rules: cite every claim "
        "with the bracketed IDs above, e.g. [PMID:123]; say what kind of evidence each is (guideline, trial, "
        "observational, case report, review); say clearly where evidence on women, pregnancy, or "
        "postpartum is thin or missing; never give dosing or treatment instructions.")
    resp = ctx["llm"].messages.create(model=config.MODEL_MAIN, max_tokens=700,
                                      messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    return {"results": {"synthesis": text, "papers": len(pm["results"]), "local_docs": len(local)},
            "sources": pm["sources"] + _doc_sources(local)}
