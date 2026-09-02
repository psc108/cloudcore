package resources_test

import (
	"regexp"
	"testing"

	"github.com/cloudcore/terraform-provider-cloudcore/acctest"
	"github.com/hashicorp/terraform-plugin-testing/helper/resource"
)

const nfsVPCConfig = `
resource "cloudcore_vpc" "nfs" {
  name       = "acc-test-nfs-vpc"
  cidr_block = "10.90.0.0/16"
}
`

func TestAccNFSServer_basic(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			// Create — provider polls until status=running
			{
				Config: acctest.ProviderConfig + nfsVPCConfig + `
resource "cloudcore_nfs_server" "test" {
  name    = "acc-test-nfs"
  vpc_id  = cloudcore_vpc.nfs.id
  flavor  = "standard.nano"
  disk_gb = 20
  tags    = { Env = "test" }
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttrSet("cloudcore_nfs_server.test", "id"),
					resource.TestCheckResourceAttr("cloudcore_nfs_server.test", "status", "running"),
					resource.TestCheckResourceAttrSet("cloudcore_nfs_server.test", "private_ip"),
					resource.TestCheckResourceAttr("cloudcore_nfs_server.test", "flavor", "standard.nano"),
					resource.TestCheckResourceAttr("cloudcore_nfs_server.test", "disk_gb", "20"),
					resource.TestCheckResourceAttr("cloudcore_nfs_server.test", "tags.Env", "test"),
				),
			},
			// ImportState — timeouts are not persisted in API state
			{
				ResourceName:            "cloudcore_nfs_server.test",
				ImportState:             true,
				ImportStateVerify:       true,
				ImportStateVerifyIgnore: []string{"timeouts"},
			},
		},
	})
}

func TestAccNFSServer_shares(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			// Create with one share
			{
				Config: acctest.ProviderConfig + nfsVPCConfig + `
resource "cloudcore_nfs_server" "shares" {
  name   = "acc-test-nfs-shares"
  vpc_id = cloudcore_vpc.nfs.id
  shares = [
    { name = "data", clients = "vpc" }
  ]
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttr("cloudcore_nfs_server.shares", "shares.#", "1"),
					resource.TestCheckResourceAttr("cloudcore_nfs_server.shares", "shares.0.name", "data"),
					resource.TestCheckResourceAttrSet("cloudcore_nfs_server.shares", "shares.0.path"),
				),
			},
			// Update — add a second share
			{
				Config: acctest.ProviderConfig + nfsVPCConfig + `
resource "cloudcore_nfs_server" "shares" {
  name   = "acc-test-nfs-shares"
  vpc_id = cloudcore_vpc.nfs.id
  shares = [
    { name = "data",    clients = "vpc" },
    { name = "backups", clients = "vpc" }
  ]
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttr("cloudcore_nfs_server.shares", "shares.#", "2"),
				),
			},
			// Update — remove first share
			{
				Config: acctest.ProviderConfig + nfsVPCConfig + `
resource "cloudcore_nfs_server" "shares" {
  name   = "acc-test-nfs-shares"
  vpc_id = cloudcore_vpc.nfs.id
  shares = [
    { name = "backups", clients = "vpc" }
  ]
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttr("cloudcore_nfs_server.shares", "shares.#", "1"),
					resource.TestCheckResourceAttr("cloudcore_nfs_server.shares", "shares.0.name", "backups"),
				),
			},
		},
	})
}

func TestAccNFSServer_flavorValidation(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			{
				Config: acctest.ProviderConfig + nfsVPCConfig + `
resource "cloudcore_nfs_server" "bad" {
  name   = "acc-test-nfs-bad-flavor"
  vpc_id = cloudcore_vpc.nfs.id
  flavor = "invalid.size"
}`,
				ExpectError: regexp.MustCompile(`value must be one of`),
			},
		},
	})
}

func TestAccNFSServer_nameForceReplace(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			{
				Config: acctest.ProviderConfig + nfsVPCConfig + `
resource "cloudcore_nfs_server" "rename" {
  name   = "acc-nfs-before"
  vpc_id = cloudcore_vpc.nfs.id
}`,
			},
			// Rename must trigger destroy+recreate (name baked into cloud-init hostname)
			{
				Config: acctest.ProviderConfig + nfsVPCConfig + `
resource "cloudcore_nfs_server" "rename" {
  name   = "acc-nfs-after"
  vpc_id = cloudcore_vpc.nfs.id
}`,
				Check: resource.TestCheckResourceAttr("cloudcore_nfs_server.rename", "name", "acc-nfs-after"),
			},
		},
	})
}
