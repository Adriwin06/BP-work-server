from __future__ import annotations

import subprocess

import pytest

from bp_work_server.decomp import DecompRepo


def _git(root, *args, env=None):
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=env
    )


def _commit(root, author, message, when=None):
    import os

    env = {**os.environ, "GIT_AUTHOR_NAME": author, "GIT_COMMITTER_NAME": author}
    if when:
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-m", message, env=env)


@pytest.fixture
def decomp_repo(tmp_path):
    """A tiny git repo mirroring the b5-decomp layout: a committed .cpp, no .h."""
    root = tmp_path / "b5-decomp"
    (root / "src" / "World").mkdir(parents=True)
    foo = root / "src" / "World" / "Foo.cpp"
    foo.write_text("// foo\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "add", ".")
    # Old pre-workflow commit (must be filtered out), then two 2026 commits.
    _commit(root, "OldDev", "ancient decomp", when="2020-01-02T00:00:00")
    foo.write_text("// foo v2\n")
    _git(root, "add", ".")
    _commit(root, "Adriwin06", "Continue decomp", when="2026-06-12T10:00:00")
    foo.write_text("// foo v3\n")
    _git(root, "add", ".")
    _commit(root, "JeBobs", "lots more debug", when="2026-06-16T10:00:00")
    return DecompRepo(root=root, branch="main")


def test_history_is_newest_first_and_year_filtered(decomp_repo):
    history = decomp_repo.history("b5-decomp/src/World/Foo.cpp")
    # The 2020 commit is excluded; the two 2026 commits remain, newest first.
    assert [c["name"] for c in history] == ["JeBobs", "Adriwin06"]
    assert history[0]["date"].startswith("2026-06-16")
    assert history[1]["date"].startswith("2026-06-12")
    assert all(c["email"] for c in history)


def test_resolve_uses_latest_qualifying_commit(decomp_repo):
    path, date = decomp_repo.resolve("b5-decomp/src/World/Foo.cpp")
    assert path == "src/World/Foo.cpp"
    assert date.startswith("2026-06-16")


def test_header_falls_back_to_cpp_sibling(decomp_repo):
    # Headers are inlined into the .cpp, so a .h destination resolves to .cpp.
    assert decomp_repo.repo_path("b5-decomp/src/World/Foo.h") == "src/World/Foo.cpp"
    assert decomp_repo.history("b5-decomp/src/World/Foo.h")  # non-empty


def test_missing_file_is_empty(decomp_repo):
    assert decomp_repo.history("b5-decomp/src/World/Missing.cpp") == []
    assert decomp_repo.resolve("b5-decomp/src/World/Missing.cpp") == (None, None)


def test_missing_clone_is_graceful(tmp_path):
    repo = DecompRepo(root=tmp_path / "nope", branch="main")
    assert repo.available is False
    assert repo.history("b5-decomp/src/World/Foo.cpp") == []
    assert repo.resolve("b5-decomp/src/World/Foo.cpp") == (None, None)
