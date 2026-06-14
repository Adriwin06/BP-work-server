# BP Work Server Protocol

The server coordinates claims and status for Burnout Paradise decompilation work.
It does not store IDA exports, leaked references, dossiers, or reconstructed code.
Those stay in `BP-Decomp_Workflow` and Git.

## Core Rules

- The server is authoritative for live TU status.
- `POST /claims` is the only way to claim work.
- Claims are leases. Agents must heartbeat or the server returns stale work to `todo`.
- Git remains authoritative for code review and source changes.
- A TU should become `done` only after the local compile/review policy has passed.

## Statuses

| Status | Meaning |
| --- | --- |
| `todo` | Available to claim. |
| `in_progress` | Claimed by an agent with an active lease. |
| `compiled` | Local compile gate passed; waiting for review or merge policy. |
| `done` | Accepted complete work. |
| `blocked` | Not claimable until manually unblocked. |

## Endpoints

### `GET /health`

Returns server liveness and version.

### `GET /`

Serves the live read-only dashboard for humans. It shows progress counts, active
agents, active work, the next queue, imported goals, blocked TUs, and recent
events.

### `POST /admin/import?workflow_root=...&reset=false`

Imports `progress/tu_index.json`, `progress/status.json`, `progress/tu_deps.json`,
and `progress/goals.json` from the workflow repo.

Use `reset=true` for the initial import or for rebuilding a disposable dev server.
Avoid `reset=true` on a live server unless you intentionally want to discard claims.

### `GET /next?n=5&goal=boot-trace`

Returns dependency-ranked `todo` TUs. If `goal` is omitted, the imported active goal
is used. Goal ranking counts unresolved dependencies inside the goal only, matching
the existing `work next` behavior.

### `POST /claims`

Atomically claims a TU.

Request:

```json
{
  "tu": "GameSource/Foo/Bar.cpp",
  "agent": "adrian-codex-1",
  "lease_seconds": 7200,
  "force": false
}
```

Responses:

- `201`: claim succeeded.
- `409`: TU is already claimed, compiled, done, or blocked.
- `404`: unknown TU.

### `POST /claims/next`

Atomically ranks the `todo` queue and claims the top `n` TUs for one agent in a
single transaction. This is the "checkout" path: concurrent agents calling it get
**distinct** work, with no rank-then-claim race window. Leases auto-expire, so
over-claiming self-heals (claim 5, finish 1, the rest return to `todo`).

Request:

```json
{
  "agent": "adrian-codex-1",
  "n": 5,
  "lease_seconds": 7200,
  "goal": "boot-trace"
}
```

`goal` is optional; when omitted the imported active goal is used (same ranking as
`GET /next`). Response returns the claims that succeeded — possibly fewer than `n`
if the queue is short:

```json
{
  "active_goal": "boot-trace",
  "count": 5,
  "claimed": [
    {"claimed": true, "tu": "GameSource/Foo/Bar.cpp", "status": "in_progress",
     "owner": "adrian-codex-1", "lease_expires_at": "2026-06-14T12:00:00+00:00"}
  ]
}
```

### `POST /claims/{tu}/heartbeat`

Renews the current owner's lease.

Request:

```json
{
  "agent": "adrian-codex-1",
  "lease_seconds": 7200
}
```

### `DELETE /claims/{tu}`

Releases the current owner's claim and returns the TU to `todo`.

Request body:

```json
{
  "agent": "adrian-codex-1"
}
```

### `POST /tu/{tu}/compiled`

Marks a claimed TU as `compiled`.

Request:

```json
{
  "agent": "adrian-codex-1",
  "notes": "compile gate passed",
  "commit": "optional-git-commit",
  "files": ["b5-decomp/src/GameSource/Foo/Bar.cpp"]
}
```

### `POST /tu/{tu}/review`

Records the reviewer verdict. `pass` marks the TU `done`; `fail` returns it to
`in_progress` for the reporting agent.

Request:

```json
{
  "agent": "reviewer-or-owner",
  "verdict": "pass",
  "notes": "trivial; gate-only",
  "commit": "optional-git-commit"
}
```

### `POST /tu/{tu}/block`

Marks a TU `blocked`.

Request:

```json
{
  "agent": "adrian-codex-1",
  "reason": "Vendor code; exists in PC lib or vendor source."
}
```

### `POST /tu/{tu}/unblock`

Returns a blocked TU to `todo`.

### `GET /snapshot?include_tus=true`

Returns status counts and, optionally, the TU table.

### `GET /events?after=0&limit=200`

Returns the append-only event log for polling dashboards or syncing local caches.

### `GET /events/stream?after=0`

Server-sent events stream used by the dashboard. It emits `work-event` messages
when new events are available and periodic `tick` messages so browsers refresh
even when a lease expiry changes state without a user action.

### `GET /dashboard/state`

Dashboard-optimized summary. It returns aggregate progress, active agents,
active work, blocked work, recent events, imported goals, and the next ranked
TUs without requiring the browser to pull the full TU table.

## `work.py` Integration

The server is **optional and invite-only**: the local workflow runs fully standalone and
is only coordinated when `WORK_SERVER` is set. Config lives in a repo-root `.env` (copy
`.env.example`), which `work` auto-loads — not shell exports. Only people given the URL
turn it on:

```
# .env (git-ignored; copy from .env.example). Leave WORK_SERVER unset to work locally.
WORK_SERVER=http://your-server:8765   # only if you were given a URL
WORK_AGENT=adrian-codex-1
```

```
work claim -n 1                 # checkout the next ready TU (atomic when a server is set)
work start "GameSource/Foo/Bar.cpp"   # or claim one specific TU by id
```

Mapping:

| Local command | Server call |
| --- | --- |
| `work next` | `GET /next` (preview only — reserves nothing) |
| `work claim [-n N]` | `POST /claims/next` (atomic checkout of the next N) |
| `work start <tu>` | `POST /claims` |
| `work submit <tu>` pass | local compile, then `POST /tu/{tu}/compiled` |
| `work review <tu> --verdict pass` | `POST /tu/{tu}/review` |
| `work block <tu>` | `POST /tu/{tu}/block` |
| `work unblock <tu>` | `POST /tu/{tu}/unblock` |
| `work server-reset [--to REF]` | `POST /admin/sync` with `reset=true` |

The local `ledger.sqlite` can remain a cache for dossiers, dependencies, and
offline work. The server exists to prevent duplicate claims.

## Two-store model and reverting

The server is **derived from** the workflow repo, not parallel to it. `progress/status.json`
is the durable record (the `done`/`blocked` states tied to committed code) and the seed
for both `/admin/sync` and a fresh `work bootstrap`. Live claims, leases, `owner`, the
transient `in_progress`/`compiled` states, and the event log are born on the server.

When `WORK_SERVER` is set, the workflow CLI writes only durable statuses into
`status.json` (no `owner`, no `in_progress`) so concurrent agents don't collide on git.

To revert everything to a known-good commit, run `work server-reset --to <ref>`: it
`git reset`s the workflow repo + `b5-decomp`, drops the local `ledger.sqlite` cache, and
re-seeds the server via `POST /admin/sync` with `reset=true`. The reset discards live
claims and the event log (claims are ephemeral; event history is not recoverable), so it
is the deliberate clean-slate path.
