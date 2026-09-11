"""Blind synthetic oracle for connection-owned textual execution."""
from __future__ import annotations

import json
import runpy
import sys
import types
from pathlib import Path

CASE_ID = "sqlalchemy-14-to-2-execute"
EXPECTED_API = "execute"


class _Result:
    scalar_value = 1


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, statement):
        if not isinstance(statement, _Text):
            raise TypeError("textual statements must use text()")
        return _Result()


class _Engine:
    def connect(self):
        return _Connection()


class _Text(str):
    pass


def _run(workspace: str) -> None:
    module = types.ModuleType("sqlalchemy")
    module.create_engine = lambda url: _Engine()
    module.text = lambda statement: _Text(statement)
    sys.modules["sqlalchemy"] = module
    result = runpy.run_path(str(Path(workspace) / "app.py"), run_name="__main__")
    if not callable(getattr(_Connection, EXPECTED_API, None)):
        raise AttributeError("documented connection method was not defined")
    if result.get("main", lambda: None)() .scalar_value != 1:
        raise AssertionError("connection execution contract failed")


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
