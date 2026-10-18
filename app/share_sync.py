"""Nextcloud share -> Immich album share bridge.
For one Immich/external-library user:
  1. Fetch outgoing folder shares from the Nextcloud OCS Share API
     (authenticated as the user via an app password).
  2. Translate each share into an upstream `.albumprops` sidecar file
     (`share_with` + `inherit: true`) inside the corresponding local folder.
  3. The album sync then runs with `--read-album-properties` and
     `--update-album-props-mode 2`, so upstream enforces the share state
     on the Immich albums (add / fix roles / remove unshared users).

Only files created by this module (tracked in a JSON state file) or absent
paths are ever written. Pre-existing manual `.albumprops` files that already
contain `share_with` are left untouched and reported as skipped.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

try:
    import requests
except ImportError:  # pragma: no cover - image always has requests
    requests = None

ALBUMPROPS_NAME = ".albumprops"
STATE_NAME = "albumprops.managed.json"

# Nextcloud OCS share_type values we deliberately handle / skip.
SHARE_TYPE_USER = 0

# Nextcloud permission bits: 1=read, 2=update, 4=create, 8=delete, 16=share.
def role_for_permissions(permissions: int) -> str:
    try:
        permissions = int(permissions)
    except (TypeError, ValueError):
        permissions = 1
    return "editor" if (permissions & 0b1110) else "viewer"


def resolve_password(user_cfg: dict) -> str:
    """App password from inline config, secret file, or env."""
    if user_cfg.get("nextcloud_app_password"):
        return str(user_cfg["nextcloud_app_password"])
    key_file = user_cfg.get("nextcloud_app_password_file")
    if key_file and Path(str(key_file)).exists():
        return Path(str(key_file)).read_text().strip()
    env_val = os.environ.get(f"NC_APP_PASSWORD_{str(user_cfg.get('username', '')).upper()}", "")
    return env_val.strip()


def nc_config_for(user_cfg: dict, global_url: str = "") -> dict:
    url = str(user_cfg.get("nextcloud_url") or global_url or os.environ.get("NEXTCLOUD_URL", "")).rstrip("/")
    nc_user = str(user_cfg.get("nextcloud_user") or user_cfg.get("username") or "")
    return {
        "enabled": bool(str(user_cfg.get("share_sync", True)).lower() in ("1", "true", "yes", "y")) and bool(url),
        "url": url,
        "username": nc_user,
        "password": resolve_password(user_cfg),
    }


def build_path_maps(user_cfg: dict) -> list:
    """Return [(nc_prefix, local_prefix)] used to translate OCS paths to disk.

    Explicit `path_maps: [{nextcloud: ..., local: ...}]` win; otherwise one
    automatic map per root_path using its basename
    (e.g. local /external_libs/alice_photos <-> Nextcloud /alice_photos),
    plus an explicit `nextcloud_base_path` for the first root if given.
    """
    explicit = user_cfg.get("path_maps") or []
    maps = []
    for entry in explicit:
        nc = "/" + str(entry.get("nextcloud", "/")).strip("/")
        local = str(entry.get("local", "")).rstrip("/")
        if local:
            maps.append((nc if nc != "/" else "/", local))
    if maps:
        return sorted(maps, key=lambda m: -len(m[0]))
    roots = list(user_cfg.get("_root_paths") or [])
    base = str(user_cfg.get("nextcloud_base_path") or "").strip()
    for i, root in enumerate(roots):
        root = str(root).rstrip("/")
        if i == 0 and base:
            nc = "/" + base.strip("/")
        else:
            nc = "/" + Path(root).name
        maps.append((nc, root))
    return sorted(maps, key=lambda m: -len(m[0]))


def fetch_shares(base_url: str, username: str, password: str, timeout: int = 15) -> list:
    """List the user's outgoing shares via the OCS Share API."""
    if requests is None:
        raise RuntimeError("the 'requests' package is not installed")
    url = f"{base_url}/ocs/v2.php/apps/files_sharing/api/v1/shares?format=json"
    resp = requests.get(
        url,
        auth=(username, password),
        headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    data = (payload.get("ocs") or {}).get("data", [])
    if isinstance(data, dict):  # single share comes back as an object
        data = list(data.values())
    return data if isinstance(data, list) else []


def map_to_local(nc_path: str, maps: list) -> str | None:
    """Translate a Nextcloud path (/alice_photos/Vacation) to a local dir."""
    nc_path = "/" + str(nc_path or "").strip("/")
    for nc_prefix, local_prefix in maps:
        if nc_prefix == "/":
            rel = nc_path[1:]
        elif nc_path == nc_prefix or nc_path.startswith(nc_prefix + "/"):
            rel = nc_path[len(nc_prefix):].lstrip("/")
        else:
            continue
        return local_prefix if not rel else f"{local_prefix}/{rel}"
    return None


def _load_state(state_path: Path) -> dict:
    try:
        return json.loads(state_path.read_text())
    except (OSError, ValueError):
        return {"managed": {}}


def _save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2))


def _read_props(path: Path) -> dict | None:
    try:
        data = yaml.safe_load(path.read_text()) or {}
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _state_path(data_dir: Path, username: str) -> Path:
    return Path(data_dir) / f"{username}.{STATE_NAME}"


def mark_success(data_dir: Path, username: str) -> None:
    """Record that share sync has produced a valid state at least once."""
    path = _state_path(data_dir, username)
    state = _load_state(path)
    state["last_success"] = datetime.now(timezone.utc).isoformat()
    _save_state(path, state)


