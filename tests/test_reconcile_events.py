from __future__ import annotations

import os
import subprocess

from bp_work_server.decomp import DecompRepo
from bp_work_server.reconcile_events import (
    RECONSTRUCTED_SOURCE,
    reconcile_review_events_from_decomp,
)
from bp_work_server.store import WorkStore, iso


def _git(root, *args, env=None):
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=env
    )


def _commit(root, author, email, message, when):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": author,
        "GIT_COMMITTER_NAME": author,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_EMAIL": email,
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
    }
    _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-m", message, env=env)


def test_reconcile_review_events_from_decomp_is_idempotent_and_preserves_real_events(tmp_path):
    repo = tmp_path / "b5-decomp"
    (repo / "src" / "GameSource").mkdir(parents=True)
    a = repo / "src" / "GameSource" / "A.cpp"
    b = repo / "src" / "GameSource" / "B.cpp"
    a.write_text("// a\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "add", str(a))
    _commit(repo, "Nathan V.", "jebcraftserver@gmail.com", "JeBobs work", "2026-06-16T10:00:00")
    b.write_text("// b\n")
    _git(repo, "add", str(b))
    _commit(repo, "Derneuere", "der@example.test", "normal workflow", "2026-06-16T11:00:00")

    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    store.create_worker("JeBobs")
    store.create_worker("Derneuere")
    with store.users_connect() as con:
        con.execute(
            "INSERT INTO worker_alias(alias, username, kind) VALUES(?, ?, ?)",
            ("jebcraftserver@gmail.com", "JeBobs", "git-email"),
        )
    with store.connect() as con:
        con.execute(
            """
            INSERT INTO tu(id, source, status, n_funcs, n_decfigs, dest_path, updated_at)
            VALUES
              ('GameSource/A.cpp', 'decfigs', 'done', 1, 1, 'b5-decomp/src/GameSource/A.cpp', ?),
              ('GameSource/B.cpp', 'decfigs', 'done', 1, 1, 'b5-decomp/src/GameSource/B.cpp', ?)
            """,
            (iso(), iso()),
        )
        con.execute(
            "INSERT INTO event(ts, tu_id, agent, action, detail_json) VALUES(?,?,?,?,?)",
            (iso(), "GameSource/B.cpp", "Derneuere", "review_pass", "{}"),
        )

    decomp = DecompRepo(root=repo, branch="main")

    dry = reconcile_review_events_from_decomp(store, decomp, actors={"JeBobs"}, apply=False)
    applied = reconcile_review_events_from_decomp(store, decomp, actors={"JeBobs"}, apply=True)
    again = reconcile_review_events_from_decomp(store, decomp, actors={"JeBobs"}, apply=True)

    assert dry.inserted == 1
    assert applied.inserted == 1
    assert again.inserted == 0
    assert again.skipped_existing_reconstructed == 1
    with store.connect() as con:
        rows = con.execute(
            """
            SELECT ts, tu_id, agent, action, detail_json
            FROM event
            WHERE agent='JeBobs'
              AND action='review_pass'
            """
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["ts"].startswith("2026-06-16T10:00:00")
    assert rows[0]["tu_id"] == "GameSource/A.cpp"
    assert rows[0]["action"] == "review_pass"
    assert f'"source": "{RECONSTRUCTED_SOURCE}"' in rows[0]["detail_json"]
