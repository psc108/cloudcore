package datasources_test

import (
	"regexp"
	"testing"

	"github.com/cloudcore/terraform-provider-cloudcore/internal/acctest"
	"github.com/hashicorp/terraform-plugin-testing/helper/resource"
)

func TestAccDataSourceVPC_byName(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			{
				Config: acctest.ProviderConfig + `
resource "cloudcore_vpc" "src" {
  name       = "acc-ds-vpc"
  cidr_block = "10.93.0.0/16"
}
data "cloudcore_vpc" "by_name" {
  name = cloudcore_vpc.src.name
  depends_on = [cloudcore_vpc.src]
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttrPair("data.cloudcore_vpc.by_name", "id", "cloudcore_vpc.src", "id"),
					resource.TestCheckResourceAttr("data.cloudcore_vpc.by_name", "cidr_block", "10.93.0.0/16"),
				),
			},
		},
	})
}

func TestAccDataSourceVPC_byID(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			{
				Config: acctest.ProviderConfig + `
resource "cloudcore_vpc" "src" {
  name       = "acc-ds-vpc-id"
  cidr_block = "10.92.0.0/16"
}
data "cloudcore_vpc" "by_id" {
  id = cloudcore_vpc.src.id
  depends_on = [cloudcore_vpc.src]
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttrPair("data.cloudcore_vpc.by_id", "name", "cloudcore_vpc.src", "name"),
				),
			},
		},
	})
}

func TestAccDataSourceInstance_byName(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			{
				Config: acctest.ProviderConfig + `
resource "cloudcore_vpc" "src" {
  name       = "acc-ds-inst-vpc"
  cidr_block = "10.91.0.0/16"
}
resource "cloudcore_instance" "src" {
  name      = "acc-ds-instance"
  image_id  = "ubuntu-22.04"
  flavor    = "standard.nano"
  vpc_id    = cloudcore_vpc.src.id
  subnet_id = "default"
}
data "cloudcore_instance" "by_name" {
  name = cloudcore_instance.src.name
  depends_on = [cloudcore_instance.src]
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttrPair("data.cloudcore_instance.by_name", "id", "cloudcore_instance.src", "id"),
					resource.TestCheckResourceAttr("data.cloudcore_instance.by_name", "status", "running"),
					resource.TestCheckResourceAttrSet("data.cloudcore_instance.by_name", "private_ip"),
				),
			},
		},
	})
}

func TestAccDataSourceVPC_missingKey(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			{
				Config: acctest.ProviderConfig + `
data "cloudcore_vpc" "bad" {}`,
				ExpectError: regexp.MustCompile(`At least one of 'id' or 'name' must be set`),
			},
		},
	})
}
