"""
Request security for the deployed API.

- Shared secret: every request must carry X-App-Secret matching APP_SHARED_SECRET. Only the Base44
  "kidney-api" proxy function knows it, and that function sets user_id itself from the logged-in user,
  so the API can trust user_id. /healthz is the only open route.
- Rate limit: per-user caps on endpoints that call a model (chat, meal logging, visit prep), so one
  account can't run up the bill.
"""
import hmac
import os
import threading
import time
from collections import defaultdict, deque

SECRET = os.getenv("APP_SHARED_SECRET", "")
OPEN_PATHS = {"/healthz"}
COSTLY_PREFIXES = ("/chat/", "/you/meals", "/you/visit-prep")
LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "12"))
LIMIT_PER_DAY = int(os.getenv("RATE_LIMIT_PER_DAY", "300"))


def secret_ok(path, header_value, secret=None):
    secret = SECRET if secret is None else secret
    if path in OPEN_PATHS:
        return True
    if not secret:                       # local dev: no secret configured -> open (never deploy like this)
        return True
    return bool(header_value) and hmac.compare_digest(header_value, secret)


class RateLimiter:
    def __init__(self, per_min=LIMIT_PER_MIN, per_day=LIMIT_PER_DAY):
        self.per_min, self.per_day = per_min, per_day
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, user_id, now=None):
        now = now or time.time()
        with self._lock:
            q = self._hits[user_id]
            while q and q[0] < now - 86400:
                q.popleft()
            last_min = sum(1 for t in q if t >= now - 60)
            if last_min >= self.per_min or len(q) >= self.per_day:
                return False
            q.append(now)
            return True


def is_costly(path):
    return path.startswith(COSTLY_PREFIXES)
