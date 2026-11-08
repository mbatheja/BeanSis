"""
Ride finder for California (Medi-Cal NMT/NEMT), as a step-by-step flow.

Each call takes what's known so far and returns EITHER the next question (with options) OR a result card.
Order: ZIP -> county (only if the ZIP spans counties) -> coverage -> Medi-Cal plan -> destination -> mobility.
Pregnancy/postpartum comes from her profile (it changes NMT eligibility).

Rules come from DHCS (sources in SOURCES); nothing here is invented:
  - NMT (car/public transit): full-scope Medi-Cal, or pregnant through the end of the month in which the
    365th day postpartum falls. Tell the plan there is no other way to get to the appointment.
  - NEMT (wheelchair van, litter van, ambulance): for people who can't use a car/transit; needs a
    prescription from her doctor.
  - Managed care: request through the plan's member services. Fee-for-service: DHCS request form / email.
  - Driving herself isn't reimbursed; plans may reimburse a friend or family member who drove her.
Phone numbers come ONLY from data/raw/ca_medi_cal_plans.csv rows your team verified (last_verified set).
No verified number -> no call button; the card links the official DHCS plan directory instead.
"""
import csv
from datetime import date
from pathlib import Path

from .. import config

DIRECTORY = "https://www.dhcs.ca.gov/individuals/medi-cal-managed-care-health-plan-directory/"
# Medi-Cal Health Care Options: plan enrollment line (from the DHCS county directory pages)
HCO = {"label": "Medi-Cal Health Care Options (plan enrollment)", "phone": "1-800-430-4263",
       "tel": "tel:+18004304263", "tty": "1-800-430-7077", "hours": "Monday-Friday, 8 a.m.-6 p.m.",
       "url": "https://www.healthcareoptions.dhcs.ca.gov"}
PLAN_TYPE_LABEL = {"pace": " (PACE program, age 55+)", "specialty": " (specialty plan)",
                   "specialty_scan": " (age 65+ with Medicare and Medi-Cal)"}
SOURCES = [
    {"source_id": "DOC:medi-cal-transport", "title": "DHCS: Medi-Cal Transportation Services",
     "url": "https://www.dhcs.ca.gov/services/medi-cal-resources/transportation-services/"},
    {"source_id": "DOC:medi-cal-transport-faq", "title": "DHCS: Transportation FAQ for Medi-Cal members",
     "url": "https://www.dhcs.ca.gov/services/medi-cal-resources/frequently-asked-questions-for-medi-cal-"
            "transportation-services-transportation-beneficiaries-faq/"},
]
FFS_CONTACT = {"label": "DHCS transportation request (fee-for-service)", "email": "DHCSNMT@dhcs.ca.gov",
               "url": "https://www.dhcs.ca.gov/services/medi-cal-resources/transportation-services/",
               "how": "Use the NMT/NEMT Scheduling Form on this page; DHCS replies by secure email with the "
                      "request form."}
FALLBACKS = [
    {"name": "ADA paratransit", "detail": "Shared-ride service from your local transit agency for people who "
                                          "can't use regular buses or trains. Ask your transit agency how to apply."},
    {"name": "American Kidney Fund", "detail": "Grant programs that may help eligible kidney patients with "
                                               "treatment-related costs. Check eligibility.",
     "url": "https://www.kidneyfund.org"},
    {"name": "211", "detail": "Call or text 211 for local ride programs and other help near you.",
     "url": "https://www.211.org", "phone": "211"},
]
DESTINATIONS = {"dialysis": "Dialysis (regular sessions)", "prenatal": "Prenatal or postpartum visit",
                "other_appointment": "Another medical appointment", "pharmacy": "Pharmacy or medical supplies"}
MOBILITY = {"car": "I can ride in a regular car or take transit", "wheelchair": "I need a wheelchair van",
            "stretcher": "I need a stretcher (litter van) or ambulance"}
COVERAGE = {"medi_cal": "Medi-Cal", "medicare": "Medicare", "both": "Both Medi-Cal and Medicare",
            "neither": "Neither / not sure"}
PLANS_CSV = Path(config.DATA_DIR) / "raw" / "ca_medi_cal_plans.csv"


