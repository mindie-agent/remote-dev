"""Run the shipped search worker against real paths and symlinks."""
import json
import glob
import subprocess
import sys

import pytest

from remote_dev.core.search_ops import REMOTE_SEARCH_PY


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="executes the remote POSIX search worker against local filesystem paths",
)


def search(root, base, pattern, **options):
    result = subprocess.run(
        [sys.executable, "-I", "-c", REMOTE_SEARCH_PY],
        input=json.dumps(dict(op="glob", root=str(root), cwd=str(root),
                              path=str(base), pattern=pattern, **options)),
        text=True, capture_output=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("escape", ["parent", "absolute", "symlink", "recursive_symlink"])
def test_glob_rejects_outside_root_before_returning_metadata(tmp_path, escape):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("not part of this workspace")
    if escape in {"symlink", "recursive_symlink"}:
        (root / "linked").symlink_to(outside, target_is_directory=True)
    pattern = {"parent": "../outside/*", "absolute": str(outside / "*"),
               "symlink": "linked/*", "recursive_symlink": "**/*.txt"}[escape]
    result = search(root, root, pattern)
    assert result["status"] == "path_outside_root"
    assert not result.get("matches")


def test_recursive_glob_does_not_scan_outside_even_if_no_file_matches(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "private").mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    result = search(root, root, "**/*.does-not-exist")
    assert result["status"] == "path_outside_root"
    assert not result.get("matches")


@pytest.mark.parametrize("pattern_kind", ["relative", "absolute", "parent_inside", "symlink_inside"])
def test_glob_keeps_supported_in_root_patterns(tmp_path, pattern_kind):
    root = tmp_path / "root"
    nested = root / "nested"
    nested.mkdir(parents=True)
    target = root / "visible.txt"
    target.write_text("visible")
    (root / "linked").symlink_to(nested, target_is_directory=True)
    (nested / "inside.txt").write_text("inside")
    base, pattern, expected = {
        "relative": (root, "*.txt", target),
        "absolute": (root, str(root / "*.txt"), target),
        "parent_inside": (nested, "../*.txt", nested / "../visible.txt"),
        "symlink_inside": (root, "linked/*.txt", root / "linked/inside.txt"),
    }[pattern_kind]
    result = search(root, base, pattern)
    assert result["status"] == "ok"
    assert {row["path"] for row in result["matches"]} == {str(expected)}
    assert result["matches"][0]["size"] == expected.stat().st_size


@pytest.mark.parametrize("pattern", ["*", "**", "**/*", "**/*.txt", "**/", "nested/*",
                                     "nested/../*.txt", ".*", "./*.txt", "absolute:*",
                                     "absolute:*/", "absolute:**/*.txt"])
def test_safe_glob_patterns_preserve_stdlib_matching_contract(tmp_path, pattern):
    root = tmp_path / "root"
    (root / "nested" / "deep").mkdir(parents=True)
    (root / "visible.txt").write_text("top")
    (root / ".hidden").write_text("hidden")
    (root / "nested" / "inside.txt").write_text("nested")
    (root / "nested" / "deep" / "bottom.py").write_text("bottom")
    if pattern.startswith("absolute:"):
        pattern = str(root) + "/" + pattern.partition(":")[2]
    expected = set(glob.iglob(pattern, root_dir=root, recursive=True))
    result = search(root, root, pattern)
    assert result["status"] == "ok"
    assert {row["relpath"] for row in result["matches"]} == expected
