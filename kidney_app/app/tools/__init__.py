"""Tool registry: name -> (LLM tool schema, implementation(args, ctx))."""
from . import external, local, reviews, signals, transport


def _wrap(fn, *keys):
    """Adapt a plain function f(**kwargs) to the (args, ctx) tool signature."""
    return lambda args, ctx: fn(**{k: args[k] for k in keys if k in args})


TOOLS = {
    "search_documents": ({
        "name": "search_documents",
        "description": ("Keyword (BM25) search over the team's curated library. collection='research' for "
                        "papers and guidelines, 'coverage' for insurance benefit documents, 'resource' for "
                        "support programs, financial aid, and community groups. Fast; try this first."),
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string"},
            "collection": {"type": "string", "enum": ["research", "coverage", "resource"]},
            "min_year": {"type": "integer"},
            "limit": {"type": "integer", "default": 6}}, "required": ["query"]}},
        local.search_documents),

    "deep_research": ({
        "name": "deep_research",
        "description": ("Research sub-agent: searches live PubMed plus the curated library and returns a "
                        "cited synthesis that names evidence gaps. Slower (several seconds). Use when "
                        "search_documents finds too little, or the question needs recent studies."),
        "input_schema": {"type": "object", "properties": {
            "question": {"type": "string"},
            "pubmed_query": {"type": "string", "description": "Optional PubMed syntax, e.g. MeSH terms"},
            "min_year": {"type": "integer"},
            "max_papers": {"type": "integer", "default": 6}}, "required": ["question"]}},
        local.deep_research),

    "normalize_drug": ({
        "name": "normalize_drug",
        "description": "Map a brand or misspelled drug name to its generic ingredient(s) via RxNorm. "
                       "Call before FAERS or label lookups.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
        _wrap(external.normalize_drug, "name")),

    "faers_summary": ({
        "name": "faers_summary",
        "description": ("Most-reported side effects for a generic drug in FDA's adverse event reports, "
                        "filtered to women by default. consumer_reports_only=true limits to reports "
                        "filed by patients themselves. Counts are NOT rates and do NOT show causation."),
        "input_schema": {"type": "object", "properties": {
            "generic_name": {"type": "string"},
            "female_only": {"type": "boolean", "default": True},
            "consumer_reports_only": {"type": "boolean", "default": False}}, "required": ["generic_name"]}},
        _wrap(external.faers_summary, "generic_name", "female_only", "consumer_reports_only")),

    "drug_label": ({
        "name": "drug_label",
        "description": "Official FDA label sections for a generic drug: boxed warning, pregnancy, "
                       "lactation, use in specific populations (incl. renal), warnings, adverse reactions.",
        "input_schema": {"type": "object", "properties": {"generic_name": {"type": "string"}},
                         "required": ["generic_name"]}},
        _wrap(external.drug_label, "generic_name")),

    "search_trials": ({
        "name": "search_trials",
        "description": ("Recruiting clinical trials that enroll women, ranked by relevance, optionally "
                        "near the user's ZIP. Each result flags whether it excludes pregnancy/breastfeeding."),
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string"},
            "zip": {"type": "string"},
            "radius_miles": {"type": "integer", "default": 100},
            "hide_pregnancy_excluding": {"type": "boolean", "default": False}}, "required": ["query"]}},
        local.search_trials),

    "list_plans": ({
        "name": "list_plans",
        "description": "Insurance plans whose directories are loaded, with plan_id and the insurer's "
                       "official find-a-doctor link.",
        "input_schema": {"type": "object", "properties": {"state": {"type": "string"}}}},
        local.list_plans),

    "find_providers": ({
        "name": "find_providers",
        "description": ("Find nephrologists or OB/GYNs near a ZIP, optionally only those listed in a plan's "
                        "directory. Defaults to the user's saved ZIP and plan."),
        "input_schema": {"type": "object", "properties": {
            "specialty": {"type": "string", "enum": ["nephrology", "obgyn", "maternal_fetal_medicine"]},
            "zip": {"type": "string"},
            "plan_id": {"type": "string"},
            "radius_miles": {"type": "integer", "default": 25}}, "required": ["specialty"]}},
        local.find_providers),

    "ride_help": ({
        "name": "ride_help",
        "description": ("California Medi-Cal ride finder (NMT/NEMT). Pass every answer collected so far; it returns "
                        "either the NEXT question to ask her (with options) or a result card with who to call, a "
                        "call script, requirements, and fallbacks. Ask its questions one at a time, in its words."),
        "input_schema": {"type": "object", "properties": {
            "zip": {"type": "string"},
            "county": {"type": "string"},
            "coverage": {"type": "string", "enum": ["medi_cal", "medicare", "both", "neither"]},
            "plan": {"type": "string", "description": "plan name, 'fee_for_service', or 'unknown'"},
            "destination": {"type": "string", "enum": ["dialysis", "prenatal", "other_appointment", "pharmacy"]},
            "mobility": {"type": "string", "enum": ["car", "wheelchair", "stretcher"]}}}},
        transport.ride_help),

    "drug_signal_profile": ({
        "name": "drug_signal_profile",
        "description": ("Side-by-side side-effect signals for one drug: patient reviews (filtered to her condition), "
                        "FDA adverse event reports for women, and whether the FDA label lists each effect, with "
                        "the label sentence. Classifies each as on-label-and-reported, reported-but-not-on-label "
                        "(PubMed checked automatically), or label-only. Use FIRST for side-effect questions."),
        "input_schema": {"type": "object", "properties": {
            "drug": {"type": "string"},
            "condition": {"type": "string", "description": "Her use, e.g. 'transplant', 'lupus', 'blood pressure'"},
            "research": {"type": "boolean", "default": True,
                         "description": "Check PubMed for signals the label doesn't list"}},
            "required": ["drug"]}},
        signals.drug_signal_profile),

    "patient_reviews": ({
        "name": "patient_reviews",
        "description": ("What patients wrote about a drug on Drugs.com (DrugRev dataset, 2008-2023): review counts "
                        "by condition, side-effect themes with how many reviews mention each, mentions of "
                        "pregnancy/breastfeeding, and short excerpts. Personal experiences only: never use for how "
                        "common an effect is or whether a drug works. Pass condition to focus on one use."),
        "input_schema": {"type": "object", "properties": {
            "drug": {"type": "string"},
            "condition": {"type": "string", "description": "e.g. 'Organ Transplant', 'Lupus', 'High Blood Pressure'"}},
            "required": ["drug"]}},
        reviews.patient_reviews),

    "check_my_interactions": ({
        "name": "check_my_interactions",
        "description": ("Check the user's own saved medication list for interactions named in FDA labels, "
                        "optionally adding one more drug she is asking about. Returns quoted label sentences "
                        "with citations, plus FAERS 'reported together' counts (NOT evidence of interaction)."),
        "input_schema": {"type": "object", "properties": {
            "drug": {"type": "string", "description": "Optional extra drug to check against her list"}}}},
        local.check_my_interactions),

    "my_nutrition": ({
        "name": "my_nutrition",
        "description": ("The user's own logged daily totals for sodium, potassium, phosphorus, protein, and "
                        "fluids over the last N days, compared with targets she entered (if any)."),
        "input_schema": {"type": "object", "properties": {"days": {"type": "integer", "default": 7}}}},
        local.my_nutrition),

    "recall_memory": ({
        "name": "recall_memory",
        "description": ("Search this user's own history across all tabs: saved resources, sources found in "
                        "earlier answers, and past questions. Use when they refer to something from before."),
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
        local.recall_memory),
}


def schemas(names):
    return [TOOLS[n][0] for n in names]


def run(name, args, ctx):
    if name not in TOOLS:
        return {"results": {"error": f"unknown tool {name}"}, "sources": []}
    try:
        return TOOLS[name][1](args, ctx)
    except Exception as e:  # tools must never crash the turn; the model sees the error and adapts
        return {"results": {"error": f"{type(e).__name__}: {e}"}, "sources": []}
