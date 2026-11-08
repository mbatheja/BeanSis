"""Persistent TTL cache for HTTP GETs (sqlite file, thread-safe)."""
import hashlib
import json
import sqlite3
import threading
import time

import requests

from . import config

_lock = threading.Lock()
_db = None


def _get_db():
    global _db
    if _db is None:
        config.CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
        _db = sqlite3.connect(config.CACHE_DB, check_same_thread=False)
        _db.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT, expires REAL)")
    return _db


def _key(prefix, url, params):
    return prefix + hashlib.sha256((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()


def _read(key):
    with _lock:
        row = _get_db().execute("SELECT v, expires FROM cache WHERE k = ?", (key,)).fetchone()
    if row and row[1] > time.time():
        return json.loads(row[0])
    return None


def _write(key, value, ttl):
    with _lock:
        db = _get_db()
        db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)", (key, json.dumps(value), time.time() + ttl))
        db.commit()


def cached_get_json(url, params=None, ttl=86400, timeout=20):
    """GET and parse JSON, served from cache when fresh. A 404 (openFDA's "no matches") caches as {}."""
    key = _key("j:", url, params)
    hit = _read(key)
    if hit is not None:
        return hit
    r = requests.get(url, params=params, timeout=timeout)
    if r.status_code == 404:
        data = {}
    else:
        r.raise_for_status()
        data = r.json()
    _write(key, data, ttl)
    return data


def cached_get_text(url, params=None, ttl=86400, timeout=20):
    """Same as cached_get_json for XML/text responses (PubMed efetch)."""
    key = _key("t:", url, params)
    hit = _read(key)
    if hit is not None:
        return hit
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    _write(key, r.text, ttl)
    return r.text
