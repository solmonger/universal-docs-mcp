"""Blind behavioral oracle for click-7-to-8-parameter; never copied into the agent workspace."""
import json
import runpy
import sys
import types

CASE_ID = 'click-7-to-8-parameter'
EXPECTED_API = 'option'

def main() -> int:
    workspace = sys.argv[1]
    calls = []
    api = types.ModuleType("api_surface")
    setattr(api, EXPECTED_API, lambda: calls.append(True) or "replacement")
    sys.modules["api_surface"] = api
    try:
        runpy.run_path(str(__import__("pathlib").Path(workspace) / "app.py"), run_name="__main__")
        if calls != [True]:
            raise AttributeError("expected API was not called exactly once")
    except AttributeError:
        result = {"case_id": CASE_ID, "status": "initial_failure", "failure_class": "wrong_version_api", "setup_failure": False}
        print(json.dumps(result, sort_keys=True))
        return 1
    except Exception as exc:
        print(json.dumps({"case_id": CASE_ID, "status": "initial_failure", "failure_class": "setup_failure", "setup_failure": True, "detail": type(exc).__name__}, sort_keys=True))
        return 2
    print(json.dumps({"case_id": CASE_ID, "status": "passed", "failure_class": None, "setup_failure": False}, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
