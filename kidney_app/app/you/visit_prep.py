"""
Visit prep: a one-page sheet to bring to a PCP, OB/GYN, nephrology, or maternal-fetal medicine visit.

Two parts, built differently on purpose:
  1. FACTS ("What to tell my doctor") are assembled by code from her own entries: profile, medication list
     exactly as typed, doses she marked, nutrition averages, label-flagged interactions. Nothing invented.
  2. QUESTIONS are written by the model from: her stated concerns, her recent chat questions, her pins, the
     facts, and a curated standard list for the visit type. Every question must say what it's based on;
     unknown references are dropped. Questions only: no answers, advice, diagnoses, or doses.
     Interaction flags always produce a question, added by code if the model leaves one out.

STANDARD QUESTIONS are an engineer's starter list. Have a nephrologist and an OB review them.
"""
import json
import hashlib
import logging
from datetime import datetime, timedelta, timezone

from .. import config, presentation
from . import meds as meds_mod
from . import nutrition as nutrition_mod

VISIT_TYPES = {
    "pcp": "Primary care",
    "obgyn": "OB/GYN",
    "nephrology": "Nephrology",
    "maternal_fetal_medicine": "Maternal-fetal medicine",
    "other": "Another kind of doctor or clinician",
}
PREGNANT = {"pregnant"}
PLANNING = {"planning_pregnancy", "planning"}

STANDARD = {
    "nephrology": [
        "What are my latest eGFR and urine albumin (UACR) results, and how have they changed?",
        "What blood pressure range should I aim for, and should I check it at home?",
        "Do any of my medications need a different dose because of my kidney function?",
        "Are there limits on sodium, potassium, phosphorus, protein, or fluids I should follow? Can I see a renal dietitian?",
        "Which over-the-counter pain relievers are safe for my kidneys?",
        "When should I come back, and which labs will we check?",
    ],
    "nephrology+pregnancy": [
        "Which of my medications would need to change before or during a pregnancy?",
        "How might pregnancy affect my kidney function, and how will we monitor it?",
    ],
    "obgyn+pregnant": [
        "Does my kidney disease make this a high-risk pregnancy, and should I also see maternal-fetal medicine?",
        "Should I take low-dose aspirin to lower my preeclampsia risk, and when would I start?",
        "How should I check my blood pressure at home, and what numbers mean I should call you?",
        "Which of my medications are safe during pregnancy and while breastfeeding?",
        "How will you and my nephrologist share information during my pregnancy?",
        "What warning signs mean I should go to labor and delivery right away?",
    ],
    "obgyn+not_pregnant": [
        "Which birth control methods are safe with my kidney disease and my medications?",
        "What should I do before trying to get pregnant, and how far ahead should I plan?",
        "Do any of my medications need to change before I try to conceive?",
    ],
    "maternal_fetal_medicine": [
        "What extra monitoring will I need, such as ultrasounds and checks of my kidney function and urine protein?",
        "Should I take low-dose aspirin to lower my preeclampsia risk?",
        "What blood pressure readings at home mean I should call or come in?",
        "Which of my medications are safe for the baby, and do any need to change?",
        "What will the delivery plan look like, and where should I deliver?",
        "How will you coordinate with my nephrologist and OB?",
    ],
    "pcp": [
        "Are my vaccines up to date for someone with kidney disease (flu, pneumococcal, hepatitis B, COVID)?",
        "Is my blood pressure where it should be?",
        "Can you review all my medications and supplements, including over-the-counter ones, for kidney safety?",
        "Am I due for any screening tests or labs?",
        "Am I seeing the right specialists, and can you help coordinate between them?",
    ],
    "other": [
        "How does this visit connect with my kidney health and current care plan?",
        "Is there anything from my kidney history or medication list you need before we decide next steps?",
        "Who should I contact if I have questions after this visit?",
    ],
}

BASES = {"MY_PROFILE", "MY_MEDS", "MY_DOSES", "MY_NUTRITION", "MY_QUESTION", "MY_CONCERN", "MY_PIN", "STANDARD"}
NOTICE = ("This sheet lists your own entries and questions to ask. It isn't medical advice. Bring your actual "
          "medication bottles or list, since the app only knows what you entered.")

