from __future__ import annotations

from tests.lib.framework import assert_eq, assert_in, assert_not_in, req
from tests.lib.helpers import (cleanup_by_prefix, delete_instance,
                                delete_vpc, make_instance, make_vpc)


class TestInstances:
    def setUp(self):
        cleanup_by_prefix("instances", "t-inst-")
        cleanup_by_prefix("vpcs", "t-inst-vpc")
        self.vpc = make_vpc("t-inst-vpc")

    def _body(self, name, **kwargs):
        return {
            "name": name,
            "image_id": "ubuntu-22.04",
            "flavor": "standard.nano",
            "vpc_id": self.vpc["id"],
            "subnet_id": "subnet-test",
            **kwargs,
        }

    # ── Create ────────────────────────────────────────────────────────────────
    def test_create_returns_202(self):
        i = make_instance("t-inst-create", self.vpc["id"])
        assert_eq(i["status"], "pending", "initial status")
        delete_instance(i["id"])

    def test_create_name_stored(self):
        i = make_instance("t-inst-name", self.vpc["id"])
        assert_eq(i["name"], "t-inst-name", "name stored")
        delete_instance(i["id"])

    def test_create_image_id_stored(self):
        i = make_instance("t-inst-image", self.vpc["id"])
        assert_eq(i["image_id"], "ubuntu-22.04", "image_id stored")
        delete_instance(i["id"])

    def test_create_vpc_id_stored(self):
        i = make_instance("t-inst-vpc", self.vpc["id"])
        assert_eq(i["vpc_id"], self.vpc["id"], "vpc_id stored")
        delete_instance(i["id"])

    def test_create_subnet_id_stored(self):
        _, i = req("POST", "/v1/instances", self._body("t-inst-subnet"), expected=202)
        assert_eq(i["subnet_id"], "subnet-test", "subnet_id stored")
        delete_instance(i["id"])

    def test_create_id_present(self):
        i = make_instance("t-inst-id", self.vpc["id"])
        assert_in("id", i, "id field")
        if not i["id"]:
            raise AssertionError("id is empty")
        delete_instance(i["id"])

    def test_create_created_at_present(self):
        i = make_instance("t-inst-ts", self.vpc["id"])
        assert_in("created_at", i, "created_at present")
        delete_instance(i["id"])

    def test_create_tags_stored(self):
        _, i = req("POST", "/v1/instances",
                   self._body("t-inst-tags", tags={"env": "ci", "team": "ops"}),
                   expected=202)
        assert_eq(i["tags"]["env"],  "ci",  "env tag")
        assert_eq(i["tags"]["team"], "ops", "team tag")
        delete_instance(i["id"])

    def test_create_user_data_accepted(self):
        ud = "#cloud-config\npackages:\n  - curl\n"
        _, i = req("POST", "/v1/instances",
                   self._body("t-inst-ud", user_data=ud), expected=202)
        delete_instance(i["id"])

    def test_create_all_flavors_accepted(self):
        for flavor in ("standard.nano", "standard.small",
                       "standard.medium", "standard.large"):
            _, i = req("POST", "/v1/instances",
                       self._body(f"t-inst-{flavor.split('.')[1]}", flavor=flavor),
                       expected=202)
            assert_eq(i["flavor"], flavor, f"flavor {flavor} stored")
            delete_instance(i["id"])

    def test_create_ssh_user_ubuntu(self):
        i = make_instance("t-inst-sshu", self.vpc["id"], image_id="ubuntu-22.04")
        assert_eq(i["ssh_user"], "ubuntu", "ssh_user for ubuntu")
        delete_instance(i["id"])

    def test_create_missing_name_returns_400(self):
        req("POST", "/v1/instances",
            {k: v for k, v in self._body("x").items() if k != "name"}, expected=400)

    def test_create_missing_image_returns_400(self):
        req("POST", "/v1/instances",
            {k: v for k, v in self._body("x").items() if k != "image_id"}, expected=400)

    def test_create_missing_vpc_returns_400(self):
        req("POST", "/v1/instances",
            {k: v for k, v in self._body("x").items() if k != "vpc_id"}, expected=400)

    def test_create_missing_subnet_returns_400(self):
        req("POST", "/v1/instances",
            {k: v for k, v in self._body("x").items() if k != "subnet_id"}, expected=400)

    def test_create_duplicate_name_rejected(self):
        i = make_instance("t-inst-dup", self.vpc["id"])
        req("POST", "/v1/instances", self._body("t-inst-dup"), expected=409)
        delete_instance(i["id"])

    # ── Read ──────────────────────────────────────────────────────────────────
    def test_list_includes_created(self):
        i = make_instance("t-inst-list", self.vpc["id"])
        _, data = req("GET", "/v1/instances")
        assert_in(i["id"], [x["id"] for x in data["items"]], "instance in list")
        delete_instance(i["id"])

    def test_list_excludes_deleted(self):
        import time
        i = make_instance("t-inst-listdel", self.vpc["id"])
        delete_instance(i["id"])
        # delete is async — poll until gone (max 15s)
        deadline = time.time() + 15
        while time.time() < deadline:
            _, data = req("GET", "/v1/instances")
            if i["id"] not in [x["id"] for x in data["items"]]:
                return
            time.sleep(1)
        raise AssertionError("instance still in list 15s after delete")

    def test_get_by_id(self):
        i = make_instance("t-inst-get", self.vpc["id"])
        _, got = req("GET", f"/v1/instances/{i['id']}")
        assert_eq(got["id"], i["id"], "id matches")
        delete_instance(i["id"])

    def test_get_missing_returns_404(self):
        req("GET", "/v1/instances/does-not-exist", expected=404)

    def test_list_users_endpoint(self):
        i = make_instance("t-inst-lusers", self.vpc["id"])
        req("POST", f"/v1/instances/{i['id']}/users",
            {"username": "alice", "sudo": False}, expected=201)
        _, data = req("GET", f"/v1/instances/{i['id']}/users")
        assert_in("items", data, "items key")
        assert_eq(len(data["items"]), 1, "one user")
        delete_instance(i["id"])

    # ── Update ────────────────────────────────────────────────────────────────
    def test_update_tags(self):
        i = make_instance("t-inst-updtags", self.vpc["id"])
        _, u = req("PUT", f"/v1/instances/{i['id']}", {"tags": {"env": "ci"}})
        assert_eq(u["tags"]["env"], "ci", "tag updated")
        delete_instance(i["id"])

    def test_update_persists(self):
        i = make_instance("t-inst-persist", self.vpc["id"])
        req("PUT", f"/v1/instances/{i['id']}", {"tags": {"k": "v"}})
        _, got = req("GET", f"/v1/instances/{i['id']}")
        assert_eq(got["tags"]["k"], "v", "update persisted")
        delete_instance(i["id"])

    # ── Delete ────────────────────────────────────────────────────────────────
    def test_delete_returns_204(self):
        i = make_instance("t-inst-del204", self.vpc["id"])
        status, _ = req("DELETE", f"/v1/instances/{i['id']}", expected=204)
        assert_eq(status, 204, "delete status")

    def test_delete_missing_returns_404(self):
        req("DELETE", "/v1/instances/does-not-exist", expected=404)

    # ── Users ─────────────────────────────────────────────────────────────────
    def test_add_user_username_stored(self):
        i = make_instance("t-inst-uname", self.vpc["id"])
        _, u = req("POST", f"/v1/instances/{i['id']}/users",
                   {"username": "alice", "sudo": False}, expected=201)
        assert_eq(u["username"], "alice", "username stored")
        delete_instance(i["id"])

    def test_add_user_sudo_false_stored(self):
        i = make_instance("t-inst-unosudo", self.vpc["id"])
        _, u = req("POST", f"/v1/instances/{i['id']}/users",
                   {"username": "alice", "sudo": False}, expected=201)
        assert_eq(u["sudo"], False, "sudo false stored")
        delete_instance(i["id"])

    def test_add_user_sudo_true_stored(self):
        i = make_instance("t-inst-usudo", self.vpc["id"])
        _, u = req("POST", f"/v1/instances/{i['id']}/users",
                   {"username": "admin", "sudo": True}, expected=201)
        assert_eq(u["sudo"], True, "sudo true stored")
        delete_instance(i["id"])

    def test_add_user_ssh_key_stored(self):
        i = make_instance("t-inst-ukey", self.vpc["id"])
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA test-key"
        _, u = req("POST", f"/v1/instances/{i['id']}/users",
                   {"username": "dave", "sudo": False, "ssh_keys": [key]},
                   expected=201)
        assert_eq(u["ssh_keys"][0], key, "ssh key stored")
        delete_instance(i["id"])

    def test_add_user_multiple_ssh_keys(self):
        i = make_instance("t-inst-umkeys", self.vpc["id"])
        keys = ["ssh-ed25519 AAAA key1", "ssh-ed25519 AAAA key2"]
        _, u = req("POST", f"/v1/instances/{i['id']}/users",
                   {"username": "multi", "sudo": False, "ssh_keys": keys},
                   expected=201)
        assert_eq(len(u["ssh_keys"]), 2, "two keys stored")
        delete_instance(i["id"])

    def test_add_multiple_users(self):
        i = make_instance("t-inst-multiuser", self.vpc["id"])
        for name in ("alice", "bob", "carol"):
            req("POST", f"/v1/instances/{i['id']}/users",
                {"username": name, "sudo": False}, expected=201)
        _, got = req("GET", f"/v1/instances/{i['id']}")
        assert_eq(len(got["users"]), 3, "three users stored")
        delete_instance(i["id"])

    def test_add_user_appears_in_instance(self):
        i = make_instance("t-inst-ucheck", self.vpc["id"])
        req("POST", f"/v1/instances/{i['id']}/users",
            {"username": "alice", "sudo": False}, expected=201)
        _, got = req("GET", f"/v1/instances/{i['id']}")
        assert_in("alice", [u["username"] for u in got["users"]], "user in instance")
        delete_instance(i["id"])

    def test_add_user_duplicate_rejected(self):
        i = make_instance("t-inst-udup", self.vpc["id"])
        req("POST", f"/v1/instances/{i['id']}/users",
            {"username": "bob", "sudo": False}, expected=201)
        req("POST", f"/v1/instances/{i['id']}/users",
            {"username": "bob", "sudo": False}, expected=409)
        delete_instance(i["id"])

    def test_add_user_missing_username_returns_400(self):
        i = make_instance("t-inst-unoname", self.vpc["id"])
        req("POST", f"/v1/instances/{i['id']}/users",
            {"sudo": False}, expected=400)
        delete_instance(i["id"])

    def test_remove_user(self):
        i = make_instance("t-inst-rmuser", self.vpc["id"])
        req("POST", f"/v1/instances/{i['id']}/users",
            {"username": "carol", "sudo": False}, expected=201)
        req("DELETE", f"/v1/instances/{i['id']}/users/carol", expected=204)
        _, got = req("GET", f"/v1/instances/{i['id']}")
        assert_not_in("carol", [u["username"] for u in got["users"]], "user removed")
        delete_instance(i["id"])

    def test_remove_one_of_multiple_users(self):
        i = make_instance("t-inst-rmmulti", self.vpc["id"])
        for name in ("alice", "bob"):
            req("POST", f"/v1/instances/{i['id']}/users",
                {"username": name, "sudo": False}, expected=201)
        req("DELETE", f"/v1/instances/{i['id']}/users/alice", expected=204)
        _, got = req("GET", f"/v1/instances/{i['id']}")
        usernames = [u["username"] for u in got["users"]]
        assert_not_in("alice", usernames, "alice removed")
        assert_in("bob", usernames, "bob remains")
        delete_instance(i["id"])

    def test_remove_missing_user_returns_404(self):
        i = make_instance("t-inst-rmmissing", self.vpc["id"])
        req("DELETE", f"/v1/instances/{i['id']}/users/nobody", expected=404)
        delete_instance(i["id"])
