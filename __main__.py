"""Delegates ``python3 -m sertchi ...`` to ``sertchi.cli.main()`` (SKETCH.md
§7's stated invocation form).

Not in fable-prompt-build-v1.md §4's file tree -- flagged, not invented: it
exists only because §7 documents the ``python3 -m sertchi`` form, alongside
§4's ``python3 -m sertchi.cli`` form. Both must work identically; this file
is the three-line shim that makes the first one true without duplicating
``cli.py``'s logic.

Importing ``sertchi.cli`` runs ``sertchi/__init__.py`` first (normal package
import order) regardless of which of the two invocation forms is used, so
this file behaves the same today, when ``__init__.py`` is a docstring-only
placeholder, as it will once Phase 7 fills ``__init__.py`` with the public
async API (``search()``/``multi_search()``, which import ``httpx`` and the
provider adapters). This module itself never imports anything beyond
``.cli`` -- it does not need to change when ``__init__.py`` grows.
"""

from .cli import main

raise SystemExit(main())
