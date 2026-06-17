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
# decomp commits only add to a file's history, so staleness here is cosmetic; a
# generous TTL keeps git fetch out of the hot path.
REFRESH_TTL = 900

# Only commits from this year onward count: the workflow (and this server's
# attribution) began in 2026, so earlier commits belong to the abandoned
# pre-workflow decomp and must not be reconstructed as Live Events.
MIN_COMMIT_YEAR = int(os.environ.get("BP_DECOMP_MIN_YEAR", "2026"))

# Field separator for ``git log`` output; ASCII unit separator never appears in
# hashes, author names, or ISO dates, so it parses unambiguously.
_FIELD_SEP = "\x1f"

# Destination paths are stored as "b5-decomp/src/...": the repo-name prefix is
# stripped to get the path relative to the clone root.
_REPO_PREFIX = "b5-decomp/"


class DecompRepo:
    """Reads per-file commit history from a local clone of the decomp source.

    A TU's destination file is the honest record of who did the work and when:
    each commit that touched it (from MIN_COMMIT_YEAR on) is one unit of work by
    its author on its date. ``history`` exposes that list; ``resolve`` and the
    thin wrappers expose the existing path and the latest commit for callers that
    only need a single date or link.

    All public methods are safe to call from FastAPI's threadpool: results are
    memoised under a lock and ``git`` is shelled out synchronously. Everything
    degrades to empty/None when the clone is missing or git fails, so the
    dashboard simply falls back to stored data.
    """

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        branch: str | None = None,
    ) -> None:
        self.root = Path(root or os.environ.get("BP_DECOMP_ROOT", DEFAULT_DECOMP_ROOT))
        self.branch = branch or os.environ.get("BP_DECOMP_BRANCH", DEFAULT_DECOMP_BRANCH)
        self._lock = threading.Lock()
        # path-relative-to-root -> {"path": existing_path|None, "history": [...]}
        self._cache: dict[str, dict] = {}
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

    def _existing_path(self, rel: str) -> str | None:
        """The file that exists for ``rel`` (a missing *.h maps to its .cpp)."""
        candidates = [rel]
        # Headers are inlined into the .cpp, so a missing *.h has no file of its own.
        if rel.endswith(".h"):
            candidates.append(rel[:-2] + ".cpp")
        for candidate in candidates:
            if (self.root / candidate).is_file():
                return candidate
        return None

    def _resolve_uncached(self, rel: str) -> dict:
        path = self._existing_path(rel)
        if not path:
            return {"path": None, "history": []}
        out = self._git(
            "log",
            f"--format=%H{_FIELD_SEP}%aI{_FIELD_SEP}%an{_FIELD_SEP}%ae",
            "--",
            path,
        )
        history: list[dict[str, str | None]] = []
        for line in (out or "").splitlines():
            commit, _, rest = line.strip().partition(_FIELD_SEP)
            date, _, rest = rest.partition(_FIELD_SEP)
            name, _, email = rest.partition(_FIELD_SEP)
            if len(date) < 4 or not date[:4].isdigit():
                continue
            if int(date[:4]) < MIN_COMMIT_YEAR:
                continue
            history.append(
                {
                    "commit": commit.strip() or None,
                    "date": date,
                    "name": name.strip() or None,
                    "email": email.strip() or None,
                }
            )
        # git log is newest-first, which is the order we want.
        return {"path": path, "history": history}

    def _record(self, dest_path: str | None) -> dict:
        if not dest_path:
            return {"path": None, "history": []}
        self._maybe_refresh()
        rel = self._repo_relative(dest_path)
        with self._lock:
            hit = self._cache.get(rel)
        if hit is not None:
            return hit
        record = self._resolve_uncached(rel)
        with self._lock:
            self._cache[rel] = record
        return record

    def history(self, dest_path: str | None) -> list[dict[str, str | None]]:
        """Commits (newest-first) that touched the file, from MIN_COMMIT_YEAR on.

        Each entry is ``{"commit": sha, "date": iso, "name": git_author,
        "email": git_email}``; empty when the file is absent or has no
        qualifying commits. The caller maps email -> GitHub login for display.
        """
        return self._record(dest_path)["history"]

    def resolve(self, dest_path: str | None) -> tuple[str | None, str | None]:
        """Map a TU destination to ``(existing_repo_path, latest_commit_date)``."""
        record = self._record(dest_path)
        history = record["history"]
        return record["path"], (history[0]["date"] if history else None)

    def commit_date(self, dest_path: str | None) -> str | None:
        return self.resolve(dest_path)[1]

    def repo_path(self, dest_path: str | None) -> str | None:
        return self._record(dest_path)["path"]