# ------------------------------------------------------------------ data access (swappable for tests)
def counties_for_zip(zip_code):
    from .. import refdb
    cur = refdb.cursor()
    cur.execute("SELECT county FROM zip_county WHERE zip = ? ORDER BY land_share DESC", [str(zip_code)[:5]])
    return [r[0] for r in cur.fetchall()]


def plans_for_county(county):
    if not PLANS_CSV.exists():
        return []
    return [r for r in csv.DictReader(open(PLANS_CSV, encoding="utf-8"))
            if r["county"].strip().lower() == county.strip().lower()]


# ------------------------------------------------------------------ the flow
def _ask(field, question, options=None, why=None):
    return {"done": False, "ask": {"field": field, "question": question,
                                   "options": [{"value": k, "label": v} for k, v in (options or {}).items()],
                                   "why": why}}


def _phone(p):
    digits = "".join(c for c in (p or "") if c.isdigit())
    return f"tel:+1{digits[-10:]}" if len(digits) >= 10 else None


def _postpartum_note(profile):
    stage = (profile or {}).get("life_stage")
    if stage == "pregnant":
        return "Because you're pregnant, you qualify for rides (NMT) during pregnancy and after."
    if stage == "postpartum":
        return ("Rides (NMT) continue after pregnancy through the end of the month in which the 365th day "
                "postpartum falls.")
    return None


def next_step(answers, profile=None):
    a = {k: v for k, v in (answers or {}).items() if v not in (None, "")}
    if "zip" not in a:
        return _ask("zip", "What's your ZIP code?")
    counties = a.get("_counties") or counties_for_zip(a["zip"])
    if not counties:
        return _ask("zip", "I couldn't find that ZIP code in California. Can you check it?",
                    why="This ride program is for California Medi-Cal.")
    if "county" not in a:
        if len(counties) > 1:
            return _ask("county", "Your ZIP code covers more than one county. Which county do you live in?",
                        {c: c for c in counties})
        a["county"] = counties[0]
    if "coverage" not in a:
        return _ask("coverage", "Do you have Medi-Cal, Medicare, both, or neither?", COVERAGE)

    plan = None
    if a["coverage"] in ("medi_cal", "both"):
        plans = plans_for_county(a["county"])
        if "plan" not in a:
            def label(p):
                extra = PLAN_TYPE_LABEL.get(p.get("plan_type"), "")
                if p.get("plan_type") == "pace" and "PACE" in p["plan_name"]:
                    extra = " (age 55+)"
                return p["plan_name"] + extra
            # main Medi-Cal plans first, then PACE and specialty plans
            plans = sorted(plans, key=lambda p: p.get("plan_type") not in ("managed_care", "", None))
            opts = {p["plan_name"]: label(p) for p in plans}
            opts.update({"fee_for_service": "No plan (fee-for-service Medi-Cal)", "unknown": "I'm not sure"})
            return _ask("plan", "Which Medi-Cal plan are you in? It's on your Medi-Cal ID card.", opts)
        plan = next((p for p in plans if p["plan_name"] == a["plan"]), None)
    if "destination" not in a:
        return _ask("destination", "Where are you going?", DESTINATIONS)
    if "mobility" not in a:
        return _ask("mobility", "Can you ride in a regular car, or do you need a wheelchair van or stretcher?",
                    MOBILITY)
    return {"done": True, "card": build_card(a, plan, profile)}


