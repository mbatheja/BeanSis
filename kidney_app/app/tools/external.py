"""
Live public APIs, all behind the persistent cache.

Every function returns {"results": ..., "sources": [{"source_id", "title", "url"}]}.
The orchestrator collects "sources" from every tool call for citation checking and memory.
"""
import xml.etree.ElementTree as ET

from .. import config
from ..cache import cached_get_json, cached_get_text

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
OPENFDA = "https://api.fda.gov/drug"
RXNAV = "https://rxnav.nlm.nih.gov/REST"


# ------------------------------------------------------------------ PubMed
def _ncbi_params(extra):
    p = {"tool": "kidney-womens-health", "email": config.CONTACT_EMAIL, **extra}
    if config.NCBI_API_KEY:
        p["api_key"] = config.NCBI_API_KEY
    return p


def parse_pubmed_xml(xml_text):
    out = []
    for art in ET.fromstring(xml_text).iter("PubmedArticle"):
        pmid = art.findtext(".//MedlineCitation/PMID")
        title = "".join(art.find(".//ArticleTitle").itertext()) if art.find(".//ArticleTitle") is not None else ""
        abstract = " ".join(
            ((a.get("Label") + ": ") if a.get("Label") else "") + "".join(a.itertext())
            for a in art.findall(".//Abstract/AbstractText"))
        year = art.findtext(".//JournalIssue/PubDate/Year") or art.findtext(".//ArticleDate/Year")
        pub_types = [p.text for p in art.findall(".//PublicationType") if p.text]
        out.append({"pmid": pmid, "title": title, "year": year, "journal": art.findtext(".//Journal/Title"),
                    "pub_types": pub_types, "abstract": abstract[:1500]})
    return out


def pubmed_search(query, max_results=6, min_year=None):
    """Live PubMed search returning parsed abstracts. Abstracts only; full text is not fetched."""
    term = query + (f" AND {min_year}:3000[dp]" if min_year else "")
    ids = cached_get_json(f"{EUTILS}/esearch.fcgi", _ncbi_params(
        {"db": "pubmed", "term": term, "retmax": max_results, "sort": "relevance", "retmode": "json"}),
        ttl=config.TTL["pubmed"]).get("esearchresult", {}).get("idlist", [])
    if not ids:
        return {"results": [], "sources": []}
    xml = cached_get_text(f"{EUTILS}/efetch.fcgi", _ncbi_params(
        {"db": "pubmed", "id": ",".join(ids), "retmode": "xml"}), ttl=config.TTL["pubmed"])
    arts = parse_pubmed_xml(xml)
    return {"results": arts,
            "sources": [{"source_id": f"PMID:{a['pmid']}", "title": a["title"],
                         "url": f"https://pubmed.ncbi.nlm.nih.gov/{a['pmid']}/"} for a in arts]}


# ------------------------------------------------------------------ RxNorm
def normalize_drug(name):
    """Map a brand/misspelled drug name to its RxNorm ingredient(s) (needed for openFDA lookups)."""
    approx = cached_get_json(f"{RXNAV}/approximateTerm.json", {"term": name, "maxEntries": 1},
                             ttl=config.TTL["rxnav"])
    cands = approx.get("approximateGroup", {}).get("candidate", [])
    if not cands:
        return {"results": {"input": name, "ingredients": []}, "sources": []}
    rxcui = cands[0]["rxcui"]
    rel = cached_get_json(f"{RXNAV}/rxcui/{rxcui}/related.json", {"tty": "IN"}, ttl=config.TTL["rxnav"])
    ings = [c["name"] for g in rel.get("relatedGroup", {}).get("conceptGroup", []) or []
            for c in g.get("conceptProperties", []) or []]
    return {"results": {"input": name, "rxcui": rxcui, "ingredients": ings},
            "sources": [{"source_id": f"RXCUI:{rxcui}", "title": f"RxNorm: {', '.join(ings) or name}",
                         "url": f"https://mor.nlm.nih.gov/RxNav/search?searchBy=RXCUI&searchTerm={rxcui}"}]}


# ------------------------------------------------------------------ openFDA
def _fda(params):
    if config.OPENFDA_API_KEY:
        params = {**params, "api_key": config.OPENFDA_API_KEY}
    return params


def faers_summary(generic_name, female_only=True, consumer_reports_only=False, top_n=15):
    """
    Most-reported reactions in FAERS for a drug. These are voluntary report COUNTS:
    they cannot establish causation or how common a side effect is.
    """
    q = f'patient.drug.openfda.generic_name:"{generic_name}"'
    if female_only:
        q += " AND patient.patientsex:2"
    if consumer_reports_only:
        q += " AND primarysource.qualification:5"   # reported by patients/non-health-professionals
    total = cached_get_json(f"{OPENFDA}/event.json", _fda({"search": q, "limit": 1}),
                            ttl=config.TTL["openfda"]).get("meta", {}).get("results", {}).get("total", 0)
    counts = cached_get_json(f"{OPENFDA}/event.json", _fda(
        {"search": q, "count": "patient.reaction.reactionmeddrapt.exact", "limit": top_n}),
        ttl=config.TTL["openfda"]).get("results", [])
    sid = f"FAERS:{generic_name.lower()}" + (":female" if female_only else "") + \
          (":consumer" if consumer_reports_only else "")
    return {"results": {"drug": generic_name, "total_reports": total,
                        "filters": {"female_only": female_only, "consumer_reports_only": consumer_reports_only},
                        "top_reactions": [{"reaction": c["term"], "reports": c["count"]} for c in counts],
                        "caveat": "Voluntary reports; counts do not show causation or frequency."},
            "sources": [{"source_id": sid, "title": f"FDA FAERS reports: {generic_name}",
                         "url": "https://open.fda.gov/data/faers/"}] if total else []}


LABEL_SECTIONS = ["boxed_warning", "pregnancy", "pregnancy_or_breast_feeding", "lactation",
                  "use_in_specific_populations", "warnings_and_cautions", "adverse_reactions"]


def drug_label(generic_name, sections=None, max_chars=1200):
    """Official FDA label sections most relevant here: pregnancy, lactation, renal use, warnings."""
    data = cached_get_json(f"{OPENFDA}/label.json", _fda(
        {"search": f'openfda.generic_name:"{generic_name}"', "limit": 1}), ttl=config.TTL["openfda"])
    res = data.get("results", [])
    if not res:
        return {"results": {"drug": generic_name, "found": False}, "sources": []}
    lab = res[0]
    out = {s: " ".join(lab[s])[:max_chars] for s in (sections or LABEL_SECTIONS) if s in lab}
    set_id = lab.get("set_id")
    return {"results": {"drug": generic_name, "found": True, "sections": out},
            "sources": [{"source_id": f"LABEL:{set_id}", "title": f"FDA label: {generic_name}",
                         "url": f"https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid={set_id}"}]}
