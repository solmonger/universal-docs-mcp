"""Offline oracle: classify only the declared version mismatch."""
import json
import subprocess
import sys
CASE_ID = 'urllib3-1-to-2-timeout'
EXPECTED = "wrong_version_api"
proc = subprocess.run([sys.executable, "app.py"], capture_output=True, text=True)
marker = "WRONG_VERSION_API:urllib3:2.2.1:TimeoutLegacy"
if proc.returncode == 0:
    print(json.dumps({"case_id": CASE_ID, "status": "passed", "failure_class": None, "setup_failure": False}))
    raise SystemExit(0)
if marker in proc.stderr and "Traceback" in proc.stderr:
    print(json.dumps({"case_id": CASE_ID, "status": "initial_failure", "failure_class": EXPECTED, "setup_failure": False}))
    raise SystemExit(1)
print(json.dumps({"case_id": CASE_ID, "status": "initial_failure", "failure_class": "setup_failure", "setup_failure": True, "detail": proc.stderr[-200:]}))
raise SystemExit(2)
