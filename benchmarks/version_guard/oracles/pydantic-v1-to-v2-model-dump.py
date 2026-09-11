"""Blind synthetic oracle for the documented model-method shape."""
from __future__ import annotations

import json
import runpy
import sys
import types
from pathlib import Path

CASE_ID = "pydantic-v1-to-v2-model-dump"
EXPECTED_API = "model_dump"


class _BaseModel:
    model_dump_calls = 0

    def __init__(self, **values):
        self.__dict__.update(values)

    def model_dump(self):
        _BaseModel.model_dump_calls += 1
        return dict(self.__dict__)


def _run(workspace: str) -> None:
    module = types.ModuleType("pydantic")
    module.BaseModel = _BaseModel
    sys.modules["pydantic"] = module
    result = runpy.run_path(str(Path(workspace) / "app.py"), run_name="_version_guard_app")
    model = result.get("User")
    app_main = result.get("main")
    if model is None or not callable(app_main):
        raise AttributeError("documented model integration was not defined")
    output = app_main()
    if _BaseModel.model_dump_calls != 1 or output != {"name": "Ada"}:
        raise AttributeError("application did not call model_dump exactly once")


def main() -> int:
    try:
        _run(sys.argv[1])
    except AttributeError:
        print(json.dumps({"case_id": CASE_ID, "status": "initial_failure", "failure_class": "wrong_version_api", "setup_failure": False}, sort_keys=True))
        return 1
    except Exception as exc:
        print(json.dumps({"case_id": CASE_ID, "status": "initial_failure", "failure_class": "setup_failure", "setup_failure": True, "detail": type(exc).__name__}, sort_keys=True))
        return 2
    print(json.dumps({"case_id": CASE_ID, "status": "passed", "failure_class": None, "setup_failure": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
