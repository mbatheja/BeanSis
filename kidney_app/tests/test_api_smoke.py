"""
API smoke test: imports app.api and calls the endpoint functions directly. No network, no model, no reference DB.

Run it before the first live start:   python tests/test_api_smoke.py
It catches import errors, missing dependencies (e.g. tzdata on Windows), and broken endpoint wiring.
If FastAPI/Pydantic/OpenAI aren't installed (CI sandboxes), tiny stand-ins are used so the module still imports.
"""
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DATABASE_URL"] = "sqlite:///:memory:"       # never touch data/app.db
os.environ["LLM_PROVIDER"] = "nebius"
os.environ["NEBIUS_API_KEY"] = "test-key"  # the client object is created at import
os.environ.pop("APP_SHARED_SECRET", None)


def _stub_missing_packages():
    try:
        import fastapi  # noqa: F401
        import pydantic  # noqa: F401
    except ImportError:
        class App:
            def __init__(self, **kw):
                pass

            def _route(self, *a, **kw):
                return lambda fn: fn
            get = post = put = patch = delete = _route

            def middleware(self, *a, **kw):
                return lambda fn: fn

            def add_middleware(self, *a, **kw):
                pass

        class HTTPException(Exception):
            def __init__(self, status_code, detail=None):
                super().__init__(detail)
                self.status_code, self.detail = status_code, detail

        class BaseModel:
            def __init__(self, **kw):
                names = {}
                for klass in reversed(type(self).__mro__):
                    names.update(getattr(klass, "__annotations__", {}))
                for n in names:
                    setattr(self, n, kw.get(n, getattr(type(self), n, None)))
                self._names = list(names)

            def model_dump(self, exclude_none=False, exclude=None):
                d = {n: getattr(self, n) for n in self._names if n not in (exclude or set())}
                return {k: v for k, v in d.items() if v is not None} if exclude_none else d

        fa = types.ModuleType("fastapi")
        fa.FastAPI, fa.HTTPException = App, HTTPException
        mw = types.ModuleType("fastapi.middleware")
        cors = types.ModuleType("fastapi.middleware.cors")
        cors.CORSMiddleware = object
        resp = types.ModuleType("fastapi.responses")
        resp.JSONResponse = dict
        pyd = types.ModuleType("pydantic")
        pyd.BaseModel = BaseModel
        sys.modules.update({"fastapi": fa, "fastapi.middleware": mw, "fastapi.middleware.cors": cors,
                            "fastapi.responses": resp, "pydantic": pyd})
    try:
        import openai  # noqa: F401
    except ImportError:
        op = types.ModuleType("openai")
        op.OpenAI = lambda **kw: object()
        sys.modules["openai"] = op


_stub_missing_packages()
from app import api  # noqa: E402


def test_basic_endpoints():
    assert api.healthz() == {"ok": True}
    assert [t["id"] for t in api.list_tabs()] == ["symptoms", "medications", "resources", "doctors"]
    r = api.retention()
    assert r["retention_days"] == 30 and "30 days" in r["description"]
    assert api.plans(user_id="u1") == []           # reference DB not built: the picker falls back, no crash


def test_profile_you_tab_and_deletion():
    api.put_profile("u1", api.ProfileIn(zip="90024", life_stage="pregnant", timezone="America/Los_Angeles"))
    assert api.get_profile("u1")["zip"] == "90024"
    api.store.add_med("u1", "Prograf", "tacrolimus", "1", "1 mg capsule", ["08:00", "20:00"])
    you = api.you_overview("u1")                  # needs time-zone data: fails on Windows without `tzdata`
    assert set(you) == {"board", "medications", "todays_doses", "nutrition_today", "retention"}
    assert [m["name"] for m in you["medications"]] == ["Prograf"]
    assert len(you["todays_doses"]) == 2 and you["todays_doses"][0]["dose_text"] == "1 mg capsule"
    assert "30 days" in you["retention"]
    api.delete_my_data("u1")
    assert api.get_profile("u1") == {} and api.store.list_meds("u1") == []


def test_ride_flow_first_question_needs_no_database():
    first = api.ride(api.RideIn(user_id="fresh-user", answers={}))
    assert first["done"] is False and first["ask"]["field"] == "zip"


def test_bad_inputs_are_rejected():
    for bad in ({"timezone": "Not/AZone"}, {"nutrition_targets": {"calories": 2000}}):
        try:
            api.put_profile("u2", api.ProfileIn(**bad))
            assert False, f"accepted {bad}"
        except Exception as e:                    # HTTPException (422), real or stand-in
            assert getattr(e, "status_code", None) == 422


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
