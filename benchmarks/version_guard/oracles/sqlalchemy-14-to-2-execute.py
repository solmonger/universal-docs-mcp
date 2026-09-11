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
    execute_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, statement):
        type(self).execute_calls += 1
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
    result = runpy.run_path(str(Path(workspace) / "app.py"), run_name="_version_guard_app")
    app_main = result.get("main")
    if not callable(app_main):
        raise AttributeError("documented application entry point was not defined")
    output = app_main()
    if _Connection.execute_calls != 1 or getattr(output, "scalar_value", None) != 1:
        raise AttributeError("application did not call connection.execute exactly once")


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
