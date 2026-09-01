package resources_test

import (
	"regexp"
	"testing"

	"github.com/cloudcore/terraform-provider-cloudcore/internal/acctest"
	"github.com/hashicorp/terraform-plugin-testing/helper/resource"
)

func TestAccSecurityGroup_basic(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			// Create with ingress rule
			{
				Config: acctest.ProviderConfig + `
resource "cloudcore_vpc" "sg" {
  name       = "acc-test-sg-vpc"
  cidr_block = "10.95.0.0/16"
}
resource "cloudcore_security_group" "test" {
  name   = "acc-test-sg"
  vpc_id = cloudcore_vpc.sg.id
  ingress_rules = [
    { protocol = "tcp", from_port = 22, to_port = 22, cidr = "0.0.0.0/0", description = "SSH" }
  ]
  tags = { Env = "test" }
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttrSet("cloudcore_security_group.test", "id"),
					resource.TestCheckResourceAttr("cloudcore_security_group.test", "name", "acc-test-sg"),
					resource.TestCheckResourceAttr("cloudcore_security_group.test", "ingress_rules.#", "1"),
					resource.TestCheckResourceAttr("cloudcore_security_group.test", "ingress_rules.0.protocol", "tcp"),
					resource.TestCheckResourceAttrSet("cloudcore_security_group.test", "status"),
				),
			},
			// Update — add egress rule
			{
				Config: acctest.ProviderConfig + `
resource "cloudcore_vpc" "sg" {
  name       = "acc-test-sg-vpc"
  cidr_block = "10.95.0.0/16"
}
resource "cloudcore_security_group" "test" {
  name   = "acc-test-sg"
  vpc_id = cloudcore_vpc.sg.id
  ingress_rules = [
    { protocol = "tcp", from_port = 22, to_port = 22, cidr = "0.0.0.0/0", description = "SSH" }
  ]
  egress_rules = [
    { protocol = "-1", cidr = "0.0.0.0/0" }
  ]
  tags = { Env = "test" }
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttr("cloudcore_security_group.test", "egress_rules.#", "1"),
					resource.TestCheckResourceAttr("cloudcore_security_group.test", "egress_rules.0.protocol", "-1"),
				),
			},
			// ImportState
			{
				ResourceName:      "cloudcore_security_group.test",
				ImportState:       true,
				ImportStateVerify: true,
			},
		},
	})
}

func TestAccSecurityGroup_protocolValidation(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			{
				Config: acctest.ProviderConfig + `
resource "cloudcore_vpc" "v" {
  name       = "acc-test-sgval-vpc"
  cidr_block = "10.94.0.0/16"
}
resource "cloudcore_security_group" "bad" {
  name   = "acc-test-bad-proto"
  vpc_id = cloudcore_vpc.v.id
  ingress_rules = [
    { protocol = "sctp", from_port = 80, to_port = 80, cidr = "0.0.0.0/0" }
  ]
}`,
				ExpectError: regexp.MustCompile(`value must be one of`),
			},
		},
	})
}
