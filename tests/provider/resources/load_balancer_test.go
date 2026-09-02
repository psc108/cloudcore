package resources_test

import (
	"regexp"
	"testing"

	"github.com/cloudcore/terraform-provider-cloudcore/acctest"
	"github.com/hashicorp/terraform-plugin-testing/helper/resource"
)

const lbVPCConfig = `
resource "cloudcore_vpc" "lb" {
  name       = "acc-test-lb-vpc"
  cidr_block = "10.88.0.0/16"
}
`

func TestAccLoadBalancer_basic(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			// Create network LB
			{
				Config: acctest.ProviderConfig + lbVPCConfig + `
resource "cloudcore_load_balancer" "test" {
  name       = "acc-test-lb"
  type       = "network"
  vpc_id     = cloudcore_vpc.lb.id
  subnet_ids = ["default"]
  internal   = true
  tags       = { Env = "test" }
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttrSet("cloudcore_load_balancer.test", "id"),
					resource.TestCheckResourceAttr("cloudcore_load_balancer.test", "type", "network"),
					resource.TestCheckResourceAttr("cloudcore_load_balancer.test", "internal", "true"),
					resource.TestCheckResourceAttrSet("cloudcore_load_balancer.test", "dns_name"),
					resource.TestCheckResourceAttrSet("cloudcore_load_balancer.test", "status"),
					resource.TestCheckResourceAttrSet("cloudcore_load_balancer.test", "created_at"),
				),
			},
			// Update tags
			{
				Config: acctest.ProviderConfig + lbVPCConfig + `
resource "cloudcore_load_balancer" "test" {
  name       = "acc-test-lb"
  type       = "network"
  vpc_id     = cloudcore_vpc.lb.id
  subnet_ids = ["default"]
  internal   = true
  tags       = { Env = "test", Updated = "true" }
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttr("cloudcore_load_balancer.test", "tags.Updated", "true"),
				),
			},
			// ImportState
			{
				ResourceName:      "cloudcore_load_balancer.test",
				ImportState:       true,
				ImportStateVerify: true,
			},
		},
	})
}

func TestAccLoadBalancer_application(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			{
				Config: acctest.ProviderConfig + lbVPCConfig + `
resource "cloudcore_load_balancer" "app" {
  name       = "acc-test-alb"
  type       = "application"
  vpc_id     = cloudcore_vpc.lb.id
  subnet_ids = ["default"]
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttr("cloudcore_load_balancer.app", "type", "application"),
				),
			},
			{
				ResourceName:      "cloudcore_load_balancer.app",
				ImportState:       true,
				ImportStateVerify: true,
			},
		},
	})
}

func TestAccLoadBalancer_typeValidation(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			{
				Config: acctest.ProviderConfig + lbVPCConfig + `
resource "cloudcore_load_balancer" "bad" {
  name       = "acc-test-lb-bad"
  type       = "classic"
  vpc_id     = cloudcore_vpc.lb.id
  subnet_ids = ["default"]
}`,
				ExpectError: regexp.MustCompile(`value must be one of`),
			},
		},
	})
}
