from __future__ import annotations

import subprocess

import pytest

from bp_work_server.decomp import DecompRepo


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def decomp_repo(tmp_path):
    """A tiny git repo mirroring the b5-decomp layout: a committed .cpp, no .h."""
    root = tmp_path / "b5-decomp"
    (root / "src" / "World").mkdir(parents=True)
    (root / "src" / "World" / "Foo.cpp").write_text("// foo\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Tester")
    _git(root, "add", ".")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "add foo")
    return DecompRepo(root=root, branch="main")


def test_resolves_cpp_path_and_date(decomp_repo):
    path, date = decomp_repo.resolve("b5-decomp/src/World/Foo.cpp")
    assert path == "src/World/Foo.cpp"
    assert date and date.startswith("20")  # ISO-8601 commit date


def test_header_falls_back_to_cpp_sibling(decomp_repo):
    # Headers are inlined into the .cpp, so a .h destination resolves to .cpp.
    path, date = decomp_repo.resolve("b5-decomp/src/World/Foo.h")
    assert path == "src/World/Foo.cpp"
    assert date


def test_missing_file_returns_none(decomp_repo):
    assert decomp_repo.resolve("b5-decomp/src/World/Missing.cpp") == (None, None)


def test_missing_clone_is_graceful(tmp_path):
    repo = DecompRepo(root=tmp_path / "nope", branch="main")
    assert repo.available is False
    assert repo.resolve("b5-decomp/src/World/Foo.cpp") == (None, None)
