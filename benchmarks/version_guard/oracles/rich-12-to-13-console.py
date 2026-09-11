"""Blind synthetic oracle for the documented rich console import and call shape."""
from __future__ import annotations

import json
import runpy
import sys
import types
from pathlib import Path

CASE_ID = "rich-12-to-13-console"
EXPECTED_API = "Console"


class _Console:
    instances = []
    print_calls = 0

    def __init__(self):
        self.messages = []
        self.instances.append(self)

    def print(self, *values):
        type(self).print_calls += 1
        self.messages.append(" ".join(str(value) for value in values))


def _run(workspace: str) -> None:
    root = types.ModuleType("rich")
    console_module = types.ModuleType("rich.console")
    console_module.Console = _Console
    root.console = console_module
    sys.modules["rich"] = root
    sys.modules["rich.console"] = console_module
    result = runpy.run_path(str(Path(workspace) / "app.py"), run_name="_version_guard_app")
    app_main = result.get("main")
    if not callable(app_main):
        raise AttributeError("documented console integration was not defined")
    output = app_main()
    console_class = console_module.Console
    if _Console.print_calls != 1 or not console_class.instances or console_class.instances[-1].messages != ["Hello World!"]:
        raise AttributeError("application did not call Console.print exactly once")
    if output is not None:
        raise AssertionError("fixture should write through the console")


def main() -> int:
    try:
        _run(sys.argv[1])
    except ImportError as exc:
        if "legacy_output" in str(exc):
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
