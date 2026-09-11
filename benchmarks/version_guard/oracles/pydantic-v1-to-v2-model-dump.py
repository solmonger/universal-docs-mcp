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
    def __init__(self, **values):
        self.__dict__.update(values)

    def model_dump(self):
        return dict(self.__dict__)


def _run(workspace: str) -> None:
    module = types.ModuleType("pydantic")
    module.BaseModel = _BaseModel
    sys.modules["pydantic"] = module
    result = runpy.run_path(str(Path(workspace) / "app.py"), run_name="__main__")
    model = result.get("User")
    if model is None or not callable(getattr(model, EXPECTED_API, None)):
        raise AttributeError("documented model method was not defined")
    if model(name="Ada").model_dump() != {"name": "Ada"}:
        raise AssertionError("model serialization contract failed")


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
