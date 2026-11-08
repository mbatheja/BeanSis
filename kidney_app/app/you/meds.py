"""
My medications: the user's own list, reminders, and an interaction check.

The app never sets or changes doses. dose_text is stored exactly as the user enters it from her prescription.

Interaction check (deterministic, citable; no LLM verdicts):
  1. FDA label: does drug A's "Drug Interactions" / "Contraindications" section name drug B
     (ingredient or its FDA pharmacologic class)? Quote the sentence and cite the label.
  2. FAERS: how many reports filed for women list both drugs. Context only: "reported together"
     is NOT evidence of an interaction.
  NLM's interaction API was discontinued in Jan 2024, so labels are the free authoritative source.
  A label only catches what it names, so "no flag" never means "no interaction".
"""
import itertools
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .. import config
from ..cache import cached_get_json
from ..tools import external

RXCLASS = "https://rxnav.nlm.nih.gov/REST/rxclass/class/byRxcui.json"
LABEL_FIELDS = ["drug_interactions", "contraindications", "boxed_warning"]
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

ALWAYS_NOTICE = ("This check only finds interactions that FDA labels name directly. No flag does not mean no "
                 "interaction. Review your full medication list, including supplements and over-the-counter "
                 "drugs, with your pharmacist.")
FAERS_NOTICE = ("'Reported together' counts come from voluntary FDA reports that listed both drugs. They do not "
                "show that the drugs interact.")
REMINDER_TEXT = "Time for your {time} medications. Open the app to see which ones."   # no drug names on lock screens


# ------------------------------------------------------------------ list management
def add(store, user_id, name, dose_text, times, days=None, reminders=True):
    bad = [t for t in times if not TIME_RE.match(t)]
    if not times or bad:
        raise ValueError(f"times must be HH:MM (24h); got {bad or times}")
    norm = external.normalize_drug(name)["results"]
    ings = norm.get("ingredients") or []
    ingredient = ings[0].lower() if len(ings) == 1 else (", ".join(i.lower() for i in ings) or None)
    med_id = store.add_med(user_id, name, ingredient, norm.get("rxcui"), dose_text, sorted(times), days, reminders)
    return {"med_id": med_id, "ingredient": ingredient, "recognized": bool(ings)}


# ------------------------------------------------------------------ reminders
def _12h(t):
    hh, mm = map(int, t.split(":"))
    return f"{hh % 12 or 12}:{mm:02d} {'AM' if hh < 12 else 'PM'}"


def _tz(store, user_id):
    try:
        return ZoneInfo(store.get_profile(user_id).get("timezone") or config.DEFAULT_TIMEZONE)
    except Exception:
        return ZoneInfo(config.DEFAULT_TIMEZONE)


def todays_schedule(store, user_id, now=None):
    """Today's doses for the You tab, with taken/skipped status."""
    tz = _tz(store, user_id)
    now = (now or datetime.now(tz)).astimezone(tz)
    out = []
    for m in store.list_meds(user_id):
        if m["days"] is not None and now.weekday() not in m["days"]:
            continue
        for t in m["times"]:
            sched = f"{now.date().isoformat()}T{t}"
            st = store.dose_statuses(m["med_id"], sched)
            status = "taken" if "taken" in st else "skipped" if "skipped" in st else "upcoming" \
                if sched > now.strftime("%Y-%m-%dT%H:%M") else "due"
            out.append({"time": t, "scheduled_for": sched, "med_id": m["med_id"], "name": m["name"],
                        "dose_text": m["dose_text"], "status": status})
    return sorted(out, key=lambda d: d["time"])


def due_reminders(store, now_utc=None):
    """Reminders to send now (one per user per time slot), marked 'reminded' so they're sent once."""
    now_utc = now_utc or datetime.now(ZoneInfo("UTC"))
    out = []
    for user in store.users_with_reminders():
        tz = _tz(store, user)
        now = now_utc.astimezone(tz)
        slots = {}
        for m in store.list_meds(user):
            if not m["reminders"] or (m["days"] is not None and now.weekday() not in m["days"]):
                continue
            for t in m["times"]:
                hh, mm = map(int, t.split(":"))
                sched_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if not (sched_dt <= now < sched_dt + timedelta(minutes=config.REMINDER_WINDOW_MIN)):
                    continue
                sched = sched_dt.strftime("%Y-%m-%dT%H:%M")
                if store.dose_statuses(m["med_id"], sched):      # already reminded / taken / skipped
                    continue
                store.log_dose(user, m["med_id"], sched, "reminded")
                slots.setdefault(t, []).append(m["med_id"])
        for t, med_ids in slots.items():
            out.append({"user_id": user, "time": t, "med_ids": med_ids,
                        "message": REMINDER_TEXT.format(time=_12h(t))})
    return out


