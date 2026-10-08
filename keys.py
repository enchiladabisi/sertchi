"""Centralized API-key storage for Sertchi (fable-prompt-build-v1.md §5
Phase 5).

Keys live OUTSIDE this project's source tree, in ONE machine-wide location:
``~/.config/sertchi/keys.json``. This is a genuine reversal of SKETCH.md's
original round-4 per-copy design (``sertchi/etc/keys.json``) -- the user
asked for a centralized store on 2026-08-20 (Phase 0's decision) so every
copy of Sertchi on this machine -- a centralized install or a future
vendored copy -- reads and writes the SAME key for a given provider,
configured once. ``sertchi/etc/`` as a concept no longer exists; there is
nothing per-copy left for a consuming project's git repo to accidentally
track, so no ``.gitignore`` entry is needed for this file's output.

I6 -- keys are never logged, echoed, printed, or put in a URL. This module
never writes a key anywhere but ``keys.json`` itself; callers (``cli.py``,
the provider adapters reading via ``get()``) are responsible for not
leaking it further.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "sertchi"
KEYS_PATH = CONFIG_DIR / "keys.json"


def ensure_config_dir() -> None:
    """Create CONFIG_DIR (mode 0700) and an empty keys.json (mode 0600) if
    either is missing. Idempotent -- safe to call on every CLI invocation
    (``cli.main()``) and lazily on every read (``load()``)."""
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    # mkdir's `mode` is subject to umask and is a no-op if the directory
    # already existed under different permissions -- chmod explicitly so a
    # pre-existing (or umask-widened) directory still ends up exactly 0700.
    os.chmod(CONFIG_DIR, 0o700)
    if not KEYS_PATH.exists():
        _atomic_write_json(KEYS_PATH, {})


def _atomic_write_json(path: Path, data: dict[str, str]) -> None:
    """Write ``data`` to ``path`` atomically: a temp file created inside
    ``path``'s own directory (same filesystem, so ``os.replace`` is atomic),
    ``chmod``'d to 0600 BEFORE ``os.replace``. Getting this order backwards
    is the documented failure mode -- a mode applied to a file that is
    already live at its final name can lose to a concurrent reader that
    opened it in the window before the chmod landed, and on some setups the
    rename itself doesn't preserve a mode applied only afterward. Chmod
    first, then replace, closes that window."""
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp_name, 0o600)  # before replace -- see docstring above.
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def load() -> dict[str, str]:
    """Return the full ``provider -> api_key`` mapping. Lazily scaffolds the
    config dir/file on first read via ``ensure_config_dir()``, which is
    itself idempotent."""
    ensure_config_dir()
    try:
        with KEYS_PATH.open("r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def get(provider: str) -> str | None:
    """The configured key for ``provider``, or ``None`` if unset. This is
    the lazy, call-time read the keyed adapters (``providers/tavily.py`` etc,
    Phase 3/4) were structured to swap in -- never called at import time."""
    return load().get(provider)


def set_key(provider: str, api_key: str) -> None:
    """Store ``api_key`` for ``provider``. Atomic -- see
    ``_atomic_write_json``."""
    ensure_config_dir()
    data = load()
    data[provider] = api_key
    _atomic_write_json(KEYS_PATH, data)


def remove(provider: str) -> None:
    """Delete ``provider``'s stored key, if any. No error if it was never
    configured."""
    ensure_config_dir()
    data = load()
    if provider in data:
        del data[provider]
        _atomic_write_json(KEYS_PATH, data)


def mask(key: str) -> str:
    """First 5 characters (or fewer, if the key is shorter) + ``***`` + last
    4, e.g. ``tvly-***9f2c`` (genchat PLAN.md D4's convention, per
    fable-prompt-build-v1.md §5 Phase 5's decision). A key shorter than 9
    characters (5 + 4, the widest un-overlapping head+tail) masks to
    ``***`` entirely -- there isn't enough length to show a head and a tail
    without exposing most or all of the real key."""
    if len(key) < 9:
        return "***"
    return f"{key[:5]}***{key[-4:]}"
