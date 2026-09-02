package resources_test

import (
	"regexp"
	"testing"

	"github.com/cloudcore/terraform-provider-cloudcore/acctest"
	"github.com/hashicorp/terraform-plugin-testing/helper/resource"
)

// instanceConfig creates a VPC + instance. image_id uses the well-known
// CloudCore default image name; adjust if your deployment uses a different ID.
const instanceConfig = `
resource "cloudcore_vpc" "inst" {
  name       = "acc-test-inst-vpc"
  cidr_block = "10.97.0.0/16"
}

resource "cloudcore_instance" "test" {
  name      = "acc-test-instance"
  image_id  = "ubuntu-22.04"
  flavor    = "standard.nano"
  vpc_id    = cloudcore_vpc.inst.id
  subnet_id = "default"
  tags      = { Env = "test" }
}
`

func TestAccInstance_basic(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			// Create — provider polls until status=running
			{
				Config: acctest.ProviderConfig + instanceConfig,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttrSet("cloudcore_instance.test", "id"),
					resource.TestCheckResourceAttr("cloudcore_instance.test", "status", "running"),
					resource.TestCheckResourceAttrSet("cloudcore_instance.test", "private_ip"),
					resource.TestCheckResourceAttrSet("cloudcore_instance.test", "ssh_port"),
					resource.TestCheckResourceAttrSet("cloudcore_instance.test", "ssh_user"),
					resource.TestCheckResourceAttrSet("cloudcore_instance.test", "created_at"),
				),
			},
			// ImportState
			{
				ResourceName:      "cloudcore_instance.test",
				ImportState:       true,
				ImportStateVerify: true,
				// user_data is write-only; timeouts are not stored in API state
				ImportStateVerifyIgnore: []string{"user_data", "timeouts"},
			},
		},
	})
}

func TestAccInstance_flavorValidation(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			{
				Config: acctest.ProviderConfig + `
resource "cloudcore_vpc" "v" {
  name       = "acc-test-flavor-vpc"
  cidr_block = "10.96.0.0/16"
}
resource "cloudcore_instance" "bad" {
  name      = "acc-test-bad-flavor"
  image_id  = "ubuntu-22.04"
  flavor    = "invalid.flavor"
  vpc_id    = cloudcore_vpc.v.id
  subnet_id = "default"
}`,
				ExpectError: regexp.MustCompile(`value must be one of`),
			},
		},
	})
}
