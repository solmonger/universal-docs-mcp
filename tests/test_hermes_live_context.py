"""Explicit live gate: public docs through the CLI and installed Hermes owner."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_hermes_native_hook import (
    _NATIVE_DRIVER,
    CANDIDATE,
    INSTALLED_HERMES_PYTHON,
    native_plugin_config,
)

pytestmark = pytest.mark.live


def test_native_hermes_delivers_fresh_public_docs_on_three_turns(tmp_path):
    if os.environ.get("UNIVERSAL_DOCS_HERMES_LIVE_TESTS") != "1":
        pytest.skip("explicit native/live opt-in required")
    assert INSTALLED_HERMES_PYTHON.is_file(), "installed Hermes interpreter unavailable"
    executable = Path(sys.executable).parent / "universal-docs-context"
    assert executable.is_file(), "context CLI is not installed in the test environment"
    home = tmp_path / "home"
    home.mkdir()
    hermes_home = tmp_path / "hermes-home"
    plugin = hermes_home / "plugins" / "universal-docs-preflight"
    plugin.parent.mkdir(parents=True)
    shutil.copytree(CANDIDATE, plugin)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "ecosystem": "python",
                "package": "requests",
                "selection": "requested",
                "requested_version": "2.32.3",
                "query": "install",
                "freshness_mode": "require_check",
                "deadline_ms": 8000,
                "context_max_bytes": 4000,
            }
        )
    )
    config = native_plugin_config(
        executable, request, timeout_ms=10000, callback_timeout=12
    )
    (hermes_home / "config.yaml").write_text(json.dumps(config))
    driver = tmp_path / "driver.py"
    driver.write_text(_NATIVE_DRIVER)
    env = {
        k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "LC_ALL", "TMPDIR"}
    }
    env.update(
        HOME=str(home),
        HERMES_HOME=str(hermes_home),
        HERMES_SKIP_CONTEXT_FILES="1",
        HERMES_API_KEY="synthetic-native-fixture-key",
        HERMES_API_BASE_URL="http://127.0.0.1:9",
    )
    proc = subprocess.run(
        [str(INSTALLED_HERMES_PYTHON), str(driver), str(home)],
        cwd=home,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    contexts = [t["current_user_api_content"] for t in receipt["turns"]]
    assert len(contexts) == 3
    times = []
    for context in contexts:
        assert "DOCS_PREFLIGHT_STATUS=prepared" in context
        assert "https://pypi.org/pypi/requests/2.32.3/json" in context
        assert "python -m pip install requests" in context
        assert 'Source version binding: "registry_version"' in context
        assert "--- END UNTRUSTED DOCUMENTATION DATA ---" in context
        observed = re.search(r"Fetched at \(Unix seconds\): ([0-9.]+)", context)
        assert observed is not None
        times.append(float(observed[1]))
    assert times[0] < times[1] < times[2]
    assert receipt["system_unchanged"] is True
    receipt.update(
        scope="native_lifecycle_with_live_public_retrieval",
        source_type="public_pypi",
        provider_request_sent=False,
        fetched_times=times,
    )
    destination = os.environ.get("UNIVERSAL_DOCS_HERMES_HOST_EVIDENCE_DIR")
    if destination:
        path = Path(destination)
        path.mkdir(parents=True, exist_ok=True)
        (path / "hermes-live-context.json").write_text(
            json.dumps(receipt, indent=2) + "\n"
        )
