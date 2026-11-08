"""
App state = everything users write: profile, per-tab threads, messages, the sources each
answer used, and explicitly saved resources.

Same SQL runs on sqlite (local dev, no setup) and Postgres (deployment, set DATABASE_URL).
Both drivers accept :named parameters, so queries are written once.
"""
import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    username_key TEXT NOT NULL UNIQUE,
    password_salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    onboarding_complete INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS profiles (
    user_id     TEXT PRIMARY KEY,
    data        TEXT NOT NULL,              -- JSON: plan_id, zip, ckd_stage, life_stage, dialysis, transplant
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS threads (
    thread_id   TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    tab         TEXT NOT NULL,              -- symptoms | medications | resources | doctors
    title       TEXT,
    summary     TEXT,                       -- rolling summary of older turns
    summarized_upto INTEGER DEFAULT 0,      -- number of messages folded into summary
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_threads_user_tab ON threads (user_id, tab);
CREATE TABLE IF NOT EXISTS messages (
    message_id  TEXT PRIMARY KEY,
    thread_id   TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    role        TEXT NOT NULL,              -- user | assistant
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    meta        TEXT                        -- JSON: answer_display, citations, notices, guardrails
);
CREATE INDEX IF NOT EXISTS ix_messages_thread ON messages (thread_id, seq);
CREATE TABLE IF NOT EXISTS sources (        -- every source an answer used: powers "what did I find before"
    message_id  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    tab         TEXT NOT NULL,
    source_id   TEXT NOT NULL,              -- e.g. PMID:123, NCT0123, NPI:123, FAERS:drug, DOC:chunk
    title       TEXT,
    url         TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sources_user ON sources (user_id);
CREATE TABLE IF NOT EXISTS saved_resources (
    saved_id    TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    tab         TEXT NOT NULL,
    source_id   TEXT,
    title       TEXT NOT NULL,
    url         TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_saved_user ON saved_resources (user_id);
CREATE TABLE IF NOT EXISTS pins (           -- personal board: snapshots that outlive chat retention
    pin_id      TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    tab         TEXT NOT NULL,
    question    TEXT,
    title       TEXT NOT NULL,
    note        TEXT,
    snapshot    TEXT NOT NULL,              -- JSON: answer_display, citations, notices, guardrails
    thread_id   TEXT,                       -- original chat (may expire; the snapshot stays)
    message_id  TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pins_user ON pins (user_id);
CREATE TABLE IF NOT EXISTS medications (    -- the user's own list, as entered (the app never sets doses)
    med_id      TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    name        TEXT NOT NULL,              -- as typed, e.g. "Prograf"
    ingredient  TEXT,                       -- RxNorm ingredient, e.g. "tacrolimus"
    rxcui       TEXT,
    dose_text   TEXT,                       -- free text as prescribed, e.g. "1 mg capsule"
    times       TEXT NOT NULL,              -- JSON list of local times, e.g. ["08:00", "20:00"]
    days        TEXT,                       -- JSON list of weekdays 0=Mon..6=Sun; NULL = every day
    reminders   INTEGER NOT NULL DEFAULT 1,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_meds_user ON medications (user_id);
CREATE TABLE IF NOT EXISTS dose_log (       -- reminder sent / dose taken / skipped, per scheduled time
    med_id        TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,            -- local ISO datetime of the scheduled dose
    status        TEXT NOT NULL,            -- reminded | taken | skipped
    at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dose_log ON dose_log (med_id, scheduled_for);
CREATE TABLE IF NOT EXISTS meals (
    meal_id     TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    day         TEXT NOT NULL,              -- local date YYYY-MM-DD
    meal_type   TEXT,                       -- breakfast | lunch | dinner | snack
    description TEXT NOT NULL,              -- what the user typed
    items       TEXT,                       -- JSON: parsed foods with grams, nutrients, USDA source
    totals      TEXT,                       -- JSON: sodium_mg, potassium_mg, phosphorus_mg, protein_g, fluids_ml
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_meals_user_day ON meals (user_id, day);
CREATE TABLE IF NOT EXISTS visit_prep_cache (
    user_id     TEXT PRIMARY KEY,
    cache_key   TEXT NOT NULL,
    sheet       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""

PROFILE_FIELDS = ("name", "birth_date", "plan_id", "insurance_other", "zip", "kidney_disease_type",
                  "ckd_stage", "life_stage", "dialysis", "transplant", "transplant_status",
                  "primary_language", "onboarding_complete", "state",
                  "timezone",            # IANA name, e.g. "America/Los_Angeles" (for reminder times)
                  "nutrition_targets")   # optional, entered by the user from her care team / dietitian
STOPWORDS = set("""a an and are as at be but by can do does for from have how i if in is it me my of on or
should so that the this to was what when where which who why will with you your about after any could""".split())


def _now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, url=None):
        self.url = url or config.DATABASE_URL
        self._lock = threading.Lock()
        if self.url.startswith("sqlite"):
            path = self.url.replace("sqlite:///", "", 1)
            if path != ":memory:":
                from pathlib import Path
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._sqlite = sqlite3.connect(path, check_same_thread=False)
            self._sqlite.row_factory = sqlite3.Row
            self._sqlite.executescript(SCHEMA)
            self._engine = None
        else:
            from sqlalchemy import create_engine, text  # pip install sqlalchemy psycopg[binary]
            self._text = text
            self._engine = create_engine(self.url, pool_pre_ping=True)
            with self._engine.begin() as c:
                for stmt in filter(None, (s.strip() for s in SCHEMA.split(";"))):
                    c.execute(text(stmt))

    # ------------------------------------------------------------ low level
    def _exec(self, sql, params=None):
        params = params or {}
        if self._engine is None:
            with self._lock:
                cur = self._sqlite.execute(sql, params)
                self._sqlite.commit()
                return [dict(r) for r in cur.fetchall()] if cur.description else []
        with self._engine.begin() as c:
            res = c.execute(self._text(sql), params)
            return [dict(r) for r in res.mappings().all()] if res.returns_rows else []

    # ------------------------------------------------------------ profile
    def create_user(self, username, username_key, password_salt, password_hash):
        user_id = str(uuid.uuid4())
        self._exec("INSERT INTO users (user_id, username, username_key, password_salt, password_hash, created_at) "
                   "VALUES (:id, :name, :key, :salt, :hash, :now)",
                   {"id": user_id, "name": username, "key": username_key, "salt": password_salt,
                    "hash": password_hash, "now": _now()})
        return user_id

    def get_user_by_username(self, username_key):
        rows = self._exec("SELECT user_id, username, password_salt, password_hash, onboarding_complete "
                          "FROM users WHERE username_key = :key", {"key": username_key})
        return rows[0] if rows else None

    def get_user(self, user_id):
        rows = self._exec("SELECT user_id, username, onboarding_complete FROM users WHERE user_id = :id",
                          {"id": user_id})
        return rows[0] if rows else None

    def set_onboarding_complete(self, user_id, complete=True):
        self._exec("UPDATE users SET onboarding_complete = :complete WHERE user_id = :id",
                   {"complete": int(bool(complete)), "id": user_id})

    def get_profile(self, user_id):
        rows = self._exec("SELECT data FROM profiles WHERE user_id = :u", {"u": user_id})
        return json.loads(rows[0]["data"]) if rows else {}

    def update_profile(self, user_id, updates):
        data = self.get_profile(user_id)
        data.update({k: v for k, v in updates.items() if k in PROFILE_FIELDS})
        self._exec("DELETE FROM profiles WHERE user_id = :u", {"u": user_id})
        self._exec("INSERT INTO profiles VALUES (:u, :d, :t)",
                   {"u": user_id, "d": json.dumps(data), "t": _now()})
        return data

    # ------------------------------------------------------------ threads & messages
    def create_thread(self, user_id, tab, title=None):
        tid, now = str(uuid.uuid4()), _now()
        self._exec("INSERT INTO threads (thread_id, user_id, tab, title, summary, summarized_upto, "
                   "created_at, updated_at) VALUES (:id, :u, :tab, :title, NULL, 0, :now, :now)",
                   {"id": tid, "u": user_id, "tab": tab, "title": title, "now": now})
        return tid

    def get_thread(self, thread_id):
        rows = self._exec("SELECT * FROM threads WHERE thread_id = :id", {"id": thread_id})
        return rows[0] if rows else None

    def list_threads(self, user_id, tab=None):
        sql = "SELECT thread_id, tab, title, updated_at FROM threads WHERE user_id = :u"
        params = {"u": user_id}
        if tab:
            sql += " AND tab = :tab"
            params["tab"] = tab
        return self._exec(sql + " ORDER BY updated_at DESC", params)

    def add_message(self, thread_id, role, content, meta=None):
        seq = self._exec("SELECT COUNT(*) AS n FROM messages WHERE thread_id = :t", {"t": thread_id})[0]["n"]
        mid = str(uuid.uuid4())
        self._exec("INSERT INTO messages VALUES (:id, :t, :seq, :role, :c, :now, :meta)",
                   {"id": mid, "t": thread_id, "seq": seq, "role": role, "c": content, "now": _now(),
                    "meta": json.dumps(meta) if meta else None})
        self._exec("UPDATE threads SET updated_at = :now WHERE thread_id = :t", {"now": _now(), "t": thread_id})
        return mid

    def get_messages(self, thread_id, start=0, with_meta=False):
        rows = self._exec("SELECT message_id, role, content, seq, meta, created_at FROM messages "
                          "WHERE thread_id = :t AND seq >= :s "
                          "ORDER BY seq", {"t": thread_id, "s": start})
        for r in rows:
            meta = r.pop("meta")
            if with_meta and meta:
                r.update(json.loads(meta))
        return rows

    def set_summary(self, thread_id, summary, upto):
        self._exec("UPDATE threads SET summary = :s, summarized_upto = :n WHERE thread_id = :t",
                   {"s": summary, "n": upto, "t": thread_id})

    # ------------------------------------------------------------ sources & saved
    def add_sources(self, message_id, user_id, tab, sources):
        for s in sources:
            self._exec("INSERT INTO sources VALUES (:m, :u, :tab, :sid, :title, :url, :now)",
                       {"m": message_id, "u": user_id, "tab": tab, "sid": s["source_id"],
                        "title": s.get("title"), "url": s.get("url"), "now": _now()})

    def save_resource(self, user_id, tab, title, url=None, source_id=None, note=None):
        sid = str(uuid.uuid4())
        self._exec("INSERT INTO saved_resources VALUES (:id, :u, :tab, :sid, :title, :url, :note, :now)",
                   {"id": sid, "u": user_id, "tab": tab, "sid": source_id, "title": title,
                    "url": url, "note": note, "now": _now()})
        return sid

    def list_saved(self, user_id, tab=None):
        sql, params = "SELECT * FROM saved_resources WHERE user_id = :u", {"u": user_id}
        if tab:
            sql += " AND tab = :tab"
            params["tab"] = tab
        return self._exec(sql + " ORDER BY created_at DESC", params)

    def recall(self, user_id, query, limit=8):
        """Keyword recall over this user's saved items, past sources, and past questions (all tabs)."""
        like = f"%{query.lower()}%"
        saved = self._exec("SELECT 'saved' AS kind, tab, title, url, source_id, created_at FROM saved_resources "
                           "WHERE user_id = :u AND (LOWER(title) LIKE :q OR LOWER(COALESCE(note,'')) LIKE :q) "
                           "ORDER BY created_at DESC LIMIT :n", {"u": user_id, "q": like, "n": limit})
        found = self._exec("SELECT DISTINCT 'previously_found' AS kind, tab, title, url, source_id, created_at "
                           "FROM sources WHERE user_id = :u AND LOWER(COALESCE(title,'')) LIKE :q "
                           "ORDER BY created_at DESC LIMIT :n", {"u": user_id, "q": like, "n": limit})
        asked = self._exec("SELECT 'past_question' AS kind, t.tab, m.content AS title, NULL AS url, "
                           "t.thread_id AS source_id, m.created_at FROM messages m "
                           "JOIN threads t ON t.thread_id = m.thread_id "
                           "WHERE t.user_id = :u AND m.role = 'user' AND LOWER(m.content) LIKE :q "
                           "ORDER BY m.created_at DESC LIMIT :n", {"u": user_id, "q": like, "n": limit})
        pinned = self._exec("SELECT 'pinned' AS kind, tab, title, NULL AS url, pin_id AS source_id, created_at "
                            "FROM pins WHERE user_id = :u AND (LOWER(title) LIKE :q OR LOWER(COALESCE(question,'')) "
                            "LIKE :q OR LOWER(COALESCE(note,'')) LIKE :q OR LOWER(snapshot) LIKE :q) "
                            "ORDER BY created_at DESC LIMIT :n", {"u": user_id, "q": like, "n": limit})
        return (pinned + saved + found + asked)[: limit * 2]

    # ------------------------------------------------------------ personal board
    def pin_message(self, user_id, message_id, title=None, note=None):
        """Snapshot an assistant answer (with its citations) onto the user's board."""
        rows = self._exec("SELECT m.message_id, m.thread_id, m.seq, m.content, m.meta, t.tab, t.user_id "
                          "FROM messages m JOIN threads t ON t.thread_id = m.thread_id "
                          "WHERE m.message_id = :m AND m.role = 'assistant'", {"m": message_id})
        if not rows or rows[0]["user_id"] != user_id:
            return None
        m = rows[0]
        q = self._exec("SELECT content FROM messages WHERE thread_id = :t AND seq = :s AND role = 'user'",
                       {"t": m["thread_id"], "s": m["seq"] - 1})
        question = q[0]["content"] if q else None
        snap = json.loads(m["meta"]) if m["meta"] else {"answer_display": m["content"], "citations": []}
        pid = str(uuid.uuid4())
        self._exec("INSERT INTO pins VALUES (:id, :u, :tab, :q, :title, :note, :snap, :t, :m, :now)",
                   {"id": pid, "u": user_id, "tab": m["tab"], "q": question,
                    "title": title or (question or "Saved answer")[:80], "note": note,
                    "snap": json.dumps(snap), "t": m["thread_id"], "m": message_id, "now": _now()})
        return pid

    def unpin(self, user_id, pin_id):
        self._exec("DELETE FROM pins WHERE pin_id = :p AND user_id = :u", {"p": pin_id, "u": user_id})

    def board(self, user_id):
        pins = self._exec("SELECT * FROM pins WHERE user_id = :u ORDER BY created_at DESC", {"u": user_id})
        for p in pins:
            p["snapshot"] = json.loads(p["snapshot"])
        return {"pinned_answers": pins, "saved_resources": self.list_saved(user_id)}

    def get_visit_prep_cache(self, user_id, cache_key, max_age_hours=24):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        rows = self._exec("SELECT sheet FROM visit_prep_cache WHERE user_id = :u AND cache_key = :k "
                          "AND updated_at >= :cutoff", {"u": user_id, "k": cache_key, "cutoff": cutoff})
        return json.loads(rows[0]["sheet"]) if rows else None

    def set_visit_prep_cache(self, user_id, cache_key, sheet):
        self._exec("DELETE FROM visit_prep_cache WHERE user_id = :u", {"u": user_id})
        self._exec("INSERT INTO visit_prep_cache (user_id, cache_key, sheet, updated_at) "
                   "VALUES (:u, :k, :sheet, :now)",
                   {"u": user_id, "k": cache_key, "sheet": json.dumps(sheet), "now": _now()})

    # ------------------------------------------------------------ "you asked this before"
    @staticmethod
    def _keywords(text):
        return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in STOPWORDS}

    def similar_past(self, user_id, text, exclude_thread=None, k=3, min_score=0.34):
        """Past questions and pins that overlap this question (keyword Jaccard). Cheap and deterministic."""
        words = self._keywords(text)
        if not words:
            return []
        cands = self._exec("SELECT t.thread_id, t.tab, m.content AS text, m.created_at, 'past_chat' AS kind, "
                           "NULL AS pin_id FROM messages m JOIN threads t ON t.thread_id = m.thread_id "
                           "WHERE t.user_id = :u AND m.role = 'user' ORDER BY m.created_at DESC LIMIT 300",
                           {"u": user_id})
        cands += self._exec("SELECT thread_id, tab, COALESCE(question, title) AS text, created_at, 'pinned' AS kind, "
                            "pin_id FROM pins WHERE user_id = :u", {"u": user_id})
        scored, seen = [], set()
        for c in cands:
            if exclude_thread and c["thread_id"] == exclude_thread and c["kind"] == "past_chat":
                continue
            cw = self._keywords(c["text"])
            score = len(words & cw) / len(words | cw) if cw else 0
            key = (c["kind"], c["pin_id"] or c["thread_id"])
            if score >= min_score and key not in seen:
                seen.add(key)
                scored.append({**c, "score": round(score, 2)})
        scored.sort(key=lambda c: (c["kind"] != "pinned", -c["score"]))    # pins first
        return scored[:k]

    # ------------------------------------------------------------ retention & deletion
    def purge_expired(self, now=None):
        """Delete chats untouched for RETENTION_DAYS (same for everyone). Pinned snapshots are never purged."""
        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=config.RETENTION_DAYS)).isoformat()
        old = [r["thread_id"] for r in self._exec("SELECT thread_id FROM threads WHERE updated_at < :c",
                                                  {"c": cutoff})]
        for tid in old:
            self._delete_thread(tid)
        log_cutoff = (now - timedelta(days=config.LOG_RETENTION_DAYS)).isoformat()
        self._exec("DELETE FROM meals WHERE created_at < :c", {"c": log_cutoff})
        self._exec("DELETE FROM dose_log WHERE at < :c", {"c": log_cutoff})
        return len(old)

    def _delete_thread(self, thread_id):
        self._exec("DELETE FROM sources WHERE message_id IN (SELECT message_id FROM messages WHERE thread_id = :t)",
                   {"t": thread_id})
        self._exec("DELETE FROM messages WHERE thread_id = :t", {"t": thread_id})
        self._exec("DELETE FROM threads WHERE thread_id = :t", {"t": thread_id})

    def delete_thread(self, user_id, thread_id):
        t = self.get_thread(thread_id)
        if t and t["user_id"] == user_id:
            self._delete_thread(thread_id)
            return True
        return False

    # ------------------------------------------------------------ You tab: medications & meals
    def add_med(self, user_id, name, ingredient, rxcui, dose_text, times, days=None, reminders=True):
        mid = str(uuid.uuid4())
        self._exec("INSERT INTO medications VALUES (:id, :u, :n, :ing, :rx, :d, :t, :days, :r, 1, :now)",
                   {"id": mid, "u": user_id, "n": name, "ing": ingredient, "rx": rxcui, "d": dose_text,
                    "t": json.dumps(times), "days": json.dumps(days) if days else None,
                    "r": int(bool(reminders)), "now": _now()})
        return mid

    def list_meds(self, user_id, active_only=True):
        rows = self._exec("SELECT * FROM medications WHERE user_id = :u" + (" AND active = 1" if active_only else "")
                          + " ORDER BY created_at", {"u": user_id})
        for r in rows:
            r["times"] = json.loads(r["times"])
            r["days"] = json.loads(r["days"]) if r["days"] else None
            r["reminders"], r["active"] = bool(r["reminders"]), bool(r["active"])
        return rows

    def update_med(self, user_id, med_id, **fields):
        allowed = {"dose_text", "times", "days", "reminders", "active"}
        for k, v in fields.items():
            if k in allowed and v is not None:
                v = json.dumps(v) if k in ("times", "days") else (int(v) if k in ("reminders", "active") else v)
                self._exec(f"UPDATE medications SET {k} = :v WHERE med_id = :m AND user_id = :u",
                           {"v": v, "m": med_id, "u": user_id})

    def remove_med(self, user_id, med_id):
        self._exec("DELETE FROM dose_log WHERE med_id = :m AND user_id = :u", {"m": med_id, "u": user_id})
        self._exec("DELETE FROM medications WHERE med_id = :m AND user_id = :u", {"m": med_id, "u": user_id})

    def log_dose(self, user_id, med_id, scheduled_for, status):
        self._exec("INSERT INTO dose_log VALUES (:m, :u, :s, :st, :now)",
                   {"m": med_id, "u": user_id, "s": scheduled_for, "st": status, "now": _now()})

    def dose_statuses(self, med_id, scheduled_for):
        return {r["status"] for r in self._exec(
            "SELECT status FROM dose_log WHERE med_id = :m AND scheduled_for = :s", {"m": med_id, "s": scheduled_for})}

    def users_with_reminders(self):
        return [r["user_id"] for r in self._exec(
            "SELECT DISTINCT user_id FROM medications WHERE active = 1 AND reminders = 1")]

    def add_meal(self, user_id, day, meal_type, description, items, totals):
        mid = str(uuid.uuid4())
        self._exec("INSERT INTO meals VALUES (:id, :u, :d, :mt, :desc, :items, :tot, :now)",
                   {"id": mid, "u": user_id, "d": day, "mt": meal_type, "desc": description,
                    "items": json.dumps(items), "tot": json.dumps(totals), "now": _now()})
        return mid

    def list_meals(self, user_id, day_from, day_to=None):
        rows = self._exec("SELECT * FROM meals WHERE user_id = :u AND day >= :a AND day <= :b ORDER BY day, created_at",
                          {"u": user_id, "a": day_from, "b": day_to or day_from})
        for r in rows:
            r["items"], r["totals"] = json.loads(r["items"] or "[]"), json.loads(r["totals"] or "{}")
        return rows

    def remove_meal(self, user_id, meal_id):
        self._exec("DELETE FROM meals WHERE meal_id = :m AND user_id = :u", {"m": meal_id, "u": user_id})

    def dose_log_since(self, user_id, since_iso):
        return self._exec("SELECT med_id, scheduled_for, status FROM dose_log WHERE user_id = :u AND at >= :s",
                          {"u": user_id, "s": since_iso})

    def recent_user_questions(self, user_id, since_iso, limit=20):
        rows = self._exec("SELECT t.tab, m.content, m.created_at FROM messages m JOIN threads t "
                          "ON t.thread_id = m.thread_id WHERE t.user_id = :u AND m.role = 'user' "
                          "AND m.created_at >= :s ORDER BY m.created_at DESC LIMIT :n",
                          {"u": user_id, "s": since_iso, "n": limit})
        seen, out = set(), []
        for r in rows:
            key = r["content"].strip().lower()
            if key not in seen:
                seen.add(key)
                out.append(r)
        return out

    def pin_snapshot(self, user_id, tab, title, snapshot, question=None):
        """Pin something that isn't a chat answer (e.g. a visit prep sheet) to the board."""
        pid = str(uuid.uuid4())
        self._exec("INSERT INTO pins VALUES (:id, :u, :tab, :q, :title, NULL, :snap, NULL, NULL, :now)",
                   {"id": pid, "u": user_id, "tab": tab, "q": question, "title": title,
                    "snap": json.dumps(snapshot), "now": _now()})
        return pid

    def delete_user_data(self, user_id):
        """Erase everything for a user: chats, sources, pins, saved items, profile."""
        for tid in [r["thread_id"] for r in self._exec("SELECT thread_id FROM threads WHERE user_id = :u",
                                                         {"u": user_id})]:
            self._delete_thread(tid)
        for table in ("pins", "saved_resources", "sources", "medications", "dose_log", "meals", "profiles",
                      "visit_prep_cache"):
            self._exec(f"DELETE FROM {table} WHERE user_id = :u", {"u": user_id})
