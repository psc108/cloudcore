package resources_test

import (
	"fmt"
	"testing"

	"github.com/cloudcore/terraform-provider-cloudcore/internal/acctest"
	"github.com/hashicorp/terraform-plugin-testing/helper/resource"
)

func TestAccVPC_basic(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			// Create and read
			{
				Config: acctest.ProviderConfig + `
resource "cloudcore_vpc" "test" {
  name       = "acc-test-vpc"
  cidr_block = "10.99.0.0/16"
  tags       = { Env = "test" }
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttrSet("cloudcore_vpc.test", "id"),
					resource.TestCheckResourceAttr("cloudcore_vpc.test", "name", "acc-test-vpc"),
					resource.TestCheckResourceAttr("cloudcore_vpc.test", "cidr_block", "10.99.0.0/16"),
					resource.TestCheckResourceAttr("cloudcore_vpc.test", "tags.Env", "test"),
					resource.TestCheckResourceAttrSet("cloudcore_vpc.test", "status"),
					resource.TestCheckResourceAttrSet("cloudcore_vpc.test", "created_at"),
				),
			},
			// Update tags
			{
				Config: acctest.ProviderConfig + `
resource "cloudcore_vpc" "test" {
  name       = "acc-test-vpc"
  cidr_block = "10.99.0.0/16"
  tags       = { Env = "test", Updated = "true" }
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttr("cloudcore_vpc.test", "tags.Updated", "true"),
				),
			},
			// ImportState
			{
				ResourceName:      "cloudcore_vpc.test",
				ImportState:       true,
				ImportStateVerify: true,
			},
		},
	})
}

func TestAccVPC_dnsSupport(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			{
				Config: acctest.ProviderConfig + fmt.Sprintf(`
resource "cloudcore_vpc" "dns" {
  name        = "acc-test-vpc-dns"
  cidr_block  = "10.98.0.0/16"
  dns_support = false
}`),
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttr("cloudcore_vpc.dns", "dns_support", "false"),
				),
			},
		},
	})
}
