"""
Safety gate. Runs BEFORE any agent.

1. Deterministic rules (always on, zero latency) catch red-flag phrasing.
2. Optional fast-model check catches what regex misses.
If either fires, the app returns a fixed, clinician-reviewable message and skips the agent entirely.
Have a clinician review these patterns and messages before any real users see the app.
"""
import json
import re

from . import config

CRISIS = ("If you're thinking about suicide or harming yourself, please reach out now: call or text 988 "
          "(Suicide & Crisis Lifeline, US), or call 911 if you're in immediate danger. "
          "Postpartum depression is common and treatable, and you deserve support.")

PREGNANCY_URGENT = ("Some of what you describe can be a sign of a pregnancy emergency such as preeclampsia. "
                    "Please call your OB or labor & delivery unit now, or 911 if symptoms are severe. "
                    "Don't wait for an app.")

KIDNEY_URGENT = ("These symptoms can signal a dangerous problem (for example very high potassium or fluid "
                 "overload). Please contact your kidney care team or dialysis unit now, or call 911 if severe.")

GENERAL_URGENT = "This sounds like it may need urgent care. Please call 911 or go to the nearest emergency room."

# (pattern, message, needs_first_person). Broad on purpose: a false positive costs a little, a miss a lot.
# Symptom rules require first-person language so "what are signs of preeclampsia?" still gets answered.
_PREG = r"\b(pregnan\w*|postpartum|weeks? along|trimester|gave birth|delivered|since the baby|after (the )?birth)\b"
_PREG_SYMPTOM = (r"\b(severe headache|terrible headache|pounding headache|headache that won'?t go away|"
                 r"blurr\w* vision|vision changes|seeing spots|seizure\w*|sudden(ly)? swell\w*|swell\w* up suddenly|"
                 r"swelling of (my )?face|right upper (belly|abdominal) pain|pain under my right ribs|"
                 r"heavy bleeding|baby (isn'?t|is not|not) moving)\b")

RULES = [
    (r"\b(kill myself|end(ing)? my (own )?life|suicid\w*|want to die|self[- ]?harm|hurt myself|"
     r"better off without me)\b", CRISIS, False),
    # pregnancy context and a warning sign anywhere in the message, in either order
    (rf"(?=.*{_PREG})(?=.*{_PREG_SYMPTOM})", PREGNANCY_URGENT, True),
    (r"\b(missed (my )?dialysis|skipped dialysis)\b.*\b(short(ness)? of breath|chest|weak\w*|palpitations|"
     r"flutter\w*|racing|heavy)\b", KIDNEY_URGENT, True),
    (r"\b(haven'?t|have not|can'?t|cannot)\s+(urinat\w*|pee\w*)\b|\bno urine\b", KIDNEY_URGENT, True),
    (r"\b(chest pain|can'?t breathe|trouble breathing|passed out|fainted|unconscious)\b", GENERAL_URGENT, True),
]

_COMPILED = [(re.compile(p, re.IGNORECASE | re.DOTALL), m, fp) for p, m, fp in RULES]
_FIRST_PERSON = re.compile(r"\b(i|i'?m|i am|i've|my|me)\b", re.IGNORECASE)

LLM_PROMPT = """Classify this message from a patient-education app for women with kidney disease.
Return ONLY JSON: {"urgent": true|false, "type": "crisis"|"pregnancy"|"kidney"|"general"|"none"}
urgent=true only if the person describes CURRENT symptoms or intent that need emergency care now
(e.g. suicidal intent, preeclampsia warning signs, signs of very high potassium, can't breathe).
General questions about symptoms ("what are signs of preeclampsia?") are NOT urgent.
Message: """

_BY_TYPE = {"crisis": CRISIS, "pregnancy": PREGNANCY_URGENT, "kidney": KIDNEY_URGENT, "general": GENERAL_URGENT}


def rule_check(text):
    first_person = bool(_FIRST_PERSON.search(text))
    for pat, msg, needs_fp in _COMPILED:
        if pat.search(text) and (first_person or not needs_fp):
            return msg
    return None


def check(text, llm=None):
    """Return an urgent message string, or None if the agent may proceed."""
    msg = rule_check(text)
    if msg or not (llm and config.SAFETY_LLM_CHECK):
        return msg
    try:
        resp = llm.messages.create(model=config.MODEL_FAST, max_tokens=60,
                                   messages=[{"role": "user", "content": LLM_PROMPT + text[:2000]}])
        raw = "".join(b.text for b in resp.content if b.type == "text").strip()
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
        verdict = json.loads(raw)
        if verdict.get("urgent"):
            return _BY_TYPE.get(verdict.get("type"), GENERAL_URGENT)
    except Exception:
        pass  # classifier failure must not block the user; the rules already ran
    return None
