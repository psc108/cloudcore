from __future__ import annotations

from tests.lib.framework import assert_eq, assert_in, assert_not_in, req
from tests.lib.helpers import (cleanup_by_prefix, cleanup_nfs_by_prefix,
                                delete_nfs_server, delete_vpc,
                                make_nfs_server, make_vpc)

PFX = "t-nfs-"


class TestNFS:
    def setUp(self):
        cleanup_nfs_by_prefix(PFX)
        cleanup_by_prefix("vpcs", PFX + "vpc")
        self.vpc = make_vpc(PFX + "vpc")

    # ── Create ────────────────────────────────────────────────────────────────
    def test_create_returns_202(self):
        status, body = req("POST", "/v1/nfs-servers", {
            "name": PFX + "create",
            "vpc_id": self.vpc["id"],
        }, expected=202)
        assert_eq(status, 202, "create status")
        delete_nfs_server(body["id"])

    def test_create_name_stored(self):
        nfs = make_nfs_server(PFX + "name", self.vpc["id"])
        assert_eq(nfs["name"], PFX + "name", "name stored")
        delete_nfs_server(nfs["id"])

    def test_create_vpc_id_stored(self):
        vpc = make_vpc(PFX + "vpc-vpcid")
        nfs = make_nfs_server(PFX + "vpcid", vpc["id"])
        assert_eq(nfs["vpc_id"], vpc["id"], "vpc_id stored")
        delete_nfs_server(nfs["id"])
        delete_vpc(vpc["id"])

    def test_create_default_flavor(self):
        nfs = make_nfs_server(PFX + "flavor", self.vpc["id"])
        assert_eq(nfs["flavor"], "standard.medium", "default flavor")
        delete_nfs_server(nfs["id"])

    def test_create_default_disk_gb(self):
        nfs = make_nfs_server(PFX + "disk", self.vpc["id"])
        assert_eq(nfs["disk_gb"], 20, "default disk_gb")
        delete_nfs_server(nfs["id"])

    def test_create_id_present(self):
        nfs = make_nfs_server(PFX + "id", self.vpc["id"])
        assert_in("id", nfs, "id field present")
        if not nfs["id"]:
            raise AssertionError("id is empty")
        delete_nfs_server(nfs["id"])

    def test_create_status_present(self):
        nfs = make_nfs_server(PFX + "status", self.vpc["id"])
        assert_in("status", nfs, "status field present")
        delete_nfs_server(nfs["id"])

    def test_create_with_shares(self):
        nfs = make_nfs_server(PFX + "shares", self.vpc["id"],
                              shares=[{"name": "data"}, {"name": "backups"}])
        assert_eq(len(nfs["shares"]), 2, "two shares created")
        share_names = [s["name"] for s in nfs["shares"]]
        assert_in("data",    share_names, "data share present")
        assert_in("backups", share_names, "backups share present")
        delete_nfs_server(nfs["id"])

    def test_create_share_path_set(self):
        # path is set when share is added via sub-resource POST
        nfs = make_nfs_server(PFX + "path", self.vpc["id"])
        _, share = req("POST", f"/v1/nfs-servers/{nfs['id']}/shares",
                       {"name": "exports"}, expected=201)
        assert_eq(share["path"], "/exports/exports", "share path set")
        delete_nfs_server(nfs["id"])

    def test_create_share_default_clients(self):
        # clients is set when share is added via sub-resource POST
        nfs = make_nfs_server(PFX + "clients", self.vpc["id"])
        _, share = req("POST", f"/v1/nfs-servers/{nfs['id']}/shares",
                       {"name": "data"}, expected=201)
        assert_eq(share["clients"], "vpc", "default clients=vpc")
        delete_nfs_server(nfs["id"])

    def test_create_missing_name_returns_400(self):
        req("POST", "/v1/nfs-servers", {"vpc_id": self.vpc["id"]}, expected=400)

    def test_create_missing_vpc_id_returns_400(self):
        req("POST", "/v1/nfs-servers", {"name": PFX + "novpc"}, expected=400)

    def test_create_invalid_vpc_returns_404(self):
        req("POST", "/v1/nfs-servers",
            {"name": PFX + "badvpc", "vpc_id": "does-not-exist"}, expected=404)

    def test_create_duplicate_name_rejected(self):
        nfs = make_nfs_server(PFX + "dup", self.vpc["id"])
        req("POST", "/v1/nfs-servers",
            {"name": PFX + "dup", "vpc_id": self.vpc["id"]}, expected=409)
        delete_nfs_server(nfs["id"])

    # ── Read ──────────────────────────────────────────────────────────────────
    def test_list_includes_created(self):
        nfs = make_nfs_server(PFX + "list", self.vpc["id"])
        _, data = req("GET", "/v1/nfs-servers")
        assert_in(nfs["id"], [x["id"] for x in data["items"]], "nfs in list")
        delete_nfs_server(nfs["id"])

    def test_list_excludes_deleted(self):
        nfs = make_nfs_server(PFX + "listdel", self.vpc["id"])
        delete_nfs_server(nfs["id"])
        _, data = req("GET", "/v1/nfs-servers")
        assert_not_in(nfs["id"], [x["id"] for x in data["items"]], "deleted not in list")

    def test_get_by_id(self):
        nfs = make_nfs_server(PFX + "get", self.vpc["id"])
        _, got = req("GET", f"/v1/nfs-servers/{nfs['id']}")
        assert_eq(got["id"], nfs["id"], "id matches")
        delete_nfs_server(nfs["id"])

    def test_get_missing_returns_404(self):
        req("GET", "/v1/nfs-servers/does-not-exist", expected=404)

    # ── Delete ────────────────────────────────────────────────────────────────
    def test_delete_returns_204(self):
        nfs = make_nfs_server(PFX + "del204", self.vpc["id"])
        status, _ = req("DELETE", f"/v1/nfs-servers/{nfs['id']}", expected=204)
        assert_eq(status, 204, "delete status")

    def test_delete_removes_from_list(self):
        nfs = make_nfs_server(PFX + "del", self.vpc["id"])
        delete_nfs_server(nfs["id"])
        _, data = req("GET", "/v1/nfs-servers")
        assert_not_in(nfs["id"], [x["id"] for x in data["items"]], "nfs removed")

    def test_delete_missing_returns_404(self):
        req("DELETE", "/v1/nfs-servers/does-not-exist", expected=404)

    # ── Shares sub-resource ───────────────────────────────────────────────────
    def test_add_share_returns_201(self):
        nfs = make_nfs_server(PFX + "addsh", self.vpc["id"])
        status, share = req("POST", f"/v1/nfs-servers/{nfs['id']}/shares",
                            {"name": "data"}, expected=201)
        assert_eq(status, 201, "add share status")
        assert_eq(share["name"], "data", "share name")
        delete_nfs_server(nfs["id"])

    def test_add_share_path_set(self):
        nfs = make_nfs_server(PFX + "shpath", self.vpc["id"])
        _, share = req("POST", f"/v1/nfs-servers/{nfs['id']}/shares",
                       {"name": "media"}, expected=201)
        assert_eq(share["path"], "/exports/media", "share path")
        delete_nfs_server(nfs["id"])

    def test_add_share_appears_in_server(self):
        nfs = make_nfs_server(PFX + "shget", self.vpc["id"])
        req("POST", f"/v1/nfs-servers/{nfs['id']}/shares",
            {"name": "logs"}, expected=201)
        _, got = req("GET", f"/v1/nfs-servers/{nfs['id']}")
        assert_in("logs", [s["name"] for s in got["shares"]], "share in server")
        delete_nfs_server(nfs["id"])

    def test_add_share_missing_name_returns_400(self):
        nfs = make_nfs_server(PFX + "shnoname", self.vpc["id"])
        req("POST", f"/v1/nfs-servers/{nfs['id']}/shares", {}, expected=400)
        delete_nfs_server(nfs["id"])

    def test_add_share_duplicate_rejected(self):
        nfs = make_nfs_server(PFX + "shdup", self.vpc["id"])
        req("POST", f"/v1/nfs-servers/{nfs['id']}/shares",
            {"name": "data"}, expected=201)
        req("POST", f"/v1/nfs-servers/{nfs['id']}/shares",
            {"name": "data"}, expected=409)
        delete_nfs_server(nfs["id"])

    def test_remove_share_returns_204(self):
        nfs = make_nfs_server(PFX + "shrm", self.vpc["id"],
                              shares=[{"name": "todel"}])
        status, _ = req("DELETE",
                        f"/v1/nfs-servers/{nfs['id']}/shares/todel",
                        expected=204)
        assert_eq(status, 204, "remove share status")
        delete_nfs_server(nfs["id"])

    def test_remove_share_gone_from_server(self):
        nfs = make_nfs_server(PFX + "shrmget", self.vpc["id"],
                              shares=[{"name": "gone"}, {"name": "keep"}])
        req("DELETE", f"/v1/nfs-servers/{nfs['id']}/shares/gone", expected=204)
        _, got = req("GET", f"/v1/nfs-servers/{nfs['id']}")
        names = [s["name"] for s in got["shares"]]
        assert_not_in("gone", names, "removed share gone")
        assert_in("keep", names, "kept share remains")
        delete_nfs_server(nfs["id"])

    def test_remove_missing_share_returns_404(self):
        nfs = make_nfs_server(PFX + "shmiss", self.vpc["id"])
        req("DELETE", f"/v1/nfs-servers/{nfs['id']}/shares/nobody", expected=404)
        delete_nfs_server(nfs["id"])

    # ── Mount config ──────────────────────────────────────────────────────────
    # mount-config requires private_ip to be set (async after VM launch).
    # We test the 404 path (no VM needed) and the field structure via the
    # add_share endpoint which populates path/clients synchronously.

    def test_mount_config_missing_share_returns_404(self):
        nfs = make_nfs_server(PFX + "mntnoshare", self.vpc["id"])
        req("GET", f"/v1/nfs-servers/{nfs['id']}/shares/nobody/mount-config",
            expected=404)
        delete_nfs_server(nfs["id"])

    def test_mount_config_missing_server_returns_404(self):
        req("GET", "/v1/nfs-servers/does-not-exist/shares/data/mount-config",
            expected=404)
