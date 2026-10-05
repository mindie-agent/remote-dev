"""Property tests for artifact manifests and transfers (``core.artifact_ops``).

Property: a manifest is a faithful, deterministic description of a tree, and
verification against it catches every single-byte corruption, truncation,
extension, and every missing, added or renamed file. Transfers never leave a
file under its final name unless its bytes match the manifest.

The remote manifest script runs in-process on a temporary tree; pulls and
pushes run the real ``core.artifact_ops`` functions through fake transports
that serve (and optionally corrupt) bytes from that tree.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parents[1]

import remote_dev.core.artifact_ops as artifact_ops  # noqa: E402
import remote_dev.core.state_store as state_store  # noqa: E402
from remote_dev.core.endpoint import Endpoint  # noqa: E402
from test_property_support import DOC_HOSTS, MULTIBYTE, Gen, run_cases, run_remote_script  # noqa: E402

NAME_ALPHABET = "abcxyz019_-." + MULTIBYTE


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def entry_name(gen: Gen, max_len: int = 8) -> str:
    """A file or directory name that is not '.' or '..'."""
    while True:
        name = gen.text(NAME_ALPHABET, 1, max_len)
        if name.strip(".") and name not in {".", ".."}:
            return name


def build_tree(gen: Gen, root: Path) -> dict[str, bytes]:
    """Create 1..6 files (some nested, some empty) and return {relpath: bytes}."""
    files: dict[str, bytes] = {}
    dirs = [""] + ["/".join(entry_name(gen, 6) for _ in range(gen.integer(1, 2))) for _ in range(gen.integer(0, 2))]
    for _ in range(gen.integer(1, 6)):
        directory = gen.choice(dirs)
        name = entry_name(gen)
        rel = f"{directory}/{name}" if directory else name
        if rel in files or any(other.startswith(rel + "/") or rel.startswith(other + "/") for other in files):
            continue
        data = gen.one_of(lambda: b"", lambda: gen.raw_bytes(1, 64), lambda: gen.raw_bytes(1000, 3000))
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files[rel] = data
    return files


def reference_manifest(files: dict[str, bytes]) -> dict[str, tuple[str, int]]:
    return {rel: (sha(data), len(data)) for rel, data in files.items()}


def manifest_view(manifest: dict[str, Any]) -> dict[str, tuple[str, int]]:
    return {item["relpath"]: (item["sha256"], item["size"]) for item in manifest["files"]}


def remote_manifest(root: Path, target: Path) -> dict[str, Any]:
    return run_remote_script(artifact_ops.REMOTE_MANIFEST_PY, {"root": str(root), "cwd": str(root), "remote_path": str(target)})


def mutate(gen: Gen, root: Path, files: dict[str, bytes]) -> tuple[str, dict[str, bytes]]:
    """Apply one mutation to the tree on disk and return (kind, expected files)."""
    updated = dict(files)
    rel = gen.choice(sorted(files))
    data = files[rel]
    kinds = ["append", "add", "remove", "rename"]
    if data:
        kinds += ["flip", "truncate"]
    kind = gen.choice(kinds)
    path = root / rel
    if kind == "flip":
        index = gen.integer(0, len(data) - 1)
        flipped = bytes([data[index] ^ (1 << gen.integer(0, 7))])
        updated[rel] = data[:index] + flipped + data[index + 1:]
        path.write_bytes(updated[rel])
    elif kind == "truncate":
        cut = gen.integer(0, len(data) - 1)
        updated[rel] = data[:cut]
        path.write_bytes(updated[rel])
    elif kind == "append":
        updated[rel] = data + gen.raw_bytes(1, 8)
        path.write_bytes(updated[rel])
    elif kind == "add":
        new_rel = entry_name(gen) + ".extra"
        while new_rel in updated:
            new_rel += "x"
        updated[new_rel] = gen.raw_bytes(0, 16)
        (root / new_rel).write_bytes(updated[new_rel])
    elif kind == "remove":
        path.unlink()
        del updated[rel]
    else:
        new_rel = rel + ".renamed"
        path.rename(root / new_rel)
        del updated[rel]
        updated[new_rel] = data
    return kind, updated


@unittest.skipIf(os.name == "nt", "in-process remote manifest uses Linux filesystem paths")
class ManifestProperties(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()

    def _case_dir(self, index: int) -> Path:
        path = self.base / f"case-{index}"
        path.mkdir()
        return path

    def test_remote_and_local_manifests_agree_with_the_reference(self) -> None:
        def body(gen: Gen, index: int) -> None:
            root = self._case_dir(index)
            tree = root / "out"
            tree.mkdir()
            files = build_tree(gen, tree)
            expected = reference_manifest(files)
            remote = remote_manifest(root, tree)
            self.assertEqual(remote["status"], "ok", remote)
            self.assertEqual(manifest_view(remote), expected)
            self.assertEqual(remote["file_count"], len(files))
            self.assertEqual(remote["total_bytes"], sum(len(d) for d in files.values()))
            self.assertTrue(remote["is_dir"])
            local = artifact_ops._local_manifest(tree)
            self.assertEqual(manifest_view(local), expected)
            self.assertEqual(local["file_count"], len(files))
            # Deterministic ordering: entries are sorted by path components
            # (the order ``sorted(Path.rglob(...))`` produces) and stable.
            relpaths = [item["relpath"] for item in remote["files"]]
            self.assertEqual(relpaths, sorted(relpaths, key=lambda rel: Path(rel).parts))
            self.assertEqual(relpaths, [item["relpath"] for item in local["files"]])
            self.assertEqual(manifest_view(remote_manifest(root, tree)), manifest_view(remote))
            for item in remote["files"]:
                self.assertFalse(item["relpath"].startswith("/"))
                self.assertNotIn("..", Path(item["relpath"]).parts)
                self.assertEqual(Path(item["path"]).read_bytes(), files[item["relpath"]])
            # Single-file manifests use relpath "." and describe that file.
            single_rel = gen.choice(sorted(files))
            single = remote_manifest(root, tree / single_rel)
            self.assertEqual(single["status"], "ok")
            self.assertFalse(single["is_dir"])
            self.assertEqual(manifest_view(single), {".": expected[single_rel]})
            self.assertEqual(manifest_view(artifact_ops._local_manifest(tree / single_rel)), {".": expected[single_rel]})

        run_cases(120, body, label="manifest agreement")

    def test_every_mutation_changes_the_manifest_view(self) -> None:
        def body(gen: Gen, index: int) -> None:
            root = self._case_dir(index)
            tree = root / "out"
            tree.mkdir()
            files = build_tree(gen, tree)
            before = manifest_view(remote_manifest(root, tree))
            kind, expected_files = mutate(gen, tree, files)
            after = manifest_view(remote_manifest(root, tree))
            self.assertEqual(after, reference_manifest(expected_files))
            if expected_files == files:
                # A flip that happens to restore identical bytes is impossible;
                # truncation to the same length is impossible; so trees differ.
                self.fail(f"mutation {kind} did not change the tree")
            self.assertNotEqual(after, before, f"manifest did not notice {kind}")
            if kind in {"flip", "truncate", "append"}:
                changed = [rel for rel in before if rel in after and before[rel] != after[rel]]
                self.assertEqual(len(changed), 1, f"{kind} must change exactly one entry")
                self.assertEqual(set(before), set(after))
            else:
                self.assertNotEqual(set(before), set(after), f"{kind} must change the relpath set")

        run_cases(200, body, label="manifest sensitivity")

    def test_symlinks_anywhere_in_the_tree_block_the_manifest(self) -> None:
        def body(gen: Gen, index: int) -> None:
            root = self._case_dir(index)
            tree = root / "out"
            tree.mkdir()
            files = build_tree(gen, tree)
            outside = root / "outside.txt"
            outside.write_bytes(b"outside\n")
            link_dir = tree / gen.choice([str(Path(rel).parent) for rel in files])
            link = link_dir / (gen.text("abc", 1, 4) + ".link")
            link.symlink_to(gen.choice((outside, tree / gen.choice(sorted(files)))))
            remote = remote_manifest(root, tree)
            self.assertEqual(remote["status"], "blocked", remote)
            self.assertNotIn("files", remote)
            with self.assertRaises(ValueError):
                artifact_ops._local_manifest(tree)
            # The symlink itself as the target is blocked too.
            self.assertEqual(remote_manifest(root, link)["status"], "blocked")

        run_cases(40, body, label="manifest symlink policy")

    def test_outside_targets_are_blocked_not_walked(self) -> None:
        root = self._case_dir(0)
        (root / "in").mkdir()
        outside = self.base / "elsewhere"
        outside.mkdir()
        (outside / "x").write_bytes(b"x")
        self.assertEqual(remote_manifest(root, outside)["status"], "blocked")
        self.assertEqual(remote_manifest(root, root / "in" / ".." / ".." / "elsewhere")["status"], "blocked")
        self.assertEqual(remote_manifest(root, root / "in" / ".." / ".." / "elsewhere" / "missing")["status"], "blocked")

    def test_manifest_of_missing_path_reports_needs_input(self) -> None:
        """Non-strict ``Path.resolve()`` never raises, so the old
        ``except FileNotFoundError`` guard was dead. A mistyped
        ``remote_path`` yielded ``status: ok, file_count: 0`` and a pull
        "succeeded" with nothing transferred. Check ``exists()``."""
        root = self._case_dir(1)
        data = remote_manifest(root, root / "missing")
        self.assertEqual(data["status"], "needs_input", data)


class SafeLocalPathProperties(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve() / "pull"
        self.base.mkdir()
        outside = self.base.parent / "outside"
        outside.mkdir()
        (self.base / "linkdir").symlink_to(outside)
        (self.base / "linkfile").symlink_to(outside / "victim")

    def test_result_is_always_strictly_inside_base_or_rejected(self) -> None:
        outside = self.base.parent / "outside"

        def body(gen: Gen, _index: int) -> None:
            # Paths *through* pre-existing symlinks are covered by the
            # known-defect tests below; here the symlinks may only be the leaf.
            parts = [gen.choice(("..", ".", "", "sub", entry_name(gen, 6))) for _ in range(gen.integer(1, 4))]
            if gen.boolean(0.2):
                parts.append(gen.choice(("linkdir", "linkfile")))
            relpath = "/".join(parts)
            if gen.boolean(0.2):
                relpath = "/" + relpath
            before_outside = sorted(p.name for p in outside.iterdir())
            try:
                candidate = artifact_ops._safe_local_artifact_path(self.base, relpath)
            except ValueError:
                self.assertEqual(sorted(p.name for p in outside.iterdir()), before_outside, "rejection must have no side effects outside base")
                return
            resolved = candidate.parent.resolve()
            self.assertTrue(resolved == self.base or self.base in resolved.parents, f"{relpath!r} -> {candidate} escapes {self.base}")
            self.assertFalse(candidate.is_symlink())
            self.assertNotIn("..", candidate.relative_to(self.base).parts)
            self.assertEqual(sorted(p.name for p in outside.iterdir()), before_outside)

        run_cases(500, body, label="safe local artifact path")

    def test_parent_directories_are_not_created_outside_base(self) -> None:
        """``mkdir(parents=True)`` used to run before the containment
        check. A relpath through ``linkdir -> outside`` created
        ``outside/sub`` and only then raised ValueError."""
        outside = self.base.parent / "outside"
        with self.assertRaises(ValueError):
            artifact_ops._safe_local_artifact_path(self.base, "linkdir/sub/x.bin")
        self.assertFalse((outside / "sub").exists(), "directory created outside the local artifact dir")

    def test_dangling_symlink_or_file_in_parent_position_is_value_error(self) -> None:
        """A parent that is a dangling symlink or a regular file used to
        raise ``FileExistsError`` from mkdir. ``remote_artifact_pull`` only
        catches ``ValueError``, so the tool crashed instead of returning
        ``blocked``."""
        (self.base / "plainfile").write_bytes(b"x")
        for relpath in ("linkfile/sub/x", "plainfile/x"):
            with self.subTest(relpath=relpath):
                with self.assertRaises(ValueError):
                    artifact_ops._safe_local_artifact_path(self.base, relpath)

    def test_manifest_relpaths_are_always_accepted(self) -> None:
        def body(gen: Gen, index: int) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                tree = Path(tmp).resolve() / "t"
                tree.mkdir()
                files = build_tree(gen, tree)
                for rel in files:
                    candidate = artifact_ops._safe_local_artifact_path(self.base / f"case-{index}", rel)
                    self.assertEqual(candidate, self.base / f"case-{index}" / Path(rel))
                self.assertEqual(artifact_ops._safe_local_artifact_path(self.base / f"case-{index}", "."), self.base / f"case-{index}" / "artifact")

        run_cases(40, body, label="manifest relpaths accepted")


class TransferHarness:
    def __init__(self, test: unittest.TestCase) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.root = base / "root"
        self.root.mkdir()
        self.state = base / "state"
        self.local_dir = base / "local"
        self.corrupt: dict[str, Any] = {}
        self.missing: set[str] = set()
        patchers = [
            mock.patch.object(state_store, "substrate_root", return_value=self.state),
            mock.patch.object(artifact_ops, "run_remote_python", side_effect=lambda _endpoint, code, payload, **_kw: run_remote_script(code, payload)),
            mock.patch.object(artifact_ops, "ArtifactStream", side_effect=self._stream),
        ]
        for patcher in patchers:
            patcher.start()
            test.addCleanup(patcher.stop)
        self.endpoint = Endpoint(host=DOC_HOSTS[0], port=46000, root=str(self.root), cwd=str(self.root))

    def _stream(self, endpoint, operation, count, timeout_ms):
        harness = self
        class Stream:
            commit_possible = False
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def pull(self, item, destination, *, overwrite=False):
                path = Path(item["path"])
                if str(path) in harness.missing:
                    raise OSError("source missing")
                data = path.read_bytes()
                if str(path) in harness.corrupt:
                    data = harness.corrupt[str(path)](data)
                observed = sha(data)
                if observed != item["sha256"]:
                    raise artifact_ops.ArtifactTransferError("hash_mismatch", item["sha256"], observed)
                destination.write_bytes(data)
                return observed
            def push(self, item, source):
                observed = sha(source.read_bytes())
                if getattr(harness, "bad_push", None) == item["path"]:
                    observed = sha(b"different")
                if observed != item["sha256"]:
                    raise artifact_ops.ArtifactTransferError("hash_mismatch", item["sha256"], observed)
                return observed
        return Stream()


def corruption(gen: Gen) -> Any:
    kind = gen.choice(("flip", "truncate", "append", "empty"))
    if kind == "flip":
        def flip(data: bytes) -> bytes:
            if not data:
                return b"\x00"
            index = gen.integer(0, len(data) - 1)
            return data[:index] + bytes([data[index] ^ 0x01]) + data[index + 1:]
        return flip
    if kind == "truncate":
        return lambda data: data[:-1] if data else b"x"
    if kind == "append":
        return lambda data: data + b"\n"
    return lambda data: b"" if data else b"\x00"


@unittest.skipIf(os.name == "nt", "fake transport maps remote POSIX roots to local Linux roots")
class TransferProperties(unittest.TestCase):
    def test_pull_never_lands_a_file_whose_bytes_disagree_with_the_manifest(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            harness = TransferHarness(self)
            tree = harness.root / "out"
            tree.mkdir()
            files = build_tree(gen, tree)
            expected = reference_manifest(files)
            fault = gen.choice(("none", "corrupt", "missing"))
            victim = gen.choice(sorted(files))
            if fault == "corrupt":
                harness.corrupt[str(tree / victim)] = corruption(gen)
            elif fault == "missing":
                harness.missing.add(str(tree / victim))
            payload = artifact_ops.remote_artifact_pull(harness.endpoint, remote_path=str(tree), local_dir=str(harness.local_dir))
            result = payload["result"]
            landed = {str(p.relative_to(harness.local_dir)): p for p in harness.local_dir.rglob("*") if p.is_file()}
            self.assertFalse([name for name in landed if name.endswith(".tmp")], "temporary files must not remain")
            for rel, path in landed.items():
                self.assertIn(rel, expected, f"unexpected file landed: {rel}")
                self.assertEqual(sha(path.read_bytes()), expected[rel][0], f"{rel} landed with bytes that disagree with the manifest")
            if fault == "none":
                self.assertEqual(result["status"], "ok", payload["text"])
                self.assertEqual(set(landed), set(expected))
                self.assertTrue(Path(result["refs"]["local_manifest"]).exists())
                self.assertFalse((harness.local_dir / "manifest.json").exists())
                pulled = result["artifacts"][0]["pulled"]
                self.assertEqual({item["relpath"]: item["sha256"] for item in pulled}, {rel: value[0] for rel, value in expected.items()})
                # A second pull is a no-op: everything is skipped by hash match.
                again = artifact_ops.remote_artifact_pull(harness.endpoint, remote_path=str(tree), local_dir=str(harness.local_dir))["result"]
                self.assertEqual(again["status"], "ok")
                self.assertEqual(len(again["artifacts"][0]["skipped"]), len(expected))
                self.assertEqual(again["artifacts"][0]["pulled"], [])
            elif fault == "corrupt":
                self.assertEqual(result["status"], "hash_mismatch", payload["text"])
                self.assertEqual(result["outcome"], "failed")
                self.assertNotIn(victim, landed)
                self.assertEqual(result["expected_sha256"], expected[victim][0])
                self.assertNotEqual(result["observed_sha256"], expected[victim][0])
            else:
                self.assertEqual(result["status"], "failed", payload["text"])
                self.assertNotIn(victim, landed)

        run_cases(120, body, label="artifact pull integrity")

    def test_pull_of_a_single_file_lands_as_artifact(self) -> None:
        harness = TransferHarness(self)
        target = harness.root / "single.bin"
        data = b"\x00\x01payload\xff"
        target.write_bytes(data)
        result = artifact_ops.remote_artifact_pull(harness.endpoint, remote_path=str(target), local_dir=str(harness.local_dir))["result"]
        self.assertEqual(result["status"], "ok")
        self.assertEqual((harness.local_dir / "artifact").read_bytes(), data)

    def test_push_reports_exactly_the_local_manifest_and_stops_on_mismatch(self) -> None:
        def body(gen: Gen, index: int) -> None:
            harness = TransferHarness(self)
            local_tree = Path(harness.tmp.name).resolve() / f"src-{index}"
            local_tree.mkdir()
            files = build_tree(gen, local_tree)
            expected = reference_manifest(files)
            remote_base = str(harness.root / "dest")
            lie = gen.boolean(0.3)
            if lie:
                bad = gen.choice(sorted(files))
                harness.bad_push = str(Path(remote_base) / bad)
            payload = artifact_ops.remote_artifact_push(harness.endpoint, local_path=str(local_tree), remote_path=remote_base)
            result = payload["result"]
            pushed = {item["relpath"]: item for item in result["artifacts"][0]["pushed"]}
            for rel, item in pushed.items():
                self.assertEqual(item["sha256"], expected[rel][0])
                self.assertEqual(item["remote_path"], str(Path(remote_base) / rel))
                self.assertTrue(item["remote_path"].startswith(str(harness.root) + "/"))
            if lie:
                self.assertEqual(result["status"], "hash_mismatch", payload["text"])
                self.assertNotIn(bad, pushed)
            else:
                self.assertEqual(result["status"], "ok", payload["text"])
                self.assertEqual(set(pushed), set(expected))

        run_cases(60, body, label="artifact push manifest")

    def test_push_outside_root_is_blocked_before_any_transfer(self) -> None:
        harness = TransferHarness(self)
        local = Path(harness.tmp.name) / "one.txt"
        local.write_text("x\n", encoding="utf-8")
        calls: list[str] = []
        artifact_ops.ArtifactStream.side_effect = lambda *args, **kwargs: calls.append(args)  # type: ignore[attr-defined]
        for remote_path in ("/etc/x", str(harness.root) + "/../escape", "../escape"):
            with self.subTest(remote_path=remote_path):
                result = artifact_ops.remote_artifact_push(harness.endpoint, local_path=str(local), remote_path=remote_path)["result"]
                self.assertEqual(result["status"], "path_outside_root")
                self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