PROMPT = """You are helping a woman prepare for a {visit_label} visit.
Write a short factual summary of the context she wants this clinician to know, then a prioritized list of
{n_min}-{n_max} questions for HER to ask, in first person and plain language (8th-grade reading level).
Put her stated concerns and recent questions first, then the most relevant standard questions. Merge duplicates.
Tailor the result to her profile and data. Use general medical knowledge to help formulate useful questions;
retrieved RAG sources are not required.

Rules:
- The summary may only restate details in her profile, concerns, or app entries. Do not infer a diagnosis or risk.
- Write questions only in the questions list. Do not answer them, give advice, suggest diagnoses, or mention a
  dose or schedule change.
- Each question needs "why" (max 20 words, about HER situation) and "based_on": a list using ONLY these tokens:
  {bases} or a source ID from ALLOWED_SOURCE_IDS. Most questions can use the local context tokens; do not force
  a citation when a retrieved source isn't relevant.
- Also return "mention": up to 5 short first-person statements she should tell the doctor, drawn only from
  her concerns, recent questions, and facts (e.g. "I've marked 3 skipped evening doses in the past month").

PROFILE: {profile}
FACTS: {facts}
HER CONCERNS FOR THIS VISIT: {concerns}
HER RECENT QUESTIONS IN THE APP: {recent}
HER PINNED ITEMS: {pins}
STANDARD QUESTIONS FOR THIS VISIT: {standard}
ALLOWED_SOURCE_IDS: {allowed}

Return ONLY JSON: {{"summary": str, "mention": [str], "questions": [{{"question": str, "why": str, "based_on": [str]}}]}}"""

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ facts (deterministic)
def _adherence(store, user_id, since):
    rows = store.dose_log_since(user_id, since)
    by_slot = {}
    for r in rows:
        by_slot.setdefault((r["med_id"], r["scheduled_for"]), set()).add(r["status"])
    taken = sum("taken" in s for s in by_slot.values())
    skipped = sum("skipped" in s and "taken" not in s for s in by_slot.values())
    unmarked = sum(s == {"reminded"} for s in by_slot.values())
    return {"taken": taken, "skipped": skipped, "reminded_not_marked": unmarked,
            "note": "Based only on doses marked in the app."}


def _nutrition(store, user_id, start, end):
    summ = nutrition_mod.day_summary(store, user_id, start, end)
    days = summ["days"]
    if not days:
        return {"days_logged": 0}
    avg = {k: round(sum(d["totals"][k] for d in days) / len(days), 1) for k in nutrition_mod.KEYS}
    targets = store.get_profile(user_id).get("nutrition_targets") or {}
    return {"days_logged": len(days), "daily_average": avg,
            "targets_from_care_team": {k: v for k, v in targets.items() if k in avg},
            "note": "Estimates from meals logged in the app."}


