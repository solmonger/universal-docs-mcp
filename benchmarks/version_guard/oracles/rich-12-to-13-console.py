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

    def __init__(self):
        self.messages = []
        self.instances.append(self)

    def print(self, *values):
        self.messages.append(" ".join(str(value) for value in values))


def _run(workspace: str) -> None:
    root = types.ModuleType("rich")
    console_module = types.ModuleType("rich.console")
    console_module.Console = _Console
    root.console = console_module
    sys.modules["rich"] = root
    sys.modules["rich.console"] = console_module
    result = runpy.run_path(str(Path(workspace) / "app.py"), run_name="__main__")
    console_class = console_module.Console
    if not callable(getattr(console_module, EXPECTED_API, None)) or not console_class.instances or console_class.instances[-1].messages != ["Hello World!"]:
        raise AssertionError("console contract failed")
    if result.get("main", lambda: None)() is not None:
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
