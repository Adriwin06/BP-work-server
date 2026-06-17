from __future__ import annotations

import json

from bp_work_server.attribution_cache import warm_attribution_cache
from bp_work_server.store import WorkStore, iso


def make_store(tmp_path) -> WorkStore:
    store = WorkStore(tmp_path / "work.sqlite3")
    store.migrate()
    with store.connect() as con:
        con.execute(
            """
            INSERT INTO tu(id, source, status, n_funcs, n_decfigs, dest_path, updated_at)
            VALUES
              ('GameSource/A.cpp', 'decfigs', 'done', 2, 2, 'b5-decomp/src/GameSource/A.cpp', ?),
              ('GameSource/B.cpp', 'decfigs', 'todo', 1, 1, 'b5-decomp/src/GameSource/B.cpp', ?),
              ('class:Utility', 'class', 'todo', 1, 0, NULL, ?)
            """,
            (iso(), iso(), iso()),
        )
        con.execute(
            "INSERT INTO func(name, tu_id, status) VALUES('A::Run', 'GameSource/A.cpp', 'reviewed')"
        )
        con.execute(
            "INSERT INTO func(name, tu_id, status) VALUES('A::Stop', 'GameSource/A.cpp', 'todo')"
        )
        con.execute(
            "INSERT INTO func(name, tu_id, status) VALUES('Utility::Fn', 'class:Utility', 'reviewed')"
        )
    return store


def test_warm_attribution_cache_populates_cacheable_reviewed_work(tmp_path):
    store = make_store(tmp_path)

    class FakeDecomp:
        def revision(self):
            return "rev-current"

        def history(self, dest_path):
            return [{"date": "2026-06-17T10:00:00+00:00", "name": "Niaz", "email": "n@example.test"}]

        def contributors(self, dest_path):
            return {
                "path": dest_path.removeprefix("b5-decomp/"),
                "basis": "surviving_lines",
                "contributors": [{"name": "Niaz", "email": "n@example.test", "lines": 4}],
            }

        def function_contributors(self, dest_path, function_name):
            return {
                "path": dest_path.removeprefix("b5-decomp/"),
                "basis": "surviving_lines",
                "line_range": [10, 20],
                "function_range_found": True,
                "contributors": [{"name": "Niaz", "email": "n@example.test", "lines": 3}],
            }

    result = warm_attribution_cache(store, FakeDecomp())

    assert result.files_cached == 1
    assert result.functions_cached == 1
    state = store.dashboard_state(attribution_repo_rev="rev-current")
    assert state["attribution_cache"]["file_cached"] == 1
    assert state["attribution_cache"]["file_total"] == 1
    assert state["attribution_cache"]["function_cached"] == 1
    assert state["attribution_cache"]["function_total"] == 1
    assert state["attribution_cache"]["file_complete"] is True
    assert state["attribution_cache"]["function_complete"] is True
    with store.connect() as con:
        rows = con.execute(
            "SELECT scope, dest_path, function_name, payload_json FROM attribution_cache ORDER BY scope"
        ).fetchall()
    assert [(row["scope"], row["function_name"]) for row in rows] == [("file", ""), ("function", "A::Run")]
    assert json.loads(rows[0]["payload_json"])["contributors"]["contributors"][0]["name"] == "Niaz"
