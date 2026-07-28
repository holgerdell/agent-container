"""Load the extensionless `agent` script as an importable module for tests."""

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_SCRIPT = REPO_ROOT / "agent"

_loader = SourceFileLoader("agent_cli", str(AGENT_SCRIPT))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
assert _spec
agent = importlib.util.module_from_spec(_spec)
_loader.exec_module(agent)
