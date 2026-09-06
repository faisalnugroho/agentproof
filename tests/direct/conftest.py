"""Direct-mode gltest fixtures for AgentProof tests."""
import sys
from pathlib import Path

# Make project root importable so tests/helpers.py can be imported from
# anywhere pytest is launched.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
