"""Native Claude/Kimi Glob/Grep semantics against the real search worker.

Only the SSH transport is substituted: each call pipes the production
``search_worker.py`` payload into a local ``python3 -I`` process, exactly as
the remote worker runs it. Tests that need ripgrep use the binary found on
PATH (or ``REMOTE_DEV_TEST_RG``) and skip when it is unavailable.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import remote_dev.core.search_ops as search_ops
from remote_dev.core.endpoint import Endpoint
from remote_dev.mcp.profiles_claude import _claude_glob, _claude_grep
from remote_dev.mcp.profiles_kimi import _kimi_glob, _kimi_grep


def _rg_path() -> str | None:
    configured = os.environ.get("REMOTE_DEV_TEST_RG")
    if configured and Path(configured).is_file():
        return configured
    return shutil.which("rg")


RG = _rg_path()


sys.path.insert(0, str(Path(__file__).parent))
from _worker_runner import run_worker_script


class _Transport:
    """Run the shipped worker locally; no protocol or payload is simulated."""

    def __init__(self, cwd: Path, *, rg: str | None = RG, path: str | None = None) -> None:
        self.cwd = cwd
        self.rg = rg
        self.path = path

    def __call__(self, _endpoint, script, payload, **_kwargs):
        env = dict(os.environ)
        if self.path is not None:
            env["PATH"] = self.path
        elif self.rg:
            env["PATH"] = str(Path(self.rg).parent) + os.pathsep + env.get("PATH", "")
        proc = run_worker_script(
            script, payload, env=env, cwd=str(self.cwd), isolated=True,
        )
        if proc.returncode != 0:
            return {"status": "failed", "error": proc.stderr[-2000:]}
        return json.loads(proc.stdout)


@unittest.skipIf(os.name == "nt", "POSIX remote search worker semantics; Windows control-end coverage stays in the client tests")
class NativeSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="rd-native-search-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "probe"
        self.root.mkdir()
        (self.root / ".hidden").mkdir()
        (self.root / "ignored").mkdir()
        (self.root / ".git").mkdir()
        (self.root / ".hidden.txt").write_text("needle hidden\n", encoding="utf-8")
        (self.root / ".hidden" / "inside.txt").write_text("needle hidden dir\n", encoding="utf-8")
        (self.root / "visible.txt").write_text("needle visible\n", encoding="utf-8")
        (self.root / "ignored" / "ignored.txt").write_text("needle ignored\n", encoding="utf-8")
        (self.root / ".git" / "vcs.txt").write_text("needle vcs\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        if shutil.which("git"):
            subprocess.run(
                ["git", "-c", "init.defaultBranch=main", "init", "-q", str(self.root)],
                capture_output=True,
                check=False,
            )
        self.endpoint = Endpoint(
            host="127.0.0.1", port=22, user="root", root=str(self.root),
            cwd=str(self.root), runtime_env=False,
        )
        # PATH exposing real grep but not rg, so the fallback path runs.
        self.bindir = Path(self.temp.name) / "grep-bin"
        self.bindir.mkdir()
        grep = shutil.which("grep")
        if grep:
            os.symlink(grep, self.bindir / "grep")

    def call(self, function, normalized, *, transport=None):
        runner = transport or _Transport(self.root)
        with mock.patch.object(search_ops, "run_remote_python", runner):
            return function(self.endpoint, **normalized)

    def grep(self, normalized, *, transport=None):
        return self.call(search_ops.remote_grep, normalized, transport=transport)

    # -- Glob ---------------------------------------------------------------
    def test_claude_glob_includes_hidden_and_ignored_but_not_vcs_or_dirs(self) -> None:
        root = self.root
        os.utime(root / ".hidden.txt", (2_000_000_000, 2_000_000_000))
        payload = self.call(search_ops.remote_glob, _claude_glob({"pattern": "*"}))
        self.assertEqual(payload["result"]["status"], "ok")
        rows = payload["result"]["matches"]
        names = {Path(row).name for row in rows}
        self.assertIn(".hidden.txt", names)
        self.assertIn("ignored.txt", names)  # native Glob passes --no-ignore
        self.assertNotIn("vcs.txt", names)
        self.assertTrue(all(Path(row).is_file() for row in rows))
        self.assertEqual(Path(rows[0]).name, ".hidden.txt")

    def test_kimi_glob_hidden_ignored_vcs_and_files_only(self) -> None:
        payload = self.call(search_ops.remote_glob, _kimi_glob({"pattern": "*"}))
        self.assertEqual(payload["result"]["status"], "ok")
        rows = payload["result"]["matches"]
        names = {Path(row).name for row in rows}
        self.assertIn(".hidden.txt", names)
        self.assertIn("inside.txt", names)
        self.assertNotIn("ignored.txt", names)
        self.assertNotIn("vcs.txt", names)
        self.assertTrue(all(Path(row).is_file() for row in rows))

    def test_kimi_glob_head_limit_zero_paginates_without_loss(self) -> None:
        for index in range(300):
            (self.root / f"f-{index:03d}.txt").write_text(f"{index}\n", encoding="utf-8")
        expected = {
            str(path)
            for path in self.root.rglob("*.txt")
            if "ignored" not in path.parts and ".git" not in path.parts
        }
        seen: list[str] = []
        offset = 0
        for _ in range(10):
            payload = self.call(
                search_ops.remote_glob,
                _kimi_glob({"pattern": "*.txt", "head_limit": 0, "offset": offset}),
            )
            self.assertEqual(payload["result"]["status"], "ok")
            seen.extend(payload["result"]["matches"])
            following = payload["result"]["next_offset"]
            if following is None:
                break
            self.assertGreater(following, offset)
            offset = following
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), expected)

    def test_kimi_glob_excludes_sensitive_names_at_search_root(self) -> None:
        (self.root / ".env").write_text("SECRET=1\n", encoding="utf-8")
        (self.root / "id_rsa").write_text("KEY\n", encoding="utf-8")
        payload = self.call(search_ops.remote_glob, _kimi_glob({"pattern": "*"}))
        names = {Path(row).name for row in payload["result"]["matches"]}
        self.assertNotIn(".env", names)
        self.assertNotIn("id_rsa", names)

    # -- Grep ---------------------------------------------------------------
    def test_grep_fallback_content_offset_paginates(self) -> None:
        for index in range(5):
            (self.root / f"hit-{index}.txt").write_text(f"hitneedle {index}\n", encoding="utf-8")
        runner = _Transport(self.root, rg=None, path=str(self.bindir))
        seen: list[str] = []
        offset = 0
        for _ in range(5):
            payload = self.grep(
                _claude_grep(
                    {
                        "pattern": "hitneedle",
                        "output_mode": "content",
                        "head_limit": 2,
                        "offset": offset,
                    }
                ),
                transport=runner,
            )
            self.assertEqual(payload["result"]["status"], "ok")
            self.assertEqual(payload["result"]["engine"], "grep")
            seen.extend(payload["result"]["matches"])
            following = payload["result"]["next_offset"]
            if following is None:
                break
            self.assertGreater(following, offset)
            offset = following
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)

    def test_grep_fallback_count_matches_reports_total_and_next_offset(self) -> None:
        for index in range(3):
            (self.root / f"pair-{index}.txt").write_text(
                f"pairneedle {index}\n pairneedle {index}\n", encoding="utf-8"
            )
        runner = _Transport(self.root, rg=None, path=str(self.bindir))
        payload = self.grep(
            _kimi_grep(
                {"pattern": "pairneedle", "output_mode": "count_matches", "head_limit": 2}
            ),
            transport=runner,
        )
        self.assertEqual(payload["result"]["status"], "ok")
        self.assertEqual(payload["result"]["engine"], "grep")
        self.assertEqual(len(payload["result"]["matches"]), 2)
        self.assertEqual(payload["result"]["total_matches"], 6)
        self.assertEqual(payload["result"]["next_offset"], 2)
        second = self.grep(
            _kimi_grep(
                {
                    "pattern": "pairneedle",
                    "output_mode": "count_matches",
                    "head_limit": 2,
                    "offset": 2,
                }
            ),
            transport=runner,
        )
        self.assertEqual(len(second["result"]["matches"]), 1)
        self.assertEqual(second["result"]["total_matches"], 6)
        self.assertIsNone(second["result"]["next_offset"])
        counts = [int(row.rsplit(":", 1)[1]) for row in payload["result"]["matches"] + second["result"]["matches"]]
        self.assertEqual(sum(counts), 6)

    @unittest.skipUnless(RG, "ripgrep is required for native Grep semantics")
    def test_native_grep_hidden_ignored_vcs_and_mtime_order(self) -> None:
        os.utime(self.root / ".hidden.txt", (2_000_000_000, 2_000_000_000))
        for name in ("claude", "kimi"):
            with self.subTest(profile=name):
                normalizer = _claude_grep if name == "claude" else _kimi_grep
                payload = self.grep(
                    normalizer({"pattern": "needle", "output_mode": "files_with_matches"})
                )
                self.assertEqual(payload["result"]["status"], "ok")
                rows = payload["result"]["matches"]
                names = {Path(row).name for row in rows}
                self.assertIn(".hidden.txt", names)
                self.assertIn("inside.txt", names)
                self.assertNotIn("ignored.txt", names)
                self.assertNotIn("vcs.txt", names)
                self.assertEqual(Path(rows[0]).name, ".hidden.txt")

    @unittest.skipUnless(RG, "ripgrep is required for native Grep semantics")
    def test_grep_offset_pagination_no_duplicates_or_gaps(self) -> None:
        for index in range(280):
            (self.root / f"f-{index:03d}.txt").write_text(f"needle {index}\n", encoding="utf-8")
        normal = _kimi_grep(
            {"pattern": "needle", "output_mode": "files_with_matches", "head_limit": 0}
        )
        expected = {
            str(path)
            for path in self.root.rglob("*.txt")
            if "ignored" not in path.parts and ".git" not in path.parts
        }
        seen: list[str] = []
        offset = 0
        for _ in range(200):
            paged = dict(normal, offset=offset, limit=7)
            payload = self.grep(paged)
            self.assertEqual(payload["result"]["status"], "ok")
            seen.extend(payload["result"]["matches"])
            following = payload["result"]["next_offset"]
            if following is None:
                break
            self.assertGreater(following, offset)
            offset = following
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), expected)

    def test_native_grep_falls_back_with_explicit_approximation_warning(self) -> None:
        runner = _Transport(self.root, rg=None, path=str(self.bindir))
        payload = self.grep(
            _kimi_grep({"pattern": "needle", "output_mode": "files_with_matches"}),
            transport=runner,
        )
        self.assertEqual(payload["result"]["status"], "ok")
        self.assertEqual(payload["result"]["outcome"], "success")
        self.assertEqual(payload["result"]["engine"], "grep")
        names = {Path(row).name for row in payload["result"]["matches"]}
        self.assertIn(".hidden.txt", names)
        self.assertNotIn("vcs.txt", names)
        self.assertNotIn("ignored.txt", names)
        caveat = " ".join(payload["result"]["warnings"])
        self.assertIn("gitignore", caveat)
        self.assertIn("approximate", caveat)
        self.assertIn("grep fallback", payload["text"])

    def test_grep_fallback_head_limit_zero_paginates_without_loss(self) -> None:
        os.utime(self.root / ".hidden.txt", (2_000_000_000, 2_000_000_000))
        os.utime(self.root / "visible.txt", (1_500_000_000, 1_500_000_000))
        for index in range(160):
            path = self.root / f"f-{index:03d}.txt"
            path.write_text(f"needle {index}\n", encoding="utf-8")
            os.utime(path, (1_000_000_000 + index, 1_000_000_000 + index))
        runner = _Transport(self.root, rg=None, path=str(self.bindir))
        normal = _kimi_grep(
            {"pattern": "needle", "output_mode": "files_with_matches", "head_limit": 0}
        )
        seen: list[str] = []
        offset = 0
        for _ in range(100):
            payload = self.grep(dict(normal, offset=offset), transport=runner)
            self.assertEqual(payload["result"]["status"], "ok")
            seen.extend(payload["result"]["matches"])
            following = payload["result"]["next_offset"]
            if following is None:
                break
            self.assertGreater(following, offset)
            offset = following
        expected = {
            str(path)
            for path in self.root.rglob("*.txt")
            if ".git" not in path.parts and "ignored" not in path.parts
        }
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), expected)
        self.assertEqual(Path(seen[0]).name, ".hidden.txt")
        names = [Path(item).name for item in seen]
        self.assertLess(names.index("f-159.txt"), names.index("f-158.txt"))

    def test_grep_require_rg_still_fails_explicitly_when_requested(self) -> None:
        runner = _Transport(self.root, rg=None, path="/nonexistent")
        with mock.patch.object(search_ops, "run_remote_python", runner):
            payload = search_ops.remote_grep(
                self.endpoint, pattern="needle", require_rg=True
            )
        self.assertEqual(payload["result"]["status"], "rg_required")
        self.assertEqual(payload["result"]["outcome"], "failed")
        self.assertIn("ripgrep", payload["text"])

    def test_grep_character_cap_advances_next_offset(self) -> None:
        matches = [f"/very/long/path/{'x' * 180}-{index}.txt:1:needle" for index in range(400)]
        with mock.patch.object(
            search_ops, "run_remote_python",
            return_value={"status": "ok", "matches": matches, "truncated": False, "next_offset": None},
        ):
            payload = search_ops.remote_grep(
                self.endpoint, pattern="needle", output_mode="content", limit=0
            )
        visible = payload["result"]["matches"]
        self.assertLess(len(visible), len(matches))
        self.assertTrue(payload["result"]["truncated"])
        self.assertEqual(payload["result"]["next_offset"], len(visible))
        self.assertIn(f"<continue offset={len(visible)}>", payload["text"])

    def test_glob_failure_is_not_an_empty_success(self) -> None:
        missing = self.root / "missing"
        payload = self.call(
            search_ops.remote_glob,
            _claude_glob({"pattern": "*", "path": str(missing)}),
        )
        self.assertEqual(payload["result"]["outcome"], "failed")
        self.assertEqual(payload["result"]["status"], "not_directory")
        self.assertIn("not a directory", payload["text"])


if __name__ == "__main__":
    unittest.main()
