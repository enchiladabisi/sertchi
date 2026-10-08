"""Phase 0 smoke test: the package imports cleanly under test discovery.

Run from ~/Documents/CODE via:
    python3 -m unittest discover -s sertchi/tests -t . < /dev/null
No sys.path hack is needed here — ``-t .`` puts ~/Documents/CODE on the
import path, so ``import sertchi`` resolves the same way it will for any
real caller (absolute import, per Phase 0's import-style decision).
"""

from __future__ import annotations

import unittest


class SmokeTest(unittest.TestCase):
    def test_import_sertchi(self) -> None:
        import sertchi  # noqa: F401 — presence of the import is the assertion