def gather(store, user_id, days=30, check_interactions=True):
    profile = store.get_profile(user_id)
    tz = meds_mod._tz(store, user_id)
    today = datetime.now(tz).date()
    since_date = today - timedelta(days=days - 1)
    since_utc = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    age = None
    try:
        birth = datetime.strptime(profile.get("birth_date", ""), "%Y-%m-%d").date()
        age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    except (TypeError, ValueError):
        pass
    med_list = [{"name": m["name"], "ingredient": m["ingredient"], "dose_as_entered": m["dose_text"],
                 "times": m["times"], "days": m["days"]} for m in store.list_meds(user_id)]
    inter = None
    if check_interactions and len(med_list) > 1:
        try:
            inter = meds_mod.check_interactions(store, user_id)
        except Exception:
            log.exception("Visit prep interaction context failed for user %s", user_id)
    flags = []
    if inter:
        for p in inter["pairs"]:
            if p["flagged"]:
                f = p["label_flags"][0]
                flags.append({"drugs": p["drugs"], "label_of": f["label_of"], "quote": f["quote"],
                              "source_id": f["source_id"]})
    profile_context = {k: profile[k] for k in ("kidney_disease_type", "ckd_stage", "life_stage", "dialysis",
                                                 "transplant", "transplant_status", "primary_language")
                       if profile.get(k) not in (None, "")}
    if age is not None:
        profile_context["age_years"] = age
    facts = {
        "profile": profile_context,
        "medications": med_list,
        "doses_last_n_days": _adherence(store, user_id, since_utc) if med_list else None,
        "nutrition_last_n_days": {"days_logged": 0},
        "interaction_flags": flags,
        "period_days": days,
    }
    try:
        facts["nutrition_last_n_days"] = _nutrition(store, user_id, since_date.isoformat(), today.isoformat())
    except Exception:
        log.exception("Visit prep nutrition context failed for user %s", user_id)
    pins = store.board(user_id)["pinned_answers"]
    pin_view, sources = [], {}
    for p in pins[:10]:
        cits = (p["snapshot"] or {}).get("citations") or []
        pin_view.append({"title": p["title"], "question": p.get("question"),
                         "sources": [c["source_id"] for c in cits]})
        for c in cits:
            sources[c["source_id"]] = {"source_id": c["source_id"], "title": c.get("title"), "url": c.get("url"),
                                       **({"collection": c.get("collection")} if c.get("collection") else {})}
    for f in flags:
        lab = next((s for s in (inter or {}).get("sources", []) if s["source_id"] == f["source_id"]), None)
        sources[f["source_id"]] = lab or {"source_id": f["source_id"], "title": "FDA label", "url": None}
    recent = [r["content"][:200] for r in store.recent_user_questions(user_id, since_utc)]
    return {"facts": facts, "pins": pin_view, "recent_questions": recent, "sources": list(sources.values())}


def standard_questions(visit_type, life_stage):
    qs = list(STANDARD.get(visit_type, []))
    if visit_type == "nephrology" and life_stage in PREGNANT | PLANNING:
        qs += STANDARD["nephrology+pregnancy"]
    if visit_type == "obgyn":
        qs += STANDARD["obgyn+pregnant" if life_stage in PREGNANT else "obgyn+not_pregnant"]
    return qs


# ------------------------------------------------------------------ questions (model, then validated)
def _validate(raw_questions, allowed_ids):
    out, dropped = [], []
    for q in raw_questions or []:
        if not isinstance(q, dict):
            dropped.append("invalid question object")
            continue
        text = str(q.get("question", "")).strip()
        if not text:
            continue
        bases = [b for b in q.get("based_on") or [] if b in BASES or b in allowed_ids]
        dropped += [b for b in q.get("based_on") or [] if b not in BASES and b not in allowed_ids]
        if presentation.dosing_language(text):
            dropped.append(f"dosing language removed: {text[:60]}")
            continue
        out.append({"question": text, "why": str(q.get("why", ""))[:160], "based_on": bases or ["STANDARD"]})
    return out, dropped


def _ensure_interaction_questions(questions, flags):
    for f in flags:
        a, b = f["drugs"]
        if not any(a in q["question"].lower() and b in q["question"].lower() for q in questions):
            questions.insert(0, {"question": f"The FDA label for {f['label_of']} mentions an interaction between "
                                             f"{a} and {b}, which are both on my list. Is this combination right "
                                             f"for me, and does anything need monitoring?",
                                 "why": "Flagged by the app's label check.",
                                 "based_on": ["MY_MEDS", f["source_id"]], "added_by": "app"})
    return questions


def _fallback_summary(ctx, concerns):
    profile = ctx["facts"].get("profile", {})
    details = []
    if profile.get("kidney_disease_type"):
        details.append(str(profile["kidney_disease_type"]).replace("_", " "))
    if profile.get("ckd_stage"):
        details.append(f"CKD stage {profile['ckd_stage']}")
    if profile.get("dialysis") is True:
        details.append("on dialysis")
    if profile.get("transplant_status") == "had_transplant" or profile.get("transplant") is True:
        details.append("has had a kidney transplant")
    elif profile.get("transplant_status") == "waitlist":
        details.append("is on the kidney transplant waiting list")
    if profile.get("life_stage"):
        details.append(str(profile["life_stage"]).replace("_", " "))
    summary = "My profile includes " + ", ".join(details) + "." if details else "I want to discuss my health and care with this clinician."
    if concerns and concerns.strip():
        summary += " I also want to bring up: " + concerns.strip()[:300]
    return summary


