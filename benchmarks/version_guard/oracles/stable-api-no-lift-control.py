"""Blind behavioral oracle for the no-lift control."""
import json
import runpy
import sys
from pathlib import Path

CASE_ID = "stable-api-no-lift-control"
EXPECTED_API = "stable_replacement"

def main() -> int:
    try:
        runpy.run_path(str(Path(sys.argv[1]) / "app.py"), run_name="__main__")
    except IndexError:
        print(json.dumps({"case_id": CASE_ID, "status": "initial_failure", "failure_class": "general_coding_error", "setup_failure": False}, sort_keys=True))
        return 1
    except Exception as exc:
        print(json.dumps({"case_id": CASE_ID, "status": "initial_failure", "failure_class": "setup_failure", "setup_failure": True, "detail": type(exc).__name__}, sort_keys=True))
        return 2
    print(json.dumps({"case_id": CASE_ID, "status": "passed", "failure_class": None, "setup_failure": False}, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
