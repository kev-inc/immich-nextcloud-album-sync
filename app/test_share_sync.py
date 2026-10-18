"""Self-contained tests for share_sync.py (no network, no Nextcloud needed).

Run:  python3 test_share_sync.py
"""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import share_sync
import yaml


def make_resp(payload):
    class R:
        def raise_for_status(self):
            pass
        def json(self):
            return payload
    return R()


SHARES = [
    # shared folder -> editor (perm 27 = all)
    {"share_type": 0, "item_type": "folder", "path": "/alice_photos/Vacation",
     "share_with": "bob", "permissions": 27},
    # same folder shared read-only with carol -> viewer
    {"share_type": 0, "item_type": "folder", "path": "/alice_photos/Vacation",
     "share_with": "carol", "permissions": 1},
    # group share -> skipped
    {"share_type": 1, "item_type": "folder", "path": "/alice_photos/Party",
     "share_with": "family", "permissions": 27},
    # public link -> skipped
    {"share_type": 3, "item_type": "folder", "path": "/alice_photos/Links",
     "share_with": "", "permissions": 1},
    # single file -> skipped
    {"share_type": 0, "item_type": "file", "path": "/alice_photos/note.txt",
     "share_with": "bob", "permissions": 1},
    # outside synced roots -> skipped
    {"share_type": 0, "item_type": "folder", "path": "/other_stuff/X",
     "share_with": "bob", "permissions": 1},
]

PASS = 0
def check(name, cond, extra=""):
    global PASS
    assert cond, f"FAIL: {name} {extra}"
    PASS += 1
    print(f"  ok: {name}")


def main():
    tmp = Path(tempfile.mkdtemp())
    lib = tmp / "alice_photos"
    (lib / "Vacation").mkdir(parents=True)
    (lib / "Private").mkdir(parents=True)
    data = tmp / "data"
    data.mkdir()

    cfg = {"username": "alice", "_root_paths": [str(lib)],
           "nextcloud_url": "https://cloud.example.com",
           "nextcloud_app_password": "secret", "share_sync": True}

    with patch.object(share_sync.requests, "get", return_value=make_resp({"ocs": {"data": SHARES}})):
        res = share_sync.run_for_user(cfg, data)
    check("share run success", res["state"] == "success", res)
    check("applied 1 folder", res["applied"] == 1, res)
    check("skipped group/link", res["skipped_group_link"] == 2, res)
    check("skipped file", res["skipped_files"] == 1, res)
    check("skipped outside", res["skipped_outside_roots"] == 1, res)

    props = yaml.safe_load((lib / "Vacation" / ".albumprops").read_text())
    by_user = {e["user"]: e["role"] for e in props["share_with"]}
    check("bob editor", by_user.get("bob") == "editor", by_user)
    check("carol viewer", by_user.get("carol") == "viewer", by_user)
    check("inherit set", props.get("inherit") is True, props)

    # viewer-wins on conflicting duplicate shares
    dup = [dict(SHARES[0], permissions=1), dict(SHARES[0], permissions=27)]
    with patch.object(share_sync.requests, "get", return_value=make_resp({"ocs": {"data": dup}})):
        share_sync.run_for_user(cfg, data)
    props = yaml.safe_load((lib / "Vacation" / ".albumprops").read_text())
    check("viewer wins conflict",
          {e["user"]: e["role"] for e in props["share_with"]} == {"bob": "viewer"})

    # unshare everything -> managed file removed, state clean
    with patch.object(share_sync.requests, "get", return_value=make_resp({"ocs": {"data": []}})):
        res2 = share_sync.run_for_user(cfg, data)
    check("unshare removed file", not (lib / "Vacation" / ".albumprops").exists())
    check("removed counted", res2["removed"] == 1, res2)
    state = json.loads((data / "alice.albumprops.managed.json").read_text())
    check("state clean", state.get("managed") == {}, state)
    check("success recorded", bool(state.get("last_success")) or share_sync.had_success(data, "alice"))

    # manual pre-existing .albumprops with share_with is never touched
    (lib / "Private" / ".albumprops").write_text(
        yaml.safe_dump({"share_with": [{"user": "dave", "role": "viewer"}], "description": "mine"}))
    priv = [{"share_type": 0, "item_type": "folder", "path": "/alice_photos/Private",
             "share_with": "bob", "permissions": 27}]
    with patch.object(share_sync.requests, "get", return_value=make_resp({"ocs": {"data": priv}})):
        res3 = share_sync.run_for_user(cfg, data)
    check("manual skipped", res3["skipped_manual"] == 1, res3)
    kept = yaml.safe_load((lib / "Private" / ".albumprops").read_text())
    check("manual untouched", kept["share_with"] == [{"user": "dave", "role": "viewer"}]
          and kept["description"] == "mine", kept)

    # no password -> clean failure, no crash
    res4 = share_sync.run_for_user({**cfg, "nextcloud_app_password": ""}, data)
    check("missing password fails clean", res4["state"] == "failed" and res4["enabled"], res4)

    # share_sync disabled
    res5 = share_sync.run_for_user({**cfg, "share_sync": False}, data)
    check("disabled short-circuits", res5["state"] == "disabled", res5)

    print(f"\nALL {PASS} CHECKS PASSED")


if __name__ == "__main__":
    main()