def had_success(data_dir: Path, username: str) -> bool:
    return bool(_load_state(_state_path(data_dir, username)).get("last_success"))


def sync_albumprops(user_cfg: dict, shares: list, data_dir: Path) -> dict:
    """Write/remove managed `.albumprops` files. Returns stats dict."""
    username = user_cfg.get("username", "?")
    maps = build_path_maps(user_cfg)
    state_path = _state_path(data_dir, username)
    state = _load_state(state_path)
    managed = state.setdefault("managed", {})

    # desired: local_dir -> {user: role}
    desired: dict[str, dict[str, str]] = {}
    stats = {"shares_seen": len(shares), "user_folder_shares": 0,
             "skipped_group_link": 0, "skipped_files": 0,
             "skipped_outside_roots": 0, "skipped_manual": 0,
             "applied": 0, "removed": 0}
    for share in shares:
        if not isinstance(share, dict):
            continue
        if share.get("share_type") != SHARE_TYPE_USER:
            stats["skipped_group_link"] += 1
            continue
        if share.get("item_type") != "folder":
            stats["skipped_files"] += 1
            continue
        local = map_to_local(str(share.get("path", "")), maps)
        if not local:
            stats["skipped_outside_roots"] += 1
            continue
        target = str(share.get("share_with") or "").strip()
        if not target:
            continue
        stats["user_folder_shares"] += 1
        # Most-restrictive role wins across repeated shares (viewer beats
        # editor), mirroring upstream's inheritance merge.
        role = role_for_permissions(share.get("permissions", 1))
        prev = desired.setdefault(local, {}).get(target)
        desired[local][target] = "viewer" if "viewer" in (prev, role) else role

    # Write / update desired files.
    for local, users in sorted(desired.items()):
        dest = Path(local) / ALBUMPROPS_NAME
        existing = _read_props(dest) if dest.exists() else None
        info = managed.get(local)
        if existing is not None and info is None and "share_with" in existing:
            stats["skipped_manual"] += 1
            print(f"[share-sync:{username}] manual .albumprops at {dest}, skipping (not managed)", flush=True)
            continue
        created_by_us = info is None and existing is None
        added_inherit = bool(info and info.get("added_inherit", False))
        props = dict(existing) if isinstance(existing, dict) else {}
        props["share_with"] = [{"user": u, "role": r} for u, r in sorted(users.items())]
        if "inherit" not in props:
            props["inherit"] = True
            added_inherit = True
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(yaml.safe_dump(props, sort_keys=False, allow_unicode=True))
            managed[local] = {"created_by_us": created_by_us or bool(info and info.get("created_by_us")),
                              "added_inherit": added_inherit}
            stats["applied"] += 1
        except OSError as e:
            print(f"[share-sync:{username}] cannot write {dest}: {e}", flush=True)

    # Remove managed files for folders that are no longer shared.
    for local in sorted(set(managed) - set(desired)):
        dest = Path(local) / ALBUMPROPS_NAME
        info = managed.get(local) or {}
        if info.get("created_by_us"):
            try:
                if dest.exists():
                    dest.unlink()
                stats["removed"] += 1
            except OSError as e:
                print(f"[share-sync:{username}] cannot remove {dest}: {e}", flush=True)
        else:
            existing = _read_props(dest)
            if isinstance(existing, dict) and "share_with" in existing:
                del existing["share_with"]
                if info.get("added_inherit") and set(existing) <= {"inherit"}:
                    existing.pop("inherit", None)
                try:
                    if existing:
                        dest.write_text(yaml.safe_dump(existing, sort_keys=False, allow_unicode=True))
                    elif dest.exists():
                        dest.unlink()
                    stats["removed"] += 1
                except OSError as e:
                    print(f"[share-sync:{username}] cannot update {dest}: {e}", flush=True)
        managed.pop(local, None)

    _save_state(state_path, state)
    return stats


def run_for_user(user_cfg: dict, data_dir: Path, global_url: str = "") -> dict:
    """Full share-sync pass for one user. Never raises."""
    username = user_cfg.get("username", "?")
    cfg = nc_config_for(user_cfg, global_url)
    if not cfg["enabled"]:
        return {"enabled": False, "state": "disabled",
                "detail": "share_sync off or nextcloud_url not set"}
    if not cfg["password"]:
        return {"enabled": True, "state": "failed",
                "detail": "no Nextcloud app password (nextcloud_app_password[_file])"}
    try:
        shares = fetch_shares(cfg["url"], cfg["username"], cfg["password"])
    except Exception as e:
        return {"enabled": True, "state": "failed",
                "detail": f"OCS share fetch failed: {e}"}
    try:
        stats = sync_albumprops(user_cfg, shares, data_dir)
    except Exception as e:
        return {"enabled": True, "state": "failed",
                "detail": f".albumprops update failed: {e}"}
    mark_success(data_dir, str(user_cfg.get("username", "?")))
    detail = (f"{stats['user_folder_shares']} folder share(s) -> "
              f"{stats['applied']} .albumprops written, {stats['removed']} removed")
    skipped = (stats["skipped_group_link"] + stats["skipped_files"]
               + stats["skipped_outside_roots"] + stats["skipped_manual"])
    if skipped:
        detail += (f" ({skipped} skipped: {stats['skipped_group_link']} group/link, "
                   f"{stats['skipped_files']} file-level, "
                   f"{stats['skipped_outside_roots']} outside synced roots, "
                   f"{stats['skipped_manual']} manual .albumprops)")
    return {"enabled": True, "state": "success", "detail": detail, **stats}
