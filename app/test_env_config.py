"""Tests for server.py env-based config (SYNC_* vars, no YAML file).

Run:  python3 test_env_config.py
"""
import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def load_srv():
    spec = importlib.util.spec_from_file_location("srv", str(Path(__file__).parent / "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


PASS = 0
def check(name, cond, extra=""):
    global PASS
    assert cond, f"FAIL: {name} {extra}"
    PASS += 1
    print(f"  ok: {name}")


def fresh_env(**kw):
    for k in list(os.environ):
        if k.startswith(("SYNC_", "API_KEY_", "NC_APP_PASSWORD_", "IMMICH_API_URL", "NEXTCLOUD_URL", "USERS_YML")):
            del os.environ[k]
    os.environ.update(kw)


def main():
    # 1. no SYNC_USERS -> file fallback path (missing file raises helpfully)
    srv = load_srv()
    fresh_env()
    srv.users_cfg.clear(); srv.status.clear(); srv.global_settings = {}
    try:
        srv.CONFIG_PATH = Path("/nonexistent/users.yml")
        srv.load_config()
        check("missing everything raises", False)
    except FileNotFoundError as e:
        check("missing everything raises", "SYNC_USERS" in str(e), e)

    # 2. env mode: two users, multi-root, mapping, custom settings
    srv = load_srv()
    fresh_env(
        SYNC_USERS="alice, bob",
        SYNC_ROOT_PATHS_ALICE="/external_libs/alice_photos",
        SYNC_ROOT_PATHS_BOB="/external_libs/bob1:/external_libs/bob2",
        SYNC_NC_USER_BOB="bob.nc",
        SYNC_ALBUM_MODE="UPDATE",
        SYNC_SCHEDULE_MINUTES="30",
        SYNC_ALBUM_LEVELS="2",
        SYNC_SHARE_SYNC="true",
        NEXTCLOUD_URL="https://cloud.example.com",
        IMMICH_API_URL="http://immich:2283/api",
        API_KEY_ALICE="k-alice",
        API_KEY_BOB="k-bob",
        NC_APP_PASSWORD_BOB="p-bob",
    )
    srv.users_cfg.clear(); srv.status.clear(); srv.global_settings = {}
    srv.load_config()
    check("two users", sorted(srv.users_cfg) == ["alice", "bob"], sorted(srv.users_cfg))
    a, b = srv.users_cfg["alice"], srv.users_cfg["bob"]
    check("alice root", a["_root_paths"] == ["/external_libs/alice_photos"], a["_root_paths"])
    check("bob multi-root", b["_root_paths"] == ["/external_libs/bob1", "/external_libs/bob2"], b["_root_paths"])
    check("alice key", a["_api_key"] == "k-alice", a["_api_key"])
    check("bob key", b["_api_key"] == "k-bob", b["_api_key"])
    check("bob nc mapping", srv.share_sync.nc_config_for(b)["username"] == "bob.nc")
    check("alice nc default", srv.share_sync.nc_config_for(a)["username"] == "alice")
    check("global schedule", srv.global_settings["sync_interval_minutes"] == "30")
    check("global levels", srv.global_settings["album_levels"] == "2")
    check("no cron", srv.global_settings["cron"] is None)

    # schedule + command plumbing from env values
    srv.jobs_meta.clear()
    srv.schedule_all()
    check("schedule label", srv.jobs_meta["alice"]["interval"] == "every 30 min")
    cmd = " ".join(srv.build_cmd(a))
    check("cmd levels", "--album-levels 2" in cmd, cmd)
    check("cmd update+sync2", "--mode CREATE --sync-mode 2" in cmd, cmd)
    check("cmd roots", "/external_libs/alice_photos" in cmd and "--root-path" not in cmd, cmd)
    cmdb = " ".join(srv.build_cmd(b))
    check("cmd multi-root", "--root-path /external_libs/bob2" in cmdb, cmdb)

    # 3. missing key is rejected at command build time
    srv = load_srv()
    fresh_env(SYNC_USERS="nobody", SYNC_ROOT_PATHS_NOBODY="/x")
    srv.users_cfg.clear(); srv.status.clear(); srv.global_settings = {}
    srv.load_config()
    try:
        srv.build_cmd(srv.users_cfg["nobody"])
        check("missing key rejected", False)
    except ValueError as e:
        check("missing key rejected", "no API key" in str(e), e)

    # 4. env mode with cron + passwords present
    srv = load_srv()
    fresh_env(SYNC_USERS="carol", SYNC_ROOT_PATHS_CAROL="/x",
              SYNC_CRON="0 */2 * * *", NEXTCLOUD_URL="https://cloud.example.com",
              API_KEY_CAROL="k", NC_APP_PASSWORD_CAROL="p")
    srv.users_cfg.clear(); srv.status.clear(); srv.global_settings = {}; srv.jobs_meta.clear()
    srv.load_config()
    srv.schedule_all()
    check("cron label", srv.jobs_meta["carol"]["interval"] == "cron `0 */2 * * *`",
          srv.jobs_meta["carol"])
    st = srv.status["carol"]["share_sync"]
    check("carol share pending w/ password",
          st["enabled"] and st["state"] == "pending" and "runs before" in st["detail"], st)

    print(f"\nALL {PASS} CHECKS PASSED")


if __name__ == "__main__":
    main()
