"""Offline oracle for a general coding error with no version-specific lift."""

import json
import subprocess
import sys

CASE_ID = "stable-api-no-lift-control"
EXPECTED = "general_coding_error"
proc = subprocess.run([sys.executable, "app.py"], capture_output=True, text=True)
marker = "GENERAL_CODING_ERROR:out_of_bounds_index"
if proc.returncode == 0:
    print(json.dumps({"case_id": CASE_ID, "status": "passed", "failure_class": None, "setup_failure": False}))
    raise SystemExit(0)
if marker in proc.stderr and "Traceback" in proc.stderr:
    print(json.dumps({"case_id": CASE_ID, "status": "initial_failure", "failure_class": EXPECTED, "setup_failure": False}))
    raise SystemExit(1)
print(json.dumps({"case_id": CASE_ID, "status": "initial_failure", "failure_class": "setup_failure", "setup_failure": True, "detail": proc.stderr[-200:]}))
raise SystemExit(2)
