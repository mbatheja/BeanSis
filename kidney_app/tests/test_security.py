"""Deployment security helpers + proxy route allowlist sanity checks."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import security  # noqa: E402


def test_secret_and_rate_limit():
    assert security.secret_ok("/healthz", None, secret="s")
    assert not security.secret_ok("/chat/symptoms", None, secret="s")
    assert not security.secret_ok("/chat/symptoms", "nope", secret="s")
    assert security.secret_ok("/chat/symptoms", "s", secret="s")
    rl = security.RateLimiter(per_min=2, per_day=3)
    assert [rl.allow("u", now=10), rl.allow("u", now=11), rl.allow("u", now=12)] == [True, True, False]
    assert rl.allow("u", now=100) and not rl.allow("u", now=200)


def test_proxy_routes_cover_every_api_route_users_need():
    ts = (Path(__file__).resolve().parent.parent / "base44/functions/kidney-api/entry.ts").read_text()
    api = (Path(__file__).resolve().parent.parent / "app/api.py").read_text()
    api_routes = set(re.findall(r'@app\.(get|post|put|patch|delete)\("([^"]+)"', api))
    internal = {("get", "/healthz")}                         # not exposed through the proxy
    for method, path in api_routes - internal:
        probe = re.sub(r"\{tab\}", "medications", path)
        probe = re.sub(r"\{(?!user_id\})[a-z_]+\}", "abc-123", probe)   # other ids first...
        probe = probe.replace("{user_id}", "{me}")                    # ...then the user placeholder
        assert method.upper() in ts and any(
            re.fullmatch(p, probe) for p in re.findall(r'pattern: /\^(.+?)\$/', ts)
            + [p.replace("${ID}", "[A-Za-z0-9_-]{1,64}") for p in re.findall(r'new RegExp\(`\^(.+?)\$`\)', ts)]
        ), f"proxy doesn't allow {method.upper()} {path}"


def test_policy_defaults_match_decisions():
    """Guards decisions the team made: 30-day chats, 90-day logs, California time for reminders."""
    from app import config
    assert config.RETENTION_DAYS == 30 and config.LOG_RETENTION_DAYS == 90
    assert config.DEFAULT_TIMEZONE == "America/Los_Angeles"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
