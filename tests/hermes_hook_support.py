"""The Hermes plugin consumes one neutral, validated delivery frame."""

import importlib.util
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PLUGIN = (
    Path(__file__).parents[1]
    / "examples/hermes/universal-docs-preflight/hermes_hook.py"
)


def load_plugin(profile_home=None):
    spec = importlib.util.spec_from_file_location("docs_hermes_bridge", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    host_api = SimpleNamespace(
        get_hermes_home=lambda: profile_home or Path.home() / ".hermes"
    )
    with patch.dict(sys.modules, {"hermes_constants": host_api}):
        spec.loader.exec_module(module)
    return module


class Context:
    def __init__(self, settings):
        self.settings = settings
        self.hooks = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, callback):
        self.hooks[name] = callback


def make_context(tmp_path, script_body=None):
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "package": "requests",
                "ecosystem": "python",
                "selection": "requested",
                "requested_version": "2.32.3",
                "freshness_mode": "require_check",
                "query": "install",
                "deadline_ms": 1000,
            }
        )
    )
    log = tmp_path / "calls.jsonl"
    executable = tmp_path / "context-cli"
    body = script_body or (
        "import json,sys,os\n"
        f"with open({str(log)!r}, 'a') as f: f.write(json.dumps({{'argv':sys.argv[1:],'env':dict(os.environ)}})+'\\n')\n"
        "print(json.dumps({'schema':'universal-docs.context/v1','status':'prepared',"
        "'context':'UNIVERSAL-DOCS PREFLIGHT CONTEXT PACKET v1\\nSource URL: fixture://docs\\nFixture documentation', 'error':None}))\n"
    )
    executable.write_text("#!" + sys.executable + "\n" + body)
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return Context(
        {
            "executable": str(executable),
            "request_file": str(request),
            "timeout_ms": 1000,
        }
    ), log
