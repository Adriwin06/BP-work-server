# Automated build → download button

On every commit to `b5-decomp` (`dev`), a self-hosted Windows runner rebuilds the
game, bundles the Google Drive assets next to the exe, zips it, and uploads it to
the work server. The dashboard shows a **Download build** button pointing at the
newest zip.

```
 b5-decomp push (dev)
        │
        ▼
 self-hosted Windows runner (MSVC)
   rclone sync Drive ──► assets mirror  (only changed files transfer)
   cmake --build       ──► game exe
   bundle exe + assets ──► zip
        │  POST /admin/builds  (admin X-Work-Token)
        ▼
 work server (adriwin.fr, Linux)
   stores zip under BP_DOWNLOADS_DIR, records the build
        │
        ▼
 dashboard "Download build" button ──► /download/latest
```

Why a Windows runner: the build needs MSVC (`cl`), which the Linux download server
can't run. So the builder and the download host are different machines; the runner
pushes the finished zip to the server over HTTPS.

## Files (all live in the **b5-decomp** repo)

| This repo (BP-work-server) | Copy to b5-decomp |
| --- | --- |
| `deploy/ci/build-and-publish.yml` | `.github/workflows/build-and-publish.yml` |
| `deploy/ci/publish-build.ps1` | `ci/publish-build.ps1` |

The server-side pieces (upload endpoint, download routes, button) are already part
of BP-work-server — nothing to install there beyond the config below.

## One-time setup

### 1. Self-hosted Windows runner

Register a runner on a Windows box that has **MSVC + CMake** (the same toolchain
the per-TU `cl` gate uses) with the labels the workflow expects:

```
labels: self-hosted, windows, msvc
```

b5-decomp → Settings → Actions → Runners → New self-hosted runner. Add `msvc` as a
custom label. Keep it running as a service so scheduled builds fire unattended.

### 2. rclone + the Drive remote (on the runner)

```powershell
winget install Rclone.Rclone           # or scoop install rclone
rclone config                          # n) new remote, type "drive"
```

Pin the remote to the **folder ID**, not the share link, so re-sharing the folder
never breaks the build. In `rclone config` set `root_folder_id` to
`1CgSSjtenfAc_Ps6_JLhtGhTly1K5n_HO` (the folder from the Drive URL). For an
unattended runner use a **service account** (`service_account_file`) instead of the
interactive OAuth token so it never needs a browser re-auth. Name it e.g.
`gdrive` → the workflow passes `gdrive:` as `RCLONE_REMOTE`.

Verify:

```powershell
rclone lsf gdrive:            # lists the asset folder
```

### 3. Mint an admin token (on the server)

The runner authenticates to `/admin/builds` with an admin `X-Work-Token`:

```powershell
bp-work-server --db data\bp-work.sqlite3 worker add ci-build --admin
# prints: WORK_AGENT=<token>   <-- this is WORK_PUBLISH_TOKEN
```

### 4. Secrets & variables (in b5-decomp)

Settings → Secrets and variables → Actions:

| Kind | Name | Value |
| --- | --- | --- |
| Variable | `WORK_SERVER` | `https://adriwin.fr` |
| Variable | `RCLONE_REMOTE` | `gdrive:` (or `gdrive:Subfolder`) |
| Secret | `WORK_PUBLISH_TOKEN` | the admin token from step 3 |

### 5. nginx upload limit (on the server) — important

The zip is uploaded *through* nginx to the app. nginx's default
`client_max_body_size` is **1 MB**, which will reject a game bundle with `413`.
Raise it for the upload path (a game download served back out is fine, but the
upload needs headroom):

```nginx
location /admin/builds {
    client_max_body_size 0;      # or e.g. 8g
    proxy_request_buffering off;  # stream to the app instead of buffering to disk
    proxy_read_timeout 3600s;
    proxy_pass http://127.0.0.1:8765;
}
```

Reload nginx afterward.

## Server configuration (optional)

| Env var | Default | Purpose |
| --- | --- | --- |
| `BP_DOWNLOADS_DIR` | `data/downloads` | Where published zips are stored (git-ignored; survives deploys). |
| `BP_KEEP_BUILDS` | `5` | How many recent builds to keep on disk; older zips are pruned. |

## Endpoints

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `POST` | `/admin/builds` | admin `X-Work-Token` | CI uploads a build zip. |
| `GET` | `/api/builds` | public | Latest + recent builds (JSON), for the dashboard. |
| `GET` | `/download/latest` | public | Stream the newest build. |
| `GET` | `/download/{id}` | public | Stream a specific build. |

> **Public reminder:** `/download/*` is unauthenticated for now. The bundle ships
> the Drive assets alongside the exe — if those are original game files, gate this
> before making the site public. Password protection is the planned next step.

## Notes

- **Asset changes without a commit:** `rclone sync` always mirrors the current
  Drive state, so each build reflects whatever is in the folder *now*. A daily
  `schedule` in the workflow also rebuilds so asset-only edits get published even
  when the source is quiet. The recorded `asset_manifest_hash` tells you which
  asset set a given build shipped.
- **Large downloads:** builds stream from disk via the app. If traffic grows,
  serve `/download/*` directly from nginx (`X-Accel-Redirect`) so the Python
  process isn't in the byte path.
