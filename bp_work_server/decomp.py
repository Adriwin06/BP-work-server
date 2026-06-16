"""Per-file commit dating from a local clone of the decomp source repo.

Backfilled Live Events ("workflow commit delta" / "legacy pre-server
attribution") all share a single import timestamp and a single, meaningless
``detail.commit`` SHA, so neither tells us when the work actually happened. The
honest signal is the *file itself*: the last commit that touched a TU's
destination file in the ``b5-decomp`` repo. This module reads that from a local
clone with ``git log`` -- free and unmetered, unlike per-path GitHub API calls,
which would blow the rate limit across hundreds of files.

Headers are decompiled inline into their ``.cpp``, so a ``*.h`` destination has
no file of its own in the repo; we transparently fall back to the ``.cpp``
sibling, which also fixes the destination links that used to 404 on ``.h`` TUs.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

# The decomp source lives next to the workflow checkout by default; both sit in
# the persistent data dir so they survive code deploys.
DEFAULT_DECOMP_REPO = "https://github.com/Adriwin06/b5-decomp.git"
DEFAULT_DECOMP_ROOT = "/var/lib/bp-work-server/b5-decomp"
DEFAULT_DECOMP_BRANCH = os.environ.get("BP_GITHUB_REF", "dev")

# How long the local clone is trusted before we fetch the branch again. New
# decomp commits only shift a file's "latest commit" date, so staleness here is
# cosmetic; a generous TTL keeps git fetch out of the hot path.
REFRESH_TTL = 900

# Destination paths are stored as "b5-decomp/src/...": the repo-name prefix is
# stripped to get the path relative to the clone root.
_REPO_PREFIX = "b5-decomp/"


class DecompRepo:
    """Resolves TU destination paths to the real file and its last commit date.

    All public methods are safe to call from FastAPI's threadpool: results are
    memoised under a lock and ``git`` is shelled out synchronously. Every method
    degrades to ``None`` when the clone is missing or git fails, so the dashboard
    simply falls back to the stored event timestamp.
    """

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        branch: str | None = None,
    ) -> None:
        self.root = Path(root or os.environ.get("BP_DECOMP_ROOT", DEFAULT_DECOMP_ROOT))
        self.branch = branch or os.environ.get("BP_DECOMP_BRANCH", DEFAULT_DECOMP_BRANCH)
        self._lock = threading.Lock()
        # path-relative-to-root -> (existing_path | None, iso_date | None)
        self._cache: dict[str, tuple[str | None, str | None]] = {}
        self._refreshed_at = 0.0

    @property
    def available(self) -> bool:
        return (self.root / ".git").exists()

    def _git(self, *args: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout

    def _maybe_refresh(self) -> None:
        """Fetch the branch at most once per TTL; clears the memo on update."""
        if not self.available:
            return
        now = time.time()
        if now - self._refreshed_at < REFRESH_TTL:
            return
        with self._lock:
            if time.time() - self._refreshed_at < REFRESH_TTL:
                return
            ok = self._git("fetch", "--quiet", "origin", self.branch)
            if ok is not None:
                self._git("reset", "--hard", f"origin/{self.branch}")
                self._cache.clear()
            # Record the attempt regardless so a flaky network does not make
            # every request pay the fetch cost.
            self._refreshed_at = time.time()

    @staticmethod
    def _repo_relative(dest_path: str) -> str:
        path = dest_path.removeprefix(_REPO_PREFIX)
        return path.lstrip("/")

    def _resolve_uncached(self, rel: str) -> tuple[str | None, str | None]:
        candidates = [rel]
        # Headers are inlined into the .cpp, so a missing *.h maps to its sibling.
        if rel.endswith(".h"):
            candidates.append(rel[:-2] + ".cpp")
        for candidate in candidates:
            if not (self.root / candidate).is_file():
                continue
            out = self._git("log", "-1", "--format=%aI", "--", candidate)
            date = (out or "").strip() or None
            return candidate, date
        return None, None

    def resolve(self, dest_path: str | None) -> tuple[str | None, str | None]:
        """Map a TU destination to ``(existing_repo_path, iso_commit_date)``.

        Both elements are ``None`` when the file cannot be located. The path is
        relative to the repo root (e.g. ``src/.../Foo.cpp``).
        """
        if not dest_path:
            return None, None
        self._maybe_refresh()
        rel = self._repo_relative(dest_path)
        with self._lock:
            hit = self._cache.get(rel)
        if hit is not None:
            return hit
        result = self._resolve_uncached(rel)
        with self._lock:
            self._cache[rel] = result
        return result

    def commit_date(self, dest_path: str | None) -> str | None:
        return self.resolve(dest_path)[1]

    def repo_path(self, dest_path: str | None) -> str | None:
        return self.resolve(dest_path)[0]