def build_card(a, plan, profile):
    nemt = a["mobility"] in ("wheelchair", "stretcher")
    mode = "NEMT" if nemt else "NMT"
    mode_label = ("Non-Emergency Medical Transportation (wheelchair van, litter van, or ambulance)" if nemt
                  else "Non-Medical Transportation (car or public transit)")
    dest = DESTINATIONS[a["destination"]].lower()
    card = {"county": a["county"], "coverage": a["coverage"], "destination": a["destination"],
            "mode": mode, "mode_label": mode_label, "contact": None, "call_script": None,
            "requirements": [], "notes": [], "fallbacks": FALLBACKS, "sources": SOURCES,
            "disclaimer": "Rules and phone numbers can change. If something doesn't match, ask your plan."}

    if a["coverage"] in ("medi_cal", "both"):
        if nemt:
            card["requirements"].append("You need a prescription from your doctor saying you need this type of "
                                        "ride. Ask your doctor's office before you call.")
        else:
            card["requirements"].append("Tell them you have no other way to get to your appointment.")
        if a["destination"] == "dialysis":
            card["requirements"].append("Dialysis is regular, so ask if they can set up standing rides for "
                                        "every session.")
        card["requirements"].append("Ask how far ahead you need to book, and have your Medi-Cal ID card ready.")
        note = _postpartum_note(profile)
        if note:
            card["notes"].append(note)
        card["notes"].append("Medi-Cal doesn't pay you back for driving yourself, but your plan may reimburse a "
                             "friend or family member who drives you. Ask first.")
        if a["coverage"] == "both":
            card["notes"].append("With both Medi-Cal and Medicare, rides to appointments come through Medi-Cal.")

        if a.get("plan") == "fee_for_service":
            card["contact"] = FFS_CONTACT
        elif plan:
            verified = (plan.get("last_verified") or "").strip()
            number = plan.get("ride_line_phone") or plan.get("member_services_phone")
            card["contact"] = {
                "label": f"{plan['plan_name']} {'ride line' if plan.get('ride_line_phone') else 'member services'}",
                "phone": number if verified else None,
                "tel": _phone(number) if verified else None,
                "tty": plan.get("tty") if verified else None,
                "other_phones": [{"label": lbl.strip(), "phone": num.strip(), "tel": _phone(num)}
                                 for lbl, num in (part.split(":", 1) for part in (plan.get("other_phones") or "").split(";")
                                                  if ":" in part)] if verified else [],
                "last_verified": verified or None,
                "url": plan.get("url") or DIRECTORY}
            if not verified:
                card["notes"].append("We haven't verified this plan's number yet. Use the number on your Medi-Cal "
                                     "ID card or the DHCS plan directory.")
            elif not number:
                card["contact"]["url"] = DIRECTORY
                card["notes"].append("We don't have this plan's main phone number. Use the number on your ID card "
                                     "or the DHCS plan directory.")
        else:
            card["contact"] = {"label": "Find your plan's member services number", "url": DIRECTORY,
                               "how": "Your plan name and member services number are on your Medi-Cal ID card."}
            card["contact"]["alternate"] = {**HCO, "how": "No ID card handy? Medi-Cal Health Care Options handles "
                                                          "plan enrollment and can help you find your plan."}
        card["call_script"] = (
            f"Hi, I'm a Medi-Cal member and I need a ride to {dest}. "
            + ("I use a wheelchair, so I need a wheelchair van. " if a["mobility"] == "wheelchair" else
               "I need a stretcher or ambulance transport. " if a["mobility"] == "stretcher" else
               "I don't have another way to get there. ")
            + ("My dialysis is on a regular schedule. Can we set up rides for every session? "
               if a["destination"] == "dialysis" else "")
            + "How far ahead do I need to book, and what information do you need from me?")
    elif a["coverage"] == "medicare":
        card["mode"], card["mode_label"] = None, "Medicare"
        card["notes"] += [
            "Original Medicare generally doesn't cover rides to routine appointments. It covers ambulance rides "
            "only when other transportation could endanger your health.",
            "Some Medicare Advantage plans include rides to appointments. Call the number on your plan card.",
            "If your income is limited, you might also qualify for Medi-Cal, which covers rides."]
    else:
        card["mode"], card["mode_label"] = None, "No coverage on file"
        card["notes"].append("You might qualify for Medi-Cal, which covers rides to appointments.")
        if (profile or {}).get("life_stage") == "pregnant":
            card["notes"].append("If you're pregnant, Presumptive Eligibility (PE4PP) can give immediate, "
                                 "temporary coverage while a Medi-Cal application is pending.")
            card["sources"] = SOURCES + [{"source_id": "DOC:medi-cal-pe4pp", "title": "DHCS: PE4PP",
                                          "url": "https://www.dhcs.ca.gov/services/medi-cal-resources/"
                                                 "medi-cal-eligibility-division/presumptive-eligibility-for-"
                                                 "pregnant-people/"}]
    return card


def ride_help(args, ctx):
    """Chat tool: pass everything learned so far; returns the next question or the result card."""
    answers = dict(args)
    profile = ctx.get("profile", {})
    if "zip" not in answers and profile.get("zip"):
        answers["zip"] = profile["zip"]
    res = next_step(answers, profile)
    srcs = res["card"]["sources"] if res["done"] else []
    return {"results": res, "sources": srcs}