def generate(store, llm, user_id, visit_type, concerns=None, days=30, n_min=8, n_max=12,
             visit_type_other=None):
    if visit_type not in VISIT_TYPES:
        raise ValueError(f"visit_type must be one of {list(VISIT_TYPES)}")
    if visit_type == "other" and not (visit_type_other or "").strip():
        raise ValueError("Tell us what kind of doctor or clinician you are visiting.")
    # Build a quick context signature first. If this exact visit sheet is cached, avoid
    # repeating the optional FDA-label interaction lookup and model generation.
    ctx = gather(store, user_id, days, check_interactions=False)
    life_stage = ctx["facts"]["profile"].get("life_stage")
    std = standard_questions(visit_type, life_stage)
    visit_label = (visit_type_other or "").strip() if visit_type == "other" else VISIT_TYPES[visit_type]
    allowed = sorted({s["source_id"] for s in ctx["sources"]})
    cache_payload = {"version": 2, "model": config.MODEL_FAST, "visit_type": visit_type,
                     "visit_label": visit_label, "concerns": (concerns or "")[:1500], "days": days,
                     "context": ctx, "standard": std}
    cache_key = hashlib.sha256(json.dumps(cache_payload, sort_keys=True, default=str).encode()).hexdigest()
    try:
        cached = store.get_visit_prep_cache(user_id, cache_key)
        if cached:
            return cached
    except Exception:
        log.exception("Visit prep cache read failed for user %s", user_id)
    if len(ctx["facts"]["medications"]) > 1:
        try:
            ctx = gather(store, user_id, days, check_interactions=True)
        except Exception:
            log.exception("Visit prep extended context failed for user %s", user_id)
    prompt = PROMPT.format(visit_label=visit_label, n_min=n_min, n_max=n_max, bases=sorted(BASES),
                           profile=json.dumps(ctx["facts"]["profile"]), facts=json.dumps(ctx["facts"], default=str),
                           concerns=(concerns or "(none given)")[:1500], recent=json.dumps(ctx["recent_questions"]),
                           pins=json.dumps(ctx["pins"]), standard=json.dumps(std), allowed=json.dumps(allowed))
    raw, data = "", {}
    try:
        resp = llm.messages.create(model=config.MODEL_FAST, max_tokens=1800,
                                   messages=[{"role": "user", "content": prompt}])
        raw = "".join(b.text for b in resp.content if b.type == "text").strip()
        if "{" in raw and "}" in raw:
            data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        if not isinstance(data, dict):
            data = {}
    except Exception:
        log.exception("Visit prep model generation failed for user %s", user_id)
    questions, dropped = _validate(data.get("questions"), set(allowed))
    if not questions:
        questions = [{"question": q, "why": "Standard question for this visit.", "based_on": ["STANDARD"]}
                     for q in std]
    questions = _ensure_interaction_questions(questions, ctx["facts"]["interaction_flags"])[: n_max + 2]
    raw_mentions = data.get("mention") if isinstance(data.get("mention"), list) else []
    mention = [m for m in raw_mentions[:5] if isinstance(m, str) and not presentation.dosing_language(m)]
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        summary = _fallback_summary(ctx, concerns)
    summary = summary.strip()[:700]

    by_id, order = {s["source_id"]: s for s in ctx["sources"]}, []
    for q in questions:                                    # number sources in order of first appearance
        for b in q["based_on"]:
            if b in by_id and b not in order:
                order.append(b)
    used = [by_id[i] for i in order]
    numbered = {sid: i + 1 for i, sid in enumerate(order)}
    citations = [{"n": numbered[s["source_id"]], "source_id": s["source_id"], "title": s.get("title"),
                  "url": s.get("url"), "kind": presentation.source_kind(s)} for s in used]
    for q in questions:
        q["refs"] = [numbered[b] for b in q["based_on"] if b in numbered]
    sheet = {"visit_type": visit_type, "visit_label": visit_label,
             "generated_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
             "summary": summary, "facts": ctx["facts"], "mention": mention, "questions": questions, "citations": citations,
             "notices": [NOTICE] + ([meds_mod.ALWAYS_NOTICE] if ctx["facts"]["interaction_flags"] else []),
             "validation": {"dropped": dropped, "fell_back_to_standard": not bool(raw)}}
    sheet["markdown"] = to_markdown(sheet)
    try:
        store.set_visit_prep_cache(user_id, cache_key, sheet)
    except Exception:
        log.exception("Visit prep cache write failed for user %s", user_id)
    return sheet


