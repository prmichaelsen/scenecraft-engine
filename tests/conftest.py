"""Ensure tests import the worktree's own `src/` — not the editable-install path
in the shared venv that still points at the main-project checkout. This is a
test-only adjustment; nothing in production cares.
"""

from __future__ import annotations

import sys
from pathlib import Path

_WORKTREE_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_WORKTREE_SRC) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_SRC))
