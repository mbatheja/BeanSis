"""
HTTP API.  Run:  uvicorn app.api:app --reload

Hackathon note: user_id is passed by the client. Before real users, replace with real auth
(e.g. Supabase Auth) and derive user_id from the verified token.
"""
import os
import threading
import time
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import agents, appstate, config, llm as llm_mod, orchestrator, security
from .you import meds as meds_mod
from .you import nutrition as nutrition_mod
from .you import visit_prep as visit_prep_mod

app = FastAPI(title="Kidney Women's Health API")
# The browser never calls this API directly (it goes through the Base44 proxy), so CORS is closed unless
# ALLOWED_ORIGINS is set, e.g. for local frontend development.
_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_methods=["*"], allow_headers=["*"])
_limiter = security.RateLimiter()


@app.middleware("http")
async def guard(request, call_next):
    from fastapi.responses import JSONResponse
    if not security.secret_ok(request.url.path, request.headers.get("x-app-secret")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if security.is_costly(request.url.path) and request.method == "POST":
        uid = request.headers.get("x-user-id") or "anonymous"
        if not _limiter.allow(uid):
            return JSONResponse({"error": "Too many requests. Please wait a minute and try again."},
                                status_code=429)
    return await call_next(request)


@app.get("/healthz")
def healthz():
    return {"ok": True}

store = appstate.Store()
llm = llm_mod.get_client()   # LLM_PROVIDER: anthropic (ANTHROPIC_API_KEY) or nebius (NEBIUS_API_KEY)


def _purge_loop():
    """Delete expired chats at startup and then daily. Pinned answers are never purged."""
    while True:
        try:
            n = store.purge_expired()
            if n:
                print(f"retention: deleted {n} expired chat(s)")
        except Exception as e:
            print(f"retention purge failed: {e}")
        time.sleep(24 * 3600)


def _reminder_loop():
    """Every minute: find due medication reminders and POST them to REMINDER_WEBHOOK_URL."""
    while True:
        try:
            meds_mod.send_reminders(store)
        except Exception as e:
            print(f"reminder loop failed: {e}")
        time.sleep(60)


threading.Thread(target=_purge_loop, daemon=True).start()
threading.Thread(target=_reminder_loop, daemon=True).start()


class ChatIn(BaseModel):
    user_id: str
    message: str
    thread_id: Optional[str] = None


class ProfileIn(BaseModel):
    plan_id: Optional[str] = None
    zip: Optional[str] = None
    state: Optional[str] = None
    ckd_stage: Optional[str] = None
    life_stage: Optional[str] = None      # e.g. pregnant, postpartum, planning, not_applicable
    dialysis: Optional[bool] = None
    transplant: Optional[bool] = None
    timezone: Optional[str] = None            # IANA, e.g. "America/Los_Angeles"
    nutrition_targets: Optional[dict] = None  # from her care team, e.g. {"sodium_mg": 2000, "fluids_ml": 1500}


class SaveIn(BaseModel):
    user_id: str
    tab: str
    title: str
    url: Optional[str] = None
    source_id: Optional[str] = None
    note: Optional[str] = None


@app.get("/tabs")
def list_tabs():
    return [{"id": k, "title": v["title"]} for k, v in agents.TABS.items()]


@app.post("/chat/{tab}")
def chat(tab: str, body: ChatIn):
    if tab not in agents.TABS:
        raise HTTPException(404, f"unknown tab {tab}")
    return orchestrator.handle_chat(store, llm, body.user_id, tab, body.message, body.thread_id)


@app.get("/threads")
def threads(user_id: str, tab: Optional[str] = None):
    return store.list_threads(user_id, tab)


@app.get("/threads/{thread_id}/messages")
def thread_messages(thread_id: str, user_id: str):
    t = store.get_thread(thread_id)
    if not t or t["user_id"] != user_id:
        raise HTTPException(404, "thread not found")
    return store.get_messages(thread_id, with_meta=True)   # includes citations + guardrails per answer


@app.get("/profile/{user_id}")
def get_profile(user_id: str):
    return store.get_profile(user_id)


@app.put("/profile/{user_id}")
def put_profile(user_id: str, body: ProfileIn):
    if body.nutrition_targets is not None:
        bad = [k for k, v in body.nutrition_targets.items()
               if k not in nutrition_mod.KEYS or not isinstance(v, (int, float)) or v <= 0]
        if bad:
            raise HTTPException(422, f"nutrition_targets keys must be in {nutrition_mod.KEYS} with positive numbers")
    if body.timezone is not None:
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(body.timezone)
        except Exception:
            raise HTTPException(422, "timezone must be an IANA name like America/Los_Angeles")
    return store.update_profile(user_id, body.model_dump(exclude_none=True))


class PinIn(BaseModel):
    user_id: str
    message_id: str
    title: Optional[str] = None
    note: Optional[str] = None


@app.post("/pins")
def pin(body: PinIn):
    pid = store.pin_message(body.user_id, body.message_id, body.title, body.note)
    if not pid:
        raise HTTPException(404, "answer not found")
    return {"pin_id": pid}


@app.delete("/pins/{pin_id}")
def unpin(pin_id: str, user_id: str):
    store.unpin(user_id, pin_id)
    return {"ok": True}


@app.get("/board")
def board(user_id: str):
    """Personal board: pinned answers (with citations) + saved resources. Never expires."""
    return store.board(user_id)


@app.get("/retention")
def retention():
    d = config.RETENTION_DAYS
    return {"retention_days": d,
            "description": f"Chats are deleted after {d} days without use. Pin an answer to keep it on your board."}


@app.delete("/threads/{thread_id}")
def delete_thread(thread_id: str, user_id: str):
    if not store.delete_thread(user_id, thread_id):
        raise HTTPException(404, "thread not found")
    return {"ok": True}


@app.delete("/users/{user_id}/data")
def delete_my_data(user_id: str):
    """Erase all chats, pins, saved items, and profile for this user."""
    store.delete_user_data(user_id)
    return {"ok": True}


@app.post("/saved")
def save(body: SaveIn):
    return {"saved_id": store.save_resource(body.user_id, body.tab, body.title, body.url,
                                            body.source_id, body.note)}


@app.get("/saved")
def saved(user_id: str, tab: Optional[str] = None):
    return store.list_saved(user_id, tab)


@app.get("/recall")
def recall(user_id: str, q: str):
    return store.recall(user_id, q)


class RideIn(BaseModel):
    user_id: str
    answers: dict = {}                  # zip, county, coverage, plan, destination, mobility (any subset)


@app.post("/resources/ride")
def ride(body: RideIn):
    """Ride finder step: returns {done: false, ask: {...}} or {done: true, card: {...}}."""
    from .tools import transport
    profile = store.get_profile(body.user_id)
    answers = {k: v for k, v in body.answers.items() if k in
               ("zip", "county", "coverage", "plan", "destination", "mobility")}
    if "zip" not in answers and profile.get("zip"):
        answers["zip"] = profile["zip"]
    return transport.next_step(answers, profile)


# ====================================================================== You tab
@app.get("/you/{user_id}")
def you_overview(user_id: str):
    """Everything the You tab opens with: board, today's doses, today's nutrition."""
    from datetime import datetime
    today = datetime.now(meds_mod._tz(store, user_id)).date().isoformat()
    return {"board": store.board(user_id),
            "medications": store.list_meds(user_id),
            "todays_doses": meds_mod.todays_schedule(store, user_id),
            "nutrition_today": nutrition_mod.day_summary(store, user_id, today),
            "retention": f"Chats are deleted after {config.RETENTION_DAYS} days without use; meal and dose logs "
                         f"after {config.LOG_RETENTION_DAYS} days. Pinned answers and your medication list stay "
                         "until you remove them."}


class MedIn(BaseModel):
    user_id: str
    name: str
    dose_text: str                      # exactly as prescribed, e.g. "1 mg capsule"
    times: list[str]                    # ["08:00", "20:00"], local 24h
    days: Optional[list[int]] = None    # 0=Mon..6=Sun; omit for every day
    reminders: bool = True


class MedUpdate(BaseModel):
    user_id: str
    dose_text: Optional[str] = None
    times: Optional[list[str]] = None
    days: Optional[list[int]] = None
    reminders: Optional[bool] = None
    active: Optional[bool] = None


class DoseIn(BaseModel):
    user_id: str
    scheduled_for: str                  # from todays_doses, e.g. "2026-11-07T08:00"
    status: str                         # taken | skipped


@app.post("/you/meds")
def add_med(body: MedIn):
    try:
        return meds_mod.add(store, body.user_id, body.name, body.dose_text, body.times, body.days, body.reminders)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.patch("/you/meds/{med_id}")
def update_med(med_id: str, body: MedUpdate):
    if body.times and not all(meds_mod.TIME_RE.match(t) for t in body.times):
        raise HTTPException(422, "times must be HH:MM (24h)")
    store.update_med(body.user_id, med_id, **body.model_dump(exclude={"user_id"}))
    return {"ok": True}


@app.delete("/you/meds/{med_id}")
def remove_med(med_id: str, user_id: str):
    store.remove_med(user_id, med_id)
    return {"ok": True}


@app.post("/you/meds/{med_id}/dose")
def log_dose(med_id: str, body: DoseIn):
    if body.status not in ("taken", "skipped"):
        raise HTTPException(422, "status must be taken or skipped")
    if not any(m["med_id"] == med_id for m in store.list_meds(body.user_id, active_only=False)):
        raise HTTPException(404, "medication not found")      # can't mark someone else's dose
    store.log_dose(body.user_id, med_id, body.scheduled_for, body.status)
    return {"ok": True}


@app.get("/you/{user_id}/interactions")
def interactions(user_id: str, drug: Optional[str] = None):
    """Label-based interaction check across her list (+ optional new drug). Always show the notices."""
    return meds_mod.check_interactions(store, user_id, extra=drug)


class MealIn(BaseModel):
    user_id: str
    day: str                            # YYYY-MM-DD (her local date)
    description: str                    # "turkey sandwich on white bread, an apple, 2 cups of coffee"
    meal_type: Optional[str] = None


@app.post("/you/meals")
def add_meal(body: MealIn):
    try:
        return nutrition_mod.log_meal(store, llm, body.user_id, body.day, body.description, body.meal_type)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.delete("/you/meals/{meal_id}")
def remove_meal(meal_id: str, user_id: str):
    store.remove_meal(user_id, meal_id)
    return {"ok": True}


@app.get("/you/{user_id}/nutrition")
def nutrition(user_id: str, day_from: str, day_to: Optional[str] = None):
    return nutrition_mod.day_summary(store, user_id, day_from, day_to)


class VisitPrepIn(BaseModel):
    user_id: str
    visit_type: str                     # pcp | obgyn | nephrology | maternal_fetal_medicine
    concerns: Optional[str] = None      # what she wants to bring up, in her words
    days: int = 30                      # how far back to look at her logs and questions
    save: bool = False                  # also pin the sheet to her board


@app.get("/you/visit-types")
def visit_types():
    return visit_prep_mod.VISIT_TYPES


@app.post("/you/visit-prep")
def visit_prep(body: VisitPrepIn):
    """Visit sheet: facts from her own data (code-built) + a validated question list (model-written)."""
    try:
        sheet = visit_prep_mod.generate(store, llm, body.user_id, body.visit_type, body.concerns,
                                        max(7, min(body.days, 90)))
    except ValueError as e:
        raise HTTPException(422, str(e))
    if body.save:
        sheet["pin_id"] = store.pin_snapshot(
            body.user_id, "you", f"Visit prep: {sheet['visit_label']} ({sheet['generated_at'][:10]})",
            {"kind": "visit_prep", "answer_display": sheet["markdown"], "citations": sheet["citations"],
             "notices": sheet["notices"], "sheet": {k: v for k, v in sheet.items() if k != "markdown"}})
    return sheet
