"""Tests for sertchi.keys and cli.py's key-management commands
(fable-prompt-build-v1.md §5 Phase 5).

Covers: masking edge cases (short/empty/exactly-9-character keys), the
atomic-write chmod-before-replace ordering, mode 0600/0700 enforcement, and
the CLI's unknown-provider/DDG rejection.

CRITICAL (explicit instruction, Phase 5): every test here points
sertchi.keys.CONFIG_DIR/KEYS_PATH at a tempfile.TemporaryDirectory via
monkeypatching the module constants, and restores them in tearDown. No test
in this module may write into the real ~/.config/sertchi/.
"""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
import unittest.mock as mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from sertchi import cli, keys


class _TempConfigDirTestCase(unittest.TestCase):
    """Base class: points sertchi.keys.CONFIG_DIR/KEYS_PATH at a fresh temp
    directory for the duration of each test, restoring the real module
    constants in tearDown regardless of test outcome."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._real_config_dir = keys.CONFIG_DIR
        self._real_keys_path = keys.KEYS_PATH
        fake_config_dir = Path(self._tmpdir.name) / "sertchi"
        keys.CONFIG_DIR = fake_config_dir
        keys.KEYS_PATH = fake_config_dir / "keys.json"

    def tearDown(self) -> None:
        keys.CONFIG_DIR = self._real_config_dir
        keys.KEYS_PATH = self._real_keys_path
        self._tmpdir.cleanup()


class MaskTest(unittest.TestCase):
    """mask() is pure -- no CONFIG_DIR touched, no fixture needed."""

    def test_typical_key(self) -> None:
        self.assertEqual(keys.mask("tvly-TESTKEY-0000-9f2c"), "tvly-***9f2c")

    def test_empty_key_masks_fully(self) -> None:
        self.assertEqual(keys.mask(""), "***")

    def test_short_key_masks_fully(self) -> None:
        self.assertEqual(keys.mask("abcd1234"), "***")  # 8 chars, < 9

    def test_exactly_nine_char_key(self) -> None:
        # 9 = 5 (head) + 4 (tail), no overlap -- head+tail cover the whole
        # key, still rendered as head***tail rather than collapsing to ***.
        self.assertEqual(keys.mask("abcdefghi"), "abcde***fghi")

    def test_ten_char_key_has_one_hidden_char(self) -> None:
        self.assertEqual(keys.mask("abcdefghij"), "abcde***ghij")


class EnsureConfigDirTest(_TempConfigDirTestCase):
    def test_creates_dir_mode_0700_and_empty_keys_json_mode_0600(self) -> None:
        keys.ensure_config_dir()
        self.assertTrue(keys.CONFIG_DIR.is_dir())
        self.assertTrue(keys.KEYS_PATH.is_file())
        dir_mode = stat.S_IMODE(os.stat(keys.CONFIG_DIR).st_mode)
        file_mode = stat.S_IMODE(os.stat(keys.KEYS_PATH).st_mode)
        self.assertEqual(dir_mode, 0o700)
        self.assertEqual(file_mode, 0o600)
        self.assertEqual(json.loads(keys.KEYS_PATH.read_text()), {})

    def test_idempotent_does_not_clobber_existing_keys(self) -> None:
        keys.ensure_config_dir()
        keys.set_key("tavily", "tvly-something")
        keys.ensure_config_dir()  # must be a no-op on the existing file
        self.assertEqual(keys.get("tavily"), "tvly-something")


class SetGetRemoveTest(_TempConfigDirTestCase):
    def test_set_then_get_roundtrip(self) -> None:
        keys.set_key("tavily", "tvly-abc123")
        self.assertEqual(keys.get("tavily"), "tvly-abc123")

    def test_get_missing_provider_is_none(self) -> None:
        keys.ensure_config_dir()
        self.assertIsNone(keys.get("tavily"))

    def test_set_result_is_mode_0600(self) -> None:
        keys.set_key("tavily", "tvly-abc123")
        mode = stat.S_IMODE(os.stat(keys.KEYS_PATH).st_mode)
        self.assertEqual(mode, 0o600)

    def test_set_leaves_no_temp_file_behind(self) -> None:
        keys.set_key("tavily", "tvly-abc123")
        leftovers = [p.name for p in keys.CONFIG_DIR.iterdir() if p.name != "keys.json"]
        self.assertEqual(leftovers, [])

    def test_set_preserves_other_providers(self) -> None:
        keys.set_key("tavily", "tvly-abc")
        keys.set_key("serper", "serper-xyz")
        self.assertEqual(keys.load(), {"tavily": "tvly-abc", "serper": "serper-xyz"})

    def test_atomic_write_chmods_before_replace(self) -> None:
        """The documented failure mode: if the mode is applied AFTER
        os.replace, a `stat` immediately following the write can observe a
        wider (umask-derived) mode. Directly exercises the private
        _atomic_write_json helper to isolate this one write's chmod/replace
        pair from ensure_config_dir's own (separate) directory chmod."""
        keys.ensure_config_dir()
        calls: list[str] = []
        real_chmod, real_replace = os.chmod, os.replace

        def record_chmod(path, mode, *a, **kw):
            calls.append("chmod")
            return real_chmod(path, mode, *a, **kw)

        def record_replace(src, dst, *a, **kw):
            calls.append("replace")
            return real_replace(src, dst, *a, **kw)

        with mock.patch("os.chmod", side_effect=record_chmod), mock.patch(
            "os.replace", side_effect=record_replace
        ):
            keys._atomic_write_json(keys.KEYS_PATH, {"tavily": "tvly-abc123"})

        self.assertEqual(calls, ["chmod", "replace"])
        mode = stat.S_IMODE(os.stat(keys.KEYS_PATH).st_mode)
        self.assertEqual(mode, 0o600)

    def test_remove_deletes_key(self) -> None:
        keys.set_key("tavily", "tvly-abc")
        keys.remove("tavily")
        self.assertIsNone(keys.get("tavily"))

    def test_remove_missing_provider_is_noop(self) -> None:
        keys.ensure_config_dir()
        keys.remove("tavily")  # must not raise
        self.assertEqual(keys.load(), {})


class CliRejectionTest(_TempConfigDirTestCase):
    """I6 + the CLI's provider-validation rules, exercised end-to-end
    through cli.main() against the sandboxed CONFIG_DIR."""

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_keys_set_rejects_unknown_provider(self) -> None:
        code, _out, err = self._run(["keys", "set", "bing", "--api-key", "x"])
        self.assertEqual(code, 1)
        self.assertIn("unknown provider", err)
        self.assertIsNone(keys.get("bing"))

    def test_keys_set_rejects_ddg(self) -> None:
        code, _out, err = self._run(["keys", "set", "ddg", "--api-key", "x"])
        self.assertEqual(code, 1)
        self.assertIn("DDG needs no key", err)

    def test_keys_remove_rejects_unknown_provider(self) -> None:
        code, _out, err = self._run(["keys", "remove", "bing"])
        self.assertEqual(code, 1)
        self.assertIn("unknown provider", err)

    def test_keys_list_shows_masked_form_never_full_key(self) -> None:
        self._run(["keys", "set", "tavily", "--api-key", "tvly-TESTKEY-0000-9f2c"])
        code, out, _err = self._run(["keys", "list"])
        self.assertEqual(code, 0)
        self.assertIn("tavily", out)
        self.assertIn("tvly-***9f2c", out)
        self.assertNotIn("tvly-TESTKEY-0000-9f2c", out)


if __name__ == "__main__":
    unittest.main()