def send_reminders(store, now_utc=None):
    """POST due reminders to REMINDER_WEBHOOK_URL (your email/SMS/push relay). Returns what was due."""
    due = due_reminders(store, now_utc)
    if due and config.REMINDER_WEBHOOK_URL:
        import requests
        for r in due:
            try:
                requests.post(config.REMINDER_WEBHOOK_URL, json=r, timeout=10,
                              headers={"x-relay-secret": config.REMINDER_WEBHOOK_SECRET or ""})
            except Exception as e:
                print(f"reminder webhook failed: {e}")
    return due


# ------------------------------------------------------------------ interactions
def _classes(rxcui):
    """FDA Established Pharmacologic Class names for a drug, e.g. 'Calcineurin Inhibitor Immunosuppressant'."""
    if not rxcui:
        return []
    data = cached_get_json(RXCLASS, {"rxcui": rxcui, "relaSource": "FDASPL", "relas": "has_epc"},
                           ttl=config.TTL["rxnav"])
    names = {i["rxclassMinConceptItem"]["className"]
             for i in data.get("rxclassDrugInfoList", {}).get("rxclassDrugInfo", []) or []}
    return sorted(names)


def _label_sections(ingredient):
    lab = external.drug_label(ingredient, sections=LABEL_FIELDS, max_chars=20000)
    return lab["results"].get("sections", {}), (lab["sources"][0] if lab["sources"] else None)


def _sentences_mentioning(text, terms):
    hits = []
    for sent in re.split(r"(?<=[.;])\s+", text):
        low = sent.lower()
        matched = [t for t in terms if len(t) > 3 and re.search(rf"\b{re.escape(t.lower())}", low)]
        if matched:
            hits.append({"matched": matched, "text": sent.strip()[:400]})
    return hits[:3]


def faers_co_reported(a, b):
    q = (f'patient.drug.openfda.generic_name:"{a}" AND patient.drug.openfda.generic_name:"{b}" '
         f"AND patient.patientsex:2")
    data = cached_get_json(f"{external.OPENFDA}/event.json", external._fda({"search": q, "limit": 1}),
                           ttl=config.TTL["openfda"])
    return data.get("meta", {}).get("results", {}).get("total", 0)


def check_interactions(store, user_id, extra=None):
    """
    Check every pair in the user's active list (plus an optional drug she's asking about).
    Returns label flags with quoted sentences + citations, FAERS co-report counts, and notices.
    """
    meds = [{"name": m["name"], "ingredient": m["ingredient"], "rxcui": m["rxcui"]}
            for m in store.list_meds(user_id) if m["ingredient"]]
    if extra:
        n = external.normalize_drug(extra)["results"]
        if n.get("ingredients"):
            meds.append({"name": extra, "ingredient": n["ingredients"][0].lower(), "rxcui": n.get("rxcui")})
    info = {}
    for m in meds:
        sections, src = _label_sections(m["ingredient"])
        info[m["ingredient"]] = {**m, "sections": sections, "label": src, "classes": _classes(m["rxcui"])}

    pairs, sources = [], {}
    for a, b in itertools.combinations(info, 2):
        flags = []
        for x, y in ((a, b), (b, a)):              # does x's label mention y (by name or class)?
            terms = [y] + [c.replace(" [EPC]", "") for c in info[y]["classes"]]
            for field, text in info[x]["sections"].items():
                for hit in _sentences_mentioning(text, terms):
                    flags.append({"label_of": x, "section": field, "mentions": hit["matched"],
                                  "quote": hit["text"], "source_id": info[x]["label"]["source_id"]})
                    sources[info[x]["label"]["source_id"]] = info[x]["label"]
        try:
            co = faers_co_reported(a, b)
        except Exception:
            co = None
        pairs.append({"drugs": [a, b], "label_flags": flags, "flagged": bool(flags),
                      "faers_reported_together_women": co})
    no_label = [i for i, v in info.items() if not v["label"]]
    return {"medications_checked": list(info), "pairs": pairs,
            "flagged_pairs": sum(p["flagged"] for p in pairs),
            "not_checked": [m["name"] for m in store.list_meds(user_id) if not m["ingredient"]] + no_label,
            "notices": [ALWAYS_NOTICE, FAERS_NOTICE], "sources": list(sources.values())}
