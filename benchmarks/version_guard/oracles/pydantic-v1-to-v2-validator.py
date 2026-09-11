"""Blind behavioral oracle for pydantic-v1-to-v2-validator; never copied into the agent workspace."""
import json
import runpy
import sys
import types

CASE_ID = 'pydantic-v1-to-v2-validator'
EXPECTED_API = 'field_validator'

def main() -> int:
    workspace = sys.argv[1]
    api = types.ModuleType("api_surface")
    setattr(api, EXPECTED_API, lambda: "replacement")
    sys.modules["api_surface"] = api
    try:
        runpy.run_path(str(__import__("pathlib").Path(workspace) / "app.py"), run_name="__main__")
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
