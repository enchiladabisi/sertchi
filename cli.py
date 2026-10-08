"""Sertchi CLI -- stdlib ``argparse`` only (I1). Key-management sub-commands
(fable-prompt-build-v1.md §5 Phase 5).

Invoked as ``python3 -m sertchi.cli ...`` (SKETCH.md §4's stated form) or,
via ``sertchi/__main__.py``, as ``python3 -m sertchi ...`` (SKETCH.md §7's
stated form). Both forms delegate to ``main()`` here and must behave
identically.

I6 -- ``keys set`` never echoes what was typed (``getpass.getpass()``);
``keys list`` prints only the masked form (``keys.mask()``); no full key
ever reaches stdout, stderr, or an exception message anywhere in this
module.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import Sequence

from . import keys
from .types import Provider

_ALL_PROVIDER_NAMES = tuple(p.value for p in Provider)
_KEYED_PROVIDER_NAMES = tuple(p.value for p in Provider if p is not Provider.DDG)


def _cmd_keys_set(args: argparse.Namespace) -> int:
    provider = args.provider
    if provider not in _ALL_PROVIDER_NAMES:
        print(
            f"error: unknown provider {provider!r} "
            f"(known: {', '.join(_ALL_PROVIDER_NAMES)})",
            file=sys.stderr,
        )
        return 1
    if provider == Provider.DDG.value:
        print("error: DDG needs no key -- it is keyless by design", file=sys.stderr)
        return 1

    api_key = args.api_key
    if api_key is None:
        # I6: getpass never echoes the typed characters to the terminal.
        api_key = getpass.getpass(f"API key for {provider}: ")
    if not api_key:
        print("error: no key provided", file=sys.stderr)
        return 1

    keys.set_key(provider, api_key)
    print(f"{provider}: key stored")  # I6: never the key itself, masked or not.
    return 0


def _cmd_keys_list(args: argparse.Namespace) -> int:
    stored = keys.load()
    for provider in _KEYED_PROVIDER_NAMES:
        api_key = stored.get(provider)
        if api_key:
            print(f"{provider}  configured  {keys.mask(api_key)}")
        else:
            print(f"{provider}  not configured")
    return 0


def _cmd_keys_remove(args: argparse.Namespace) -> int:
    provider = args.provider
    if provider not in _ALL_PROVIDER_NAMES:
        print(
            f"error: unknown provider {provider!r} "
            f"(known: {', '.join(_ALL_PROVIDER_NAMES)})",
            file=sys.stderr,
        )
        return 1
    keys.remove(provider)
    print(f"{provider}: key removed")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sertchi", description="Sertchi -- shared multi-provider search utility."
    )
    top = parser.add_subparsers(dest="command", required=True)

    keys_parser = top.add_parser("keys", help="Manage provider API keys.")
    keys_sub = keys_parser.add_subparsers(dest="keys_command", required=True)

    set_parser = keys_sub.add_parser("set", help="Store a provider's API key.")
    set_parser.add_argument(
        "provider", help=f"one of: {', '.join(_KEYED_PROVIDER_NAMES)}"
    )
    set_parser.add_argument(
        "--api-key",
        default=None,
        help="API key value; prompted via getpass (never echoed) if omitted",
    )
    set_parser.set_defaults(func=_cmd_keys_set)

    list_parser = keys_sub.add_parser(
        "list", help="List configured providers (masked keys only)."
    )
    list_parser.set_defaults(func=_cmd_keys_list)

    remove_parser = keys_sub.add_parser(
        "remove", help="Remove a provider's stored API key."
    )
    remove_parser.add_argument(
        "provider", help=f"one of: {', '.join(_KEYED_PROVIDER_NAMES)}"
    )
    remove_parser.set_defaults(func=_cmd_keys_remove)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Scaffold ~/.config/sertchi/ on first run (SKETCH.md §7) -- idempotent,
    # also happens lazily on first keys.load()/get() so this is a belt-and-
    # braces call, not the only path that creates the directory.
    keys.ensure_config_dir()
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
