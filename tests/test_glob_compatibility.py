"""Local end-to-end checks for the REMOTE_SEARCH_PY glob branch.

The helper normally runs on the remote host over SSH; here the same script
runs locally in a subprocess with a JSON payload and a temporary tree, following
``tests/test_grep_fallback.py``. A legacy ``glob.glob`` shim accepts only the
Python 3.9 signature ``(pathname, *, recursive=False)`` and delegates to the
real stdlib implementation so a ``root_dir`` call fails through normal argument
binding even on newer interpreters.
"""
from __future__ import annotations

import json
import subprocess
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
from _worker_runner import run_worker_script

import remote_dev.core.search_ops as search_ops  # noqa: E402

LEGACY_GLOB_SHIM = """\
import glob as _stdlib_glob

_orig_glob = _stdlib_glob.glob

def _py39_glob(pathname, *, recursive=False):
    return _orig_glob(pathname, recursive=recursive)

_stdlib_glob.glob = _py39_glob

"""


def run_remote_search_helper(payload, *, cwd, script=None, shim=False):
    source = search_ops.REMOTE_SEARCH_PY if script is None else script
    if shim:
        source = LEGACY_GLOB_SHIM + source
    return run_worker_script(
        source, payload, cwd=str(cwd),
    )


def glob_payload(root, path, pattern, limit=100):
    return {
        "op": "glob",
        "root": str(root),
        "cwd": str(root),
        "path": str(path),
        "pattern": pattern,
        "limit": limit,
    }


class GlobCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.unrelated_cwd = self.home / "unrelated-cwd"
        self.unrelated_cwd.mkdir()
        (self.unrelated_cwd / "trap.txt").write_text("trap\n", encoding="utf-8")
        self.root = self.home / "root"
        self.nested = self.root / "nested"
        (self.nested / "deep").mkdir(parents=True)
        (self.nested / "keep.txt").write_text("keep\n", encoding="utf-8")
        (self.nested / "skip.py").write_text("skip\n", encoding="utf-8")
        (self.nested / "deep" / "buried.txt").write_text("buried\n", encoding="utf-8")
        (self.nested / ".dot.txt").write_text("hidden\n", encoding="utf-8")
        (self.root / "sibling.txt").write_text("sibling\n", encoding="utf-8")
        self.parent_cwd = os.getcwd()

    def tearDown(self) -> None:
        self.assertEqual(os.getcwd(), self.parent_cwd)

    def run_glob(self, path, pattern, *, limit=100, shim=False, script=None, cwd=None):
        proc = run_remote_search_helper(
            glob_payload(self.root, path, pattern, limit=limit),
            cwd=cwd or self.unrelated_cwd,
            script=script,
            shim=shim,
        )
        self.assertEqual(os.getcwd(), self.parent_cwd)
        return proc

    def load_ok(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["status"], "ok")
        return data

    def test_plain_glob_stays_under_explicit_nested_base_from_unrelated_cwd(self) -> None:
        data = self.load_ok(self.run_glob(self.nested, "*.txt"))
        relpaths = {Path(row["relpath"]).as_posix() for row in data["matches"]}
        self.assertEqual(relpaths, {"keep.txt"})
        row = data["matches"][0]
        self.assertEqual(Path(row["path"]), self.nested / "keep.txt")
        self.assertTrue(str(row["path"]).startswith(str(self.nested)))
        self.assertNotIn(str(self.unrelated_cwd / "trap.txt"), {row["path"] for row in data["matches"]})
        self.assertNotIn(str(self.root / "sibling.txt"), {row["path"] for row in data["matches"]})
        self.assertFalse(data["truncated"])

    def test_recursive_glob_includes_direct_and_nested_txt(self) -> None:
        data = self.load_ok(self.run_glob(self.nested, "**/*.txt"))
        relpaths = {Path(row["relpath"]).as_posix() for row in data["matches"]}
        self.assertEqual(relpaths, {"keep.txt", "deep/buried.txt"})
        paths = {row["path"] for row in data["matches"]}
        self.assertEqual(paths, {str(self.nested / "keep.txt"), str(self.nested / "deep" / "buried.txt")})
        self.assertNotIn("skip.py", relpaths)
        self.assertNotIn(".dot.txt", relpaths)
        self.assertFalse(any(row["relpath"].endswith(".py") for row in data["matches"]))

    def test_literal_metacharacters_in_base_are_not_expanded(self) -> None:
        base = self.root / "base [literal]"
        (base / "inner").mkdir(parents=True)
        (base / "hit.txt").write_text("hit\n", encoding="utf-8")
        (base / "inner" / "deep.txt").write_text("deep\n", encoding="utf-8")
        (base / "skip.py").write_text("skip\n", encoding="utf-8")

        plain = self.load_ok(self.run_glob(base, "*.txt"))
        self.assertEqual({Path(row["relpath"]).as_posix() for row in plain["matches"]}, {"hit.txt"})
        self.assertEqual(Path(plain["matches"][0]["path"]), base / "hit.txt")

        recursive = self.load_ok(self.run_glob(base, "**/*.txt"))
        self.assertEqual({Path(row["relpath"]).as_posix() for row in recursive["matches"]}, {"hit.txt", "inner/deep.txt"})
        for row in recursive["matches"]:
            self.assertEqual(Path(row["path"]), base / row["relpath"])
            self.assertIn("base [literal]", row["path"])

    def test_metadata_directory_nomatch_and_limit_truncated(self) -> None:
        base = self.root / "meta"
        newest = base / "newest.txt"
        oldest = base / "oldest.txt"
        mid_dir = base / "mid_dir"
        mid_dir.mkdir(parents=True)
        newest.write_text("NNN\n", encoding="utf-8")
        oldest.write_text("O\n", encoding="utf-8")
        newest_ns = self._stamp(newest, 9_000_000_000)
        mid_ns = self._stamp(mid_dir, 5_000_000_000)
        oldest_ns = self._stamp(oldest, 1_000_000_000)
        self.assertEqual(len({newest_ns, mid_ns, oldest_ns}), 3)

        listing = self.load_ok(self.run_glob(base, "*"))
        self.assertFalse(listing["truncated"])
        relpaths = [Path(row["relpath"]).as_posix() for row in listing["matches"]]
        self.assertEqual(relpaths, ["newest.txt", "mid_dir", "oldest.txt"])

        newest_row, mid_row, oldest_row = listing["matches"]
        self.assertEqual(Path(newest_row["path"]), newest)
        self.assertEqual(newest_row["type"], "file")
        self.assertEqual(newest_row["size"], newest.stat().st_size)
        self.assertEqual(newest_row["mtime_ns"], newest_ns)

        self.assertEqual(Path(mid_row["path"]), mid_dir)
        self.assertEqual(mid_row["relpath"], "mid_dir")
        self.assertEqual(mid_row["type"], "directory")
        self.assertEqual(mid_row["mtime_ns"], mid_ns)
        self.assertEqual(mid_row["size"], mid_dir.lstat().st_size)

        self.assertEqual(Path(oldest_row["path"]), oldest)
        self.assertEqual(oldest_row["type"], "file")
        self.assertEqual(oldest_row["size"], oldest.stat().st_size)
        self.assertEqual(oldest_row["mtime_ns"], oldest_ns)

        empty = self.load_ok(self.run_glob(base, "*.nomatch"))
        self.assertEqual(empty["matches"], [])
        self.assertFalse(empty["truncated"])

        limited = self.load_ok(self.run_glob(base, "*", limit=1))
        self.assertTrue(limited["truncated"])
        self.assertEqual(len(limited["matches"]), 1)
        self.assertEqual(limited["matches"][0]["relpath"], "newest.txt")
        self.assertEqual(limited["matches"][0]["mtime_ns"], newest_ns)

    def test_legacy_glob_signature_shim_runs_real_helper(self) -> None:
        binding = subprocess.run(
            [sys.executable, "-c", LEGACY_GLOB_SHIM + "import glob\nglob.glob('*', root_dir='.', recursive=True)"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(binding.returncode, 0, binding.stdout)
        self.assertIn("unexpected keyword argument", binding.stderr)
        self.assertIn("root_dir", binding.stderr)

        data = self.load_ok(self.run_glob(self.nested, "**/*.txt", shim=True))
        self.assertEqual({Path(row["relpath"]).as_posix() for row in data["matches"]}, {"keep.txt", "deep/buried.txt"})
        for row in data["matches"]:
            self.assertEqual(Path(row["path"]), self.nested / row["relpath"])

    def test_base_path_errors(self) -> None:
        missing_dir = self.parse(self.run_glob(self.nested / "keep.txt", "*"))
        self.assertEqual(missing_dir["status"], "not_directory")
        self.assertIn("not a directory", missing_dir["error"])

        outside = self.parse(self.run_glob(self.unrelated_cwd, "*"))
        self.assertEqual(outside["status"], "path_outside_root")
        self.assertIn("outside root", outside["error"])

    def parse(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    @staticmethod
    def _stamp(path: Path, mtime_ns: int) -> int:
        os.utime(path, ns=(mtime_ns, mtime_ns))
        return path.lstat().st_mtime_ns


if __name__ == "__main__":
    unittest.main()
