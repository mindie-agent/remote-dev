"""Local checks that RemoteGlob ``respect_gitignore`` actually filters."""

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

from remote_dev.core.endpoint import Endpoint
import remote_dev.core.search_ops as search_ops


def run_glob(tree: Path, *, respect_gitignore: bool, extra_env: dict[str, str] | None = None) -> dict:
    payload = {
        "op": "glob",
        "root": str(tree),
        "cwd": str(tree),
        "path": str(tree),
        "pattern": "**/*",
        "limit": 100,
        "respect_gitignore": respect_gitignore,
    }
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-c", search_ops.REMOTE_SEARCH_PY],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=str(tree),
    )
    if proc.returncode != 0:
        raise AssertionError(f"glob helper failed: {proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout)


class RespectGitignoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name) / "tree"
        self.tree.mkdir()
        (self.tree / "keep.py").write_text("keep\n", encoding="utf-8")
        (self.tree / "skip.pyc").write_text("skip\n", encoding="utf-8")
        (self.tree / "build").mkdir()
        (self.tree / "build" / "out.bin").write_text("out\n", encoding="utf-8")
        (self.tree / ".gitignore").write_text("*.pyc\nbuild/\n", encoding="utf-8")

    def test_respect_gitignore_false_keeps_ignored_paths(self) -> None:
        data = run_glob(self.tree, respect_gitignore=False)
        self.assertEqual(data["status"], "ok")
        relpaths = {row["relpath"] for row in data["matches"]}
        self.assertIn("keep.py", relpaths)
        self.assertIn("skip.pyc", relpaths)
        self.assertTrue(any(item.startswith("build") for item in relpaths))

    def test_fallback_filters_without_git_on_path(self) -> None:
        bindir = Path(self.temp.name) / "empty-bin"
        bindir.mkdir()
        data = run_glob(self.tree, respect_gitignore=True, extra_env={"PATH": str(bindir)})
        self.assertEqual(data["status"], "ok")
        relpaths = {row["relpath"] for row in data["matches"]}
        self.assertIn("keep.py", relpaths)
        self.assertNotIn("skip.pyc", relpaths)
        self.assertFalse(any(item == "build" or item.startswith("build/") for item in relpaths))
        self.assertIn("git is unavailable", " ".join(data.get("warnings", [])))

    def test_git_check_ignore_filters_in_a_worktree(self) -> None:
        git = shutil.which("git")
        if git is None:
            self.skipTest("git is not available")
        env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "remote-dev-test",
            "GIT_AUTHOR_EMAIL": "remote-dev-test@example.test",
            "GIT_COMMITTER_NAME": "remote-dev-test",
            "GIT_COMMITTER_EMAIL": "remote-dev-test@example.test",
            "HOME": str(Path(self.temp.name) / "empty-home"),
        }
        Path(env["HOME"]).mkdir()
        init = subprocess.run(
            [git, "init", "-q", str(self.tree)],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        self.assertEqual(init.returncode, 0, init.stderr)
        data = run_glob(self.tree, respect_gitignore=True, extra_env=env)
        self.assertEqual(data["status"], "ok")
        relpaths = {row["relpath"] for row in data["matches"]}
        self.assertIn("keep.py", relpaths)
        self.assertNotIn("skip.pyc", relpaths)
        self.assertFalse(any(item == "build" or item.startswith("build/") for item in relpaths))

    def test_remote_glob_no_longer_emits_unimplemented_warning(self) -> None:
        endpoint = Endpoint(host="192.0.2.10", port=46000)

        def fake_run(_endpoint, _script, payload, **_kwargs):
            self.assertTrue(payload["respect_gitignore"])
            return {
                "status": "ok",
                "matches": [{"path": "/vllm-workspace/keep.py", "relpath": "keep.py"}],
                "truncated": False,
                "warnings": [],
            }

        with mock.patch.object(search_ops, "run_remote_python", fake_run):
            payload = search_ops.remote_glob(endpoint, pattern="*", respect_gitignore=True)
        warnings = payload["result"]["warnings"]
        self.assertEqual(warnings, [])
        self.assertFalse(any("not implemented" in str(item) for item in warnings))


if __name__ == "__main__":
    unittest.main()
