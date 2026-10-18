"""Multi-user Immich folder-album sync orchestrator + dashboard.

Wraps Salvoxia/immich-folder-album-creator (immich_auto_album.py) to:
  - sync MULTIPLE users, each with their own API key + root_path libraries
    (typically Nextcloud data folders exposed to Immich as external libraries)
  - schedule each user independently (interval minutes or cron expression)
  - expose a Web UI: sync status, folders synced, interval, manual refresh
"""
import copy
import datetime as dt
import json
import os
import re
import subprocess
import threading
from pathlib import Path

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from flask import Flask, jsonify, redirect, render_template, request, url_for

import share_sync

CONFIG_PATH = Path(os.environ.get("USERS_YML", "/app/config/users.yml"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
STATUS_FILE = DATA_DIR / "status.json"
SCRIPT = Path(os.environ.get("SYNC_SCRIPT", "/app/immich_auto_album.py"))
PORT = int(os.environ.get("PORT", "8080"))

# Single global behaviour for all users (from the `settings:` block).
global_settings: dict = {}

app = Flask(__name__)
sched = BackgroundScheduler(timezone=os.environ.get("TZ", "UTC"))
status_lock = threading.Lock()
# username -> status dict
status: dict = {}
# username -> resolved user config
users_cfg: dict = {}
immich_api_url: str = ""
jobs_meta: dict = {}  # username -> {"interval": str, "cron": str|None}


def load_config():
    global users_cfg, immich_api_url, jobs_meta, global_settings
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    immich_api_url = os.environ.get(
        "IMMICH_API_URL", cfg.get("immich_api_url", "http://immich_server:2283/api")
    )
    settings = cfg.get("settings", cfg.get("defaults", {})) or {}
    # Top-level keys act as global fallbacks unless overridden per user.
    settings.setdefault("nextcloud_url", cfg.get("nextcloud_url", ""))
    global_settings = settings
    for u in cfg.get("users", []) or []:
        name = u.get("username")
        if not name:
            continue
        merged = {**settings, **u}
        # resolve api key (inline or file, for docker secrets)
        api_key = merged.get("api_key") or os.environ.get(f"API_KEY_{name.upper()}")
        key_file = merged.get("api_key_file")
        if not api_key and key_file and Path(key_file).exists():
            api_key = Path(key_file).read_text().strip()
        merged["_api_key"] = api_key or ""
        merged["_api_url"] = merged.get("immich_api_url", immich_api_url)
        paths = merged.get("root_paths") or ([merged["root_path"]] if merged.get("root_path") else [])
        merged["_root_paths"] = paths
        users_cfg[name] = merged
        with status_lock:
            if name not in status:
                nc0 = share_sync.nc_config_for(merged)
                if not nc0["enabled"]:
                    share_state, share_detail = "disabled", "share_sync off or nextcloud_url not set"
                elif not nc0["password"]:
                    share_state, share_detail = "pending", f"no app password set (NC_APP_PASSWORD_{name.upper()})"
                else:
                    share_state, share_detail = "pending", "runs before next album sync"
                status[name] = {
                    "username": name,
                    "state": "idle",  # idle | running | success | failed
                    "last_run": None,
                    "last_result": "never run",
                    "albums_found": None,
                    "folders": paths,
                    "log_tail": "",
                    "run_count": 0,
                    "share_sync": {
                        "enabled": nc0["enabled"],
                        "state": share_state,
                        "detail": share_detail,
                    },
                }
            else:
                status[name]["folders"] = paths


def persist():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with status_lock:
            snap = copy.deepcopy(status)
        STATUS_FILE.write_text(json.dumps(snap, indent=2, default=str))
    except Exception as e:
        print(f"[dashboard] persist failed: {e}", flush=True)


def load_persisted():
    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text())
            with status_lock:
                status.update(data)
        except Exception as e:
            print(f"[dashboard] could not load status.json: {e}", flush=True)


def truthy(v) -> bool:
    return str(v).lower() in ("1", "true", "yes", "y")


def build_cmd(u: dict) -> list:
    """Build the upstream script CLI invocation for one user.

    Supported `mode` values per user:
      CREATE      – create missing albums, append new assets (upstream default,
                    never deletes anything by itself)
      UPDATE      – pseudo-mode implemented here: CREATE + `--sync-mode 2`, i.e.
                    create missing albums AND prune stale ones:
                      1. Immich must first rescan the external library so files
                         deleted/renamed in Nextcloud are marked as offline assets
                      2. offline asset records are deleted (files on disk untouched)
                      3. albums left empty are deleted (incl. renamed folders'
                         old albums, which converge empty over a run or two)
      CLEANUP     – upstream teardown mode: deletes albums matching the CURRENT
                    folder structure (NOT stale pruning!). Requires
                    delete_confirm: true or it only dry-runs.
      DELETE_ALL  – deletes every album. Requires delete_confirm: true.
    """
    if not u["_root_paths"]:
        raise ValueError(f"user {u.get('username')}: no root_paths configured")
    if not u["_api_key"]:
        raise ValueError(f"user {u.get('username')}: no API key (set api_key or api_key_file)")
    mode = str(u.get("mode", "CREATE")).upper()
    if mode not in ("CREATE", "UPDATE", "CLEANUP", "DELETE_ALL"):
        raise ValueError(f"user {u.get('username')}: unknown mode {mode!r}")
    effective_sync_mode = int(u.get("sync_mode", 0))
    if mode == "UPDATE":
        # UPDATE = CREATE that also prunes stale/renamed albums
        mode = "CREATE"
        effective_sync_mode = 2
    cmd = [
        "python3", str(SCRIPT),
        u["_root_paths"][0],          # positional root_path
        u["_api_url"],
        u["_api_key"],
        "--unattended",
        "--mode", mode,
        "--sync-mode", str(effective_sync_mode),
    ]
    if mode in ("CLEANUP", "DELETE_ALL") and truthy(u.get("delete_confirm", False)):
        cmd.append("--delete-confirm")
    for extra in u["_root_paths"][1:]:
        cmd += ["--root-path", extra]
    if u.get("_read_album_props"):
        cmd.append("--read-album-properties")
    if u.get("_update_props_mode") is not None:
        cmd += ["--update-album-props-mode", str(u["_update_props_mode"])]
    def add(flag, val):
        if val is not None and val != "":
            cmd.append(flag)
            cmd.append(str(val))
    add("--album-levels", u.get("album_levels", 1))
    if u.get("album_separator"):
        add("--album-separator", u["album_separator"])
    add("--chunk-size", u.get("chunk_size", 2000))
    add("--fetch-chunk-size", u.get("fetch_chunk_size", 1000))
    add("--log-level", u.get("log_level", "INFO"))
    if u.get("ignore"):
        for pat in str(u["ignore"]).split(":"):
            add("--ignore", pat)
    if u.get("path_filter"):
        for pat in str(u["path_filter"]).split(":"):
            add("--path-filter", pat)
    if str(u.get("insecure", False)).lower() in ("1", "true", "yes"):
        cmd.append("--insecure")
    if str(u.get("find_assets_in_albums", False)).lower() in ("1", "true", "yes"):
        cmd.append("--find-assets-in-albums")
    return cmd


ALBUM_RE = re.compile(r"(\d+)\s+album", re.IGNORECASE)


def run_sync(username: str, manual: bool = False) -> dict:
    u = users_cfg.get(username)
    if not u:
        return {"ok": False, "error": f"unknown user {username}"}
    with status_lock:
        if status[username]["state"] == "running":
            return {"ok": False, "error": f"sync already running for {username}"}
        status[username]["state"] = "running"
        status[username]["last_run"] = dt.datetime.now(dt.timezone.utc).isoformat()
    print(f"[dashboard] starting sync for {username} (manual={manual})", flush=True)
    # Step 1: mirror Nextcloud folder shares into .albumprops sidecars.
    # Never aborts the album sync: on failure we continue with last-known
    # files and record the problem in the status.
    share_result = share_sync.run_for_user(u, DATA_DIR)
    with status_lock:
        status[username]["share_sync"] = {k: share_result.get(k) for k in
                                          ("enabled", "state", "detail")}
    nc = share_sync.nc_config_for(u)
    if nc["enabled"] and nc["password"]:
        # Shares are (or will be) expressed via .albumprops: let upstream
        # discover them. Full enforcement (add/fix/remove) only once share
        # sync has succeeded at least once -- otherwise an all-empty state
        # caused by bad credentials could strip manual Immich shares.
        u["_read_album_props"] = True
        if share_sync.had_success(DATA_DIR, username):
            u["_update_props_mode"] = 2
    try:
        cmd = build_cmd(u)
    except ValueError as e:
        with status_lock:
            status[username].update(state="failed", last_result=str(e))
        persist()
        return {"ok": False, "error": str(e)}
    # mask API key in logs
    safe = [c if c != u["_api_key"] else "***" for c in cmd]
    print(f"[dashboard] exec: {' '.join(safe)}", flush=True)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=int(u.get("timeout_seconds", 1800)))
        out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        tail = "\n".join(out.splitlines()[-120:])
        m = ALBUM_RE.search(out)
        albums = m.group(1) if m else None
        ok = proc.returncode == 0
        with status_lock:
            status[username].update(
                state="success" if ok else "failed",
                last_result=f"exit {proc.returncode}" + (f" — {albums} albums mentioned" if albums else ""),
                albums_found=albums,
                log_tail=tail,
                run_count=status[username].get("run_count", 0) + 1,
                last_run=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
        result = {"ok": ok, "exit": proc.returncode, "albums": albums}
    except subprocess.TimeoutExpired:
        with status_lock:
            status[username].update(state="failed", last_result="timeout")
        result = {"ok": False, "error": "timeout"}
    except Exception as e:
        with status_lock:
            status[username].update(state="failed", last_result=str(e))
        result = {"ok": False, "error": str(e)}
    persist()
    print(f"[dashboard] finished sync for {username}: {result}", flush=True)
    return result


def schedule_all():
    # One schedule for everybody (from the global settings block).
    cron_expr = global_settings.get("cron")
    interval = int(global_settings.get("sync_interval_minutes", 60))
    label = f"cron `{cron_expr}`" if cron_expr else f"every {interval} min"
    for name in users_cfg:
        jobs_meta[name] = {
            "interval": label,
            "cron": cron_expr,
            "interval_minutes": interval,
        }
        job_id = f"sync-{name}"
        try:
            sched.remove_job(job_id)
        except Exception:
            pass
        trigger = CronTrigger.from_crontab(cron_expr) if cron_expr else IntervalTrigger(minutes=interval)
        sched.add_job(run_sync, trigger, args=[name], id=job_id,
                      max_instances=1, coalesce=True, misfire_grace_time=300)
    # next-run info helper stored at request time


def status_view():
    with status_lock:
        snap = copy.deepcopy(status)
    out = []
    for name, s in snap.items():
        meta = jobs_meta.get(name, {})
        try:
            job = sched.get_job(f"sync-{name}")
            nxt = job.next_run_time.isoformat() if job and job.next_run_time else None
        except Exception:
            nxt = None
        out.append({**s, "schedule": meta.get("interval", "?"),
                    "mode": str(users_cfg.get(name, {}).get("mode", "CREATE")).upper(),
                    "nextcloud_user": str(users_cfg.get(name, {}).get("nextcloud_user")
                                          or users_cfg.get(name, {}).get("username", "")),
                    "share_sync": s.get("share_sync", {}),
                    "cron": meta.get("cron"), "next_run": nxt,
                    "api_url": users_cfg.get(name, {}).get("_api_url", "")})
    return sorted(out, key=lambda r: r["username"])


@app.get("/")
def index():
    return render_template("index.html", users=status_view(),
                           api_url=immich_api_url, now=dt.datetime.now(dt.timezone.utc).isoformat())


@app.get("/api/status")
def api_status():
    return jsonify(status_view())


@app.post("/api/sync/<username>")
def api_sync(username):
    threading.Thread(target=run_sync, args=[username, True], daemon=True).start()
    return jsonify({"ok": True, "queued": username})


@app.post("/api/sync-all")
def api_sync_all():
    for name in list(users_cfg):
        threading.Thread(target=run_sync, args=[name, True], daemon=True).start()
    return jsonify({"ok": True, "queued": list(users_cfg)})


@app.get("/api/log/<username>")
def api_log(username):
    with status_lock:
        s = status.get(username, {})
    return jsonify({"username": username, "log": s.get("log_tail", "")})


if __name__ == "__main__":
    load_config()
    load_persisted()
    schedule_all()
    if not users_cfg:
        print(f"[dashboard] WARNING: no users in {CONFIG_PATH}", flush=True)
    else:
        print(f"[dashboard] loaded users: {list(users_cfg)} -> {immich_api_url}", flush=True)
    sched.start()
    # optional immediate first run
    if str(os.environ.get("RUN_IMMEDIATELY", "false")).lower() in ("1", "true", "yes"):
        for n in list(users_cfg):
            sched.add_job(run_sync, args=[n], id=f"boot-{n}", misfire_grace_time=60)
    app.run(host="0.0.0.0", port=PORT)
