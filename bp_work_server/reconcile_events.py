from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from bp_work_server.decomp import DecompRepo
from bp_work_server.github import login_from_noreply_email
from bp_work_server.store import WorkStore


RECONSTRUCTED_SOURCE = "b5-decomp commit reconstruction"
RECONSTRUCTED_REASON = "No server workflow event was recorded; inferred from b5-decomp git history."


@dataclass
class ReconcileResult:
    scanned_tus: int = 0
    scanned_commits: int = 0
    inserted: int = 0
    skipped_existing_real: int = 0
    skipped_existing_reconstructed: int = 0
    skipped_actor_filter: int = 0
    skipped_unresolved_actor: int = 0


def _actor_for_commit(commit: dict[str, str | None], aliases: dict[str, str]) -> str | None:
    email = (commit.get("email") or "").strip()
    candidates = [
        login_from_noreply_email(email),
        email,
        commit.get("name"),
    ]
    for candidate in candidates:
        cleaned = (candidate or "").strip()
        if not cleaned:
            continue
        return aliases.get(cleaned.lower(), cleaned)
    return None


def _has_real_review(con: sqlite3.Connection, tu_id: str, actor: str) -> bool:
    return bool(
        con.execute(
            """
            SELECT 1
            FROM event
            WHERE tu_id=?
              AND action='review_pass'
              AND agent=?
              AND COALESCE(json_extract(detail_json, '$.source'), '') NOT IN (
                'workflow commit delta',
                'legacy pre-server attribution',
                ?
              )
            LIMIT 1
            """,
            (tu_id, actor, RECONSTRUCTED_SOURCE),
        ).fetchone()
    )


def _has_reconstructed_commit(
    con: sqlite3.Connection, tu_id: str, actor: str, commit: dict[str, str | None]
) -> bool:
    commit_hash = commit.get("commit")
    if commit_hash:
        row = con.execute(
            """
            SELECT 1
            FROM event
            WHERE tu_id=?
              AND action='review_pass'
              AND agent=?
              AND json_extract(detail_json, '$.source')=?
              AND json_extract(detail_json, '$.commit')=?
            LIMIT 1
            """,
            (tu_id, actor, RECONSTRUCTED_SOURCE, commit_hash),
        ).fetchone()
        return bool(row)
    row = con.execute(
        """
        SELECT 1
        FROM event
        WHERE tu_id=?
          AND action='review_pass'
          AND agent=?
          AND json_extract(detail_json, '$.source')=?
          AND json_extract(detail_json, '$.git_author_date')=?
        LIMIT 1
        """,
        (tu_id, actor, RECONSTRUCTED_SOURCE, commit.get("date")),
    ).fetchone()
    return bool(row)


def _event_detail(dest_path: str, commit: dict[str, str | None]) -> dict[str, Any]:
    return {
        "source": RECONSTRUCTED_SOURCE,
        "reconstructed": True,
        "reason": RECONSTRUCTED_REASON,
        "commit": commit.get("commit"),
        "git_author_date": commit.get("date"),
        "git_author_name": commit.get("name"),
        "git_author_email": commit.get("email"),
        "dest_path": dest_path,
    }


def reconcile_review_events_from_decomp(
    store: WorkStore,
    decomp: DecompRepo,
    *,
    actors: set[str] | None = None,
    apply: bool = False,
) -> ReconcileResult:
    """Append missing reconstructed ``review_pass`` events from b5-decomp history.

    Normal server-submitted workflow events are left alone. A reconstructed event
    is added only when the same TU/actor does not already have a real
    ``review_pass`` and the exact commit was not previously reconstructed.
    """
    store.migrate()
    aliases, _profiles = store.actor_maps()
    actor_filter = {actor.lower() for actor in actors or set()}
    result = ReconcileResult()

    with store.connect() as con:
        rows = con.execute(
            """
            SELECT id, dest_path
            FROM tu
            WHERE status='done'
              AND dest_path IS NOT NULL
            ORDER BY id
            """
        ).fetchall()
        result.scanned_tus = len(rows)

        for row in rows:
            tu_id = row["id"]
            dest_path = row["dest_path"]
            for commit in decomp.history(dest_path):
                result.scanned_commits += 1
                actor = _actor_for_commit(commit, aliases)
                if not actor:
                    result.skipped_unresolved_actor += 1
                    continue
                if actor_filter and actor.lower() not in actor_filter:
                    result.skipped_actor_filter += 1
                    continue
                if _has_real_review(con, tu_id, actor):
                    result.skipped_existing_real += 1
                    continue
                if _has_reconstructed_commit(con, tu_id, actor, commit):
                    result.skipped_existing_reconstructed += 1
                    continue

                if apply:
                    con.execute(
                        """
                        INSERT INTO event(ts, tu_id, agent, action, detail_json)
                        VALUES(?, ?, ?, 'review_pass', ?)
                        """,
                        (
                            commit["date"],
                            tu_id,
                            actor,
                            json.dumps(_event_detail(dest_path, commit), sort_keys=True),
                        ),
                    )
                result.inserted += 1

    return result
