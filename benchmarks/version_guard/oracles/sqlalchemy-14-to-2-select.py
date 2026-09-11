"""Blind synthetic oracle for positional selectable construction."""
from __future__ import annotations

import json
import runpy
import sys
import types
from pathlib import Path

CASE_ID = "sqlalchemy-14-to-2-select"
EXPECTED_API = "select"


class _Column:
    def __init__(self, name):
        self.name = name


class _Table:
    def __init__(self, name, *columns):
        self.name = name
        self.c = types.SimpleNamespace(**{column.name: column for column in columns})


def _select(*columns):
    if len(columns) == 1 and isinstance(columns[0], list):
        raise TypeError("select() takes column expressions positionally")
    if not columns or not all(isinstance(column, _Column) for column in columns):
        raise TypeError("select() requires column expressions")
    return ("select", tuple(column.name for column in columns))


def _run(workspace: str) -> None:
    module = types.ModuleType("sqlalchemy")
    module.column = _Column
    module.table = _Table
    module.select = _select
    sys.modules["sqlalchemy"] = module
    result = runpy.run_path(str(Path(workspace) / "app.py"), run_name="__main__")
    if result.get("main", lambda: None)() != ("select", ("id",)):
        raise TypeError("select contract was not used")


def main() -> int:
    try:
        _run(sys.argv[1])
    except TypeError:
        print(json.dumps({"case_id": CASE_ID, "status": "initial_failure", "failure_class": "wrong_version_api", "setup_failure": False}, sort_keys=True))
        return 1
    except Exception as exc:
        print(json.dumps({"case_id": CASE_ID, "status": "initial_failure", "failure_class": "setup_failure", "setup_failure": True, "detail": type(exc).__name__}, sort_keys=True))
        return 2
    print(json.dumps({"case_id": CASE_ID, "status": "passed", "failure_class": None, "setup_failure": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
