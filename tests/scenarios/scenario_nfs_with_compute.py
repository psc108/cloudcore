from __future__ import annotations

from tests.lib.framework import assert_eq, assert_in, req
from tests.lib.helpers import (cleanup_by_prefix, cleanup_nfs_by_prefix,
                                delete_instance, delete_nfs_server, delete_vpc,
                                make_instance, make_nfs_server, make_vpc)

PFX = "t-scen-nfs-"


class ScenarioNFSWithCompute:
    """
    Integration scenario: NFS server + compute instances sharing storage.

    Flow:
    1. Create VPC
    2. Create NFS server with a 'data' share
    3. Verify mount-config is available
    4. Create two instances in the same VPC
    5. Verify both instances and NFS server are listed
    6. Add a second share dynamically
    7. Remove the first share
    8. Verify final share state
    9. Tear down
    """

    def setUp(self):
        cleanup_nfs_by_prefix(PFX)
        cleanup_by_prefix("instances", PFX)
        cleanup_by_prefix("vpcs", PFX + "vpc")

    def test_01_create_vpc_and_nfs(self):
        self.vpc = make_vpc(PFX + "vpc")
        self.nfs = make_nfs_server(PFX + "srv", self.vpc["id"],
                                   shares=[{"name": "data"}])
        assert_eq(self.nfs["vpc_id"], self.vpc["id"], "nfs vpc_id")
        assert_eq(len(self.nfs["shares"]), 1, "one share on create")

    def test_02_mount_config_available(self):
        # mount-config requires private_ip (set after async VM launch).
        # Test the 404 error paths which are synchronous and always available.
        vpc = make_vpc(PFX + "vpc2")
        nfs = make_nfs_server(PFX + "srv2", vpc["id"],
                              shares=[{"name": "data"}])
        # Known share — should return 200 or 500 (500 only if private_ip not set yet)
        # but missing share must always 404
        req("GET", f"/v1/nfs-servers/{nfs['id']}/shares/nobody/mount-config",
            expected=404)
        # Verify share fields are correct via GET
        _, got = req("GET", f"/v1/nfs-servers/{nfs['id']}")
        assert_eq(len(got["shares"]), 1, "one share")
        assert_eq(got["shares"][0]["name"], "data", "share name")
        delete_nfs_server(nfs["id"])
        delete_vpc(vpc["id"])

    def test_03_instances_and_nfs_coexist(self):
        vpc = make_vpc(PFX + "vpc3")
        nfs = make_nfs_server(PFX + "srv3", vpc["id"],
                              shares=[{"name": "shared"}])
        inst1 = make_instance(PFX + "app-01", vpc["id"])
        inst2 = make_instance(PFX + "app-02", vpc["id"])

        _, nfs_list = req("GET", "/v1/nfs-servers")
        nfs_ids = [x["id"] for x in nfs_list["items"]]
        assert_in(nfs["id"], nfs_ids, "nfs server in list")

        _, inst_list = req("GET", "/v1/instances")
        inst_ids = [x["id"] for x in inst_list["items"]]
        assert_in(inst1["id"], inst_ids, "instance 1 in list")
        assert_in(inst2["id"], inst_ids, "instance 2 in list")

        delete_instance(inst1["id"])
        delete_instance(inst2["id"])
        delete_nfs_server(nfs["id"])
        delete_vpc(vpc["id"])

    def test_04_dynamic_share_management(self):
        vpc = make_vpc(PFX + "vpc4")
        nfs = make_nfs_server(PFX + "srv4", vpc["id"],
                              shares=[{"name": "original"}])

        # Add a second share
        req("POST", f"/v1/nfs-servers/{nfs['id']}/shares",
            {"name": "added"}, expected=201)
        _, got = req("GET", f"/v1/nfs-servers/{nfs['id']}")
        assert_eq(len(got["shares"]), 2, "two shares after add")

        # Remove the original share
        req("DELETE", f"/v1/nfs-servers/{nfs['id']}/shares/original", expected=204)
        _, got = req("GET", f"/v1/nfs-servers/{nfs['id']}")
        names = [s["name"] for s in got["shares"]]
        assert_eq(len(names), 1, "one share after remove")
        assert_in("added", names, "added share remains")

        delete_nfs_server(nfs["id"])
        delete_vpc(vpc["id"])