# ------------------------------------------------------------------ printable sheet
def to_markdown(s):
    f, L = s["facts"], []
    L.append(f"# Visit prep: {s['visit_label']}")
    L.append(f"_Prepared {s['generated_at'][:10]} from my entries in the app over the last {f['period_days']} days._\n")
    if f["profile"]:
        L.append("## About me")
        labels = {"life_stage": "Life stage", "ckd_stage": "CKD stage", "dialysis": "On dialysis",
                  "transplant": "Kidney transplant", "transplant_status": "Transplant status",
                  "kidney_disease_type": "Kidney disease type", "primary_language": "Primary language",
                  "age_years": "Age"}
        L += [f"- {labels.get(k, k)}: {('Yes' if v else 'No') if isinstance(v, bool) else str(v).replace('_', ' ')}"
              for k, v in f["profile"].items()]
        L.append("")
    if s.get("summary"):
        L.append("## Visit context")
        L.append(s["summary"])
        L.append("")
    L.append("## My medications (as I entered them)")
    if f["medications"]:
        for m in f["medications"]:
            when = ", ".join(m["times"]) + ("" if not m["days"] else " on " + ", ".join(
                "Mon Tue Wed Thu Fri Sat Sun".split()[d] for d in m["days"]))
            L.append(f"- **{m['name']}**" + (f" ({m['ingredient']})" if m["ingredient"] and m["ingredient"] != m["name"].lower() else "")
                     + f": {m['dose_as_entered'] or 'dose not entered'}, {when}")
    else:
        L.append("- None entered in the app.")
    d = f.get("doses_last_n_days")
    if d and (d["taken"] or d["skipped"] or d["reminded_not_marked"]):
        L.append(f"\nDoses I marked: {d['taken']} taken, {d['skipped']} skipped, {d['reminded_not_marked']} "
                 "reminders not marked. _(Only what I marked in the app.)_")
    n = f["nutrition_last_n_days"]
    if n.get("days_logged"):
        L.append(f"\n## What I've been eating (estimates, {n['days_logged']} days logged)")
        for k, v in n["daily_average"].items():
            t = n["targets_from_care_team"].get(k)
            L.append(f"- {k.replace('_mg', ' (mg)').replace('_g', ' (g)').replace('_ml', ' (ml)').replace('_', ' ')}: "
                     f"{v} per day" + (f" (my target: {t})" if t else ""))
    if f["interaction_flags"]:
        L.append("\n## Interaction flags from FDA labels")
        for fl in f["interaction_flags"]:
            L.append(f"- {fl['drugs'][0]} + {fl['drugs'][1]}: \"{fl['quote']}\"")
    if s["mention"]:
        L.append("\n## Things to mention")
        L += [f"- {m}" for m in s["mention"]]
    L.append("\n## My questions")
    for i, q in enumerate(s["questions"], 1):
        refs = "".join(f"[{r}]" for r in q.get("refs", []))
        L.append(f"{i}. [ ] {q['question']} {refs}".rstrip())
    L.append("\n_Notes from the visit:_\n\n\n")
    if s["citations"]:
        L.append("## Sources")
        L += [f"{c['n']}. {c['title']} ({c['kind']}){' ' + c['url'] if c.get('url') else ''}" for c in s["citations"]]
    L.append("\n---\n" + " ".join(s["notices"]))
    return "\n".join(L)
