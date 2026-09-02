package resources_test

import (
	"testing"

	"github.com/cloudcore/terraform-provider-cloudcore/acctest"
	"github.com/hashicorp/terraform-plugin-testing/helper/resource"
)

func TestAccDNSZone_basic(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			// Create and read
			{
				Config: acctest.ProviderConfig + `
resource "cloudcore_dns_zone" "test" {
  name = "acc-test.cloudcore.local"
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttr("cloudcore_dns_zone.test", "id", "acc-test.cloudcore.local"),
					resource.TestCheckResourceAttr("cloudcore_dns_zone.test", "name", "acc-test.cloudcore.local"),
					resource.TestCheckResourceAttrSet("cloudcore_dns_zone.test", "created_at"),
				),
			},
			// ImportState — import by zone name
			{
				ResourceName:      "cloudcore_dns_zone.test",
				ImportState:       true,
				ImportStateVerify: true,
			},
		},
	})
}

func TestAccDNSZone_replaceOnNameChange(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			{
				Config: acctest.ProviderConfig + `
resource "cloudcore_dns_zone" "rename" {
  name = "acc-rename-before.cloudcore.local"
}`,
				Check: resource.TestCheckResourceAttr("cloudcore_dns_zone.rename", "name", "acc-rename-before.cloudcore.local"),
			},
			// Changing name must destroy+recreate, not fail with immutable-field error.
			{
				Config: acctest.ProviderConfig + `
resource "cloudcore_dns_zone" "rename" {
  name = "acc-rename-after.cloudcore.local"
}`,
				Check: resource.TestCheckResourceAttr("cloudcore_dns_zone.rename", "name", "acc-rename-after.cloudcore.local"),
			},
		},
	})
}
