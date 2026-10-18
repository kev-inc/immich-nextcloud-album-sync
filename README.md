# Immich ↔ Nextcloud multi-user folder-album sync + Web UI

Wraps [Salvoxia/immich-folder-album-creator](https://github.com/Salvoxia/immich-folder-album-creator)
so **multiple users/libraries** sync on independent schedules, with a dashboard showing
**sync status · folders synced · interval · manual refresh**.

## How it works

- `sync-dashboard` (built from `./app`) reads `./config/users.yml`
- For each user it schedules `immich_auto_album.py <root_path> <api_url> <api_key> --unattended …`
  using `sync_interval_minutes` (or `cron` if set)
- Web UI on `:8080` shows per-user state + `Sync now` buttons
- Status persists in `./data/status.json`; last-run logs kept per user

## 1. Prerequisites

1. Immich with external libraries pointing at your Nextcloud data.
   The path Immich sees (e.g. `/external_libs/alice_photos`) **must be identical**
   inside this container — that's why we mount the same host dir to `/external_libs`.
2. One Immich **API key per user** with permissions (Immich ≥ v3):
   `asset.read/update`, `album.*`, `albumAsset.create`, `albumUser.*`, `user.read`

## 2. Setup

Two files to fiddle with — everything else is use-as-is:

```bash
cd immich-nextcloud-sync
cp .env.example .env   # edit: URLs, data root, TZ, and ALL per-user keys
chmod 600 .env
# edit config/users.yml — usernames, root_paths, intervals
docker compose up -d --build
```

Open **http://<host>:8080**

(All keys live in `.env`: `API_KEY_<USER>` for Immich, `NC_APP_PASSWORD_<USER>`
for Nextcloud share sync. Prefer docker secret files instead? Uncomment the
`secrets:` example at the bottom of `docker-compose.yml` and use
`api_key_file:` / `nextcloud_app_password_file:` in `users.yml`.)

## Run on another machine (no git clone needed)

This project is just files — pick whichever transfer suits you. In all cases
you only fiddle with **two files**: `.env` (URLs + all per-user keys) and
`config/users.yml` (users, paths, intervals). `docker-compose.yml` is used
as-is. Never commit real keys; `chmod 600 .env` on the target.

**Option A — copy the folder (simplest, no registry):**
```bash
# on this machine (excludes runtime data; .env is excluded — recreate it)
tar czf immich-sync.tar.gz --exclude=data --exclude=.env \
  -C /home/kevin/opencode immich-nextcloud-sync
scp immich-sync.tar.gz user@other-machine:/opt/
# on the other machine
cd /opt && tar xzf immich-sync.tar.gz && cd immich-nextcloud-sync
cp .env.example .env   # edit for that host, incl. all API_KEY_* keys
chmod 600 .env
docker compose up -d --build
```

**Option B — prebuilt image via a registry (no source needed on target):**
```bash
# on this machine (GitHub Container Registry example)
echo $CR_PAT | docker login ghcr.io -u <github-user> --password-stdin
docker buildx build ./app -t ghcr.io/<user>/immich-sync-dashboard:latest --push
# on the other machine: only docker-compose.yml + config/users.yml + .env,
# with DASHBOARD_IMAGE=ghcr.io/<user>/immich-sync-dashboard:latest in .env
echo $CR_PAT | docker login ghcr.io -u <github-user> --password-stdin
docker compose pull && docker compose up -d   # no --build, no ./app needed
```

**Option C — image as a file (offline/air-gapped target):**
```bash
# on this machine
docker compose build
docker save immich-sync-dashboard:latest | gzip > dashboard-image.tar.gz
scp dashboard-image.tar.gz docker-compose.yml user@other:/opt/sync/
# on the other machine (needs compose file + config/users.yml + .env alongside)
docker load < dashboard-image.tar.gz
docker compose up -d   # image already present, no build attempted
```

Whichever route: adjust `.env` per host (`NEXTCLOUD_DATA_ROOT`,
`IMMICH_API_URL`, `TZ`) — the library mount path *inside* the container must
stay `/external_libs` to match `users.yml` and your immich-server mounts.

## Sync modes (per user, `mode:` in `users.yml`)

No database is involved anywhere — neither upstream nor this dashboard stores state.
The script is stateless: every run queries Immich's API for current assets/albums,
and this dashboard only keeps display state (`state`, timestamps, log tails) in
`./data/status.json`. Immich itself is the source of truth.

| `mode` | Behaviour |
|---|---|
| `CREATE` | Create missing albums + append new assets. Never deletes anything (unless `sync_mode` says so). Upstream default. |
| **`UPDATE`** (default here) | `CREATE` **plus pruning of removed/renamed folders**: deletes offline asset records, then deletes albums left empty. This is the "keep albums in sync with Nextcloud folders" mode. |
| `CLEANUP` | ⚠️ Upstream teardown mode: **deletes albums matching the current folder structure** — the opposite of pruning stale ones. Needs `delete_confirm: true`, otherwise dry-run only. |
| `DELETE_ALL` | ⚠️ Deletes every album. Needs `delete_confirm: true`. |

How `UPDATE` converges (important): Immich must rescan the external library
**before** the album sync runs, so deleted/renamed files are marked as offline
assets. Schedule Immich's library scan more often than the album sync, or accept
one run of lag — stale albums disappear within 1–2 cycles. Only Immich asset
records are removed; files on disk are never touched. Note `UPDATE` also deletes
*any* empty album in the account, including manually created ones.

## Nextcloud share sync (folder shares → album shares)

Assuming the same usernames exist in Nextcloud and Immich, folder shares are
mirrored onto the corresponding albums: share `alice_photos/Vacation` with `bob`
in Nextcloud, and the `Vacation` album in Immich is shared with `bob` (read-only
share → `viewer`, anything with write → `editor`). Unsharing in Nextcloud
removes the Immich share on the next run.

How it works: before each user's album sync, the dashboard queries Nextcloud's
OCS Share API as that user and writes upstream `.albumprops` sidecar files
(`share_with` + `inherit: true`, so sub-albums inherit — matching Nextcloud
semantics) into the shared folders. The sync then runs with
`--read-album-properties --update-album-props-mode 2`, which makes upstream
enforce the share state (add / fix roles / remove). Only files created by the
dashboard (tracked in `./data`) are ever written or deleted; pre-existing manual
`.albumprops` containing `share_with` are left alone and reported as skipped.

Setup per user:

1. Nextcloud → user Settings → Security → *Devices & sessions* → *Create new
   app password*.
2. Put it in `.env` as `NC_APP_PASSWORD_<USER>` (e.g. `NC_APP_PASSWORD_ALICE`).
   Omit the line to skip share sync for that user.
3. Set `nextcloud_url` (or `NEXTCLOUD_URL` in `.env`). Per-user `nextcloud_user`
   defaults to the username; `nextcloud_base_path` defaults to the basename of
   the first `root_paths` entry (e.g. `/external_libs/alice_photos` ↔ Nextcloud
   `/alice_photos`) — set it explicitly if your layout differs, or use
   `path_maps: [{nextcloud: ..., local: ...}]` for several mappings.
4. The dashboard shows share-sync state per user (`🔗 Share sync` row).

Full share enforcement (including removals) kicks in only after share sync has
succeeded at least once — until then the sync runs without
`--update-album-props-mode 2`, so a misconfigured password can never strip
manually created Immich shares.

Limitations: only user-to-user **folder** shares map cleanly. Group shares,
public links and single-file shares are counted and skipped (visible in the
dashboard detail line). If you share a deep subfolder while `album_levels`
produces coarser albums, the share lands on the parent album.

## 3. Adding another user

1. Add to `.env`: `API_KEY_CAROL=...` (+ `NC_APP_PASSWORD_CAROL=...` for share sync)
2. Add a block in `config/users.yml`:
```yaml
  - username: carol
    root_paths:
      - /external_libs/carol_photos
    album_levels: 1
    sync_interval_minutes: 60
```
3. Ensure `/external_libs/carol_photos` exists (i.e. `$NEXTCLOUD_DATA_ROOT/carol_photos` on host)
4. `docker compose up -d --build` (or `up -d` after `pull` on a prebuilt-image machine)

Per-user options mirror the upstream script: `mode`, `album_levels`, `album_separator`,
`chunk_size`, `fetch_chunk_size`, `sync_mode` (only applies to `mode: CREATE`;
`UPDATE` always forces `2`), `ignore`, `path_filter`, `insecure`,
`find_assets_in_albums`, `cron` (e.g. `"0 */2 * * *"` overrides interval).

## 4. Manual sync

- UI: **Sync now** per user, or **Sync all now**
- CLI: `curl -XPOST http://localhost:8080/api/sync/alice`
- Status JSON: `curl http://localhost:8080/api/status`

## 5. Notes / Nextcloud mapping

Typical layout — Nextcloud data dir on host, one folder per user:

```
/mnt/nextcloud-data/
  alice_photos/      → users.yml: /external_libs/alice_photos
  bob_photos/        → users.yml: /external_libs/bob_photos
```

Mount the **same host path** into `immich-server` (e.g. `- /mnt/nextcloud-data:/external_libs`)
and register each subfolder as an external library in Immich, then run an Immich
external-library scan so assets exist before album sync.
