"""Blind synthetic oracle for documented field-validation semantics."""
from __future__ import annotations

import json
import runpy
import sys
import types
from pathlib import Path

CASE_ID = "pydantic-v1-to-v2-validator"
EXPECTED_API = "field_validator"


class _ValidationInfo:
    config = {"title": "Synthetic"}


_VALIDATOR_STATE = {"decorator_calls": 0}


def _field_validator(field_name):
    _VALIDATOR_STATE["decorator_calls"] += 1

    def decorate(function):
        function._validated_field = field_name
        return function
    return decorate
class _ModelMeta(type):
    def __new__(mcls, name, bases, namespace):
        validators = [value for value in namespace.values() if hasattr(value, "_validated_field")]
        cls = super().__new__(mcls, name, bases, namespace)
        cls._validators = validators
        return cls


class _BaseModel(metaclass=_ModelMeta):
    def __init__(self, **values):
        for validator in self._validators:
            field_name = validator._validated_field
            values[field_name] = validator(self.__class__, values[field_name], _ValidationInfo())
        self.__dict__.update(values)


def _run(workspace: str) -> None:
    module = types.ModuleType("pydantic")
    module.BaseModel = _BaseModel
    module.ValidationInfo = _ValidationInfo
    module.field_validator = _field_validator
    sys.modules["pydantic"] = module
    result = runpy.run_path(str(Path(workspace) / "app.py"), run_name="_version_guard_app")
    model = result.get("User")
    app_main = result.get("main")
    if model is None or not callable(app_main):
        raise AttributeError("documented validator integration was not defined")
    output = app_main()
    if _VALIDATOR_STATE["decorator_calls"] != 1 or output != "Ada":
        raise AttributeError("application did not use field_validator exactly once")


def main() -> int:
    try:
        _run(sys.argv[1])
    except ImportError as exc:
        if "validator" in str(exc):
            print(json.dumps({"case_id": CASE_ID, "status": "initial_failure", "failure_class": "wrong_version_api", "setup_failure": False}, sort_keys=True))
            return 1
        print(json.dumps({"case_id": CASE_ID, "status": "initial_failure", "failure_class": "setup_failure", "setup_failure": True, "detail": type(exc).__name__}, sort_keys=True))
        return 2
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
