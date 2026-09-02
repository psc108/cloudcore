package resources_test

import (
	"regexp"
	"testing"

	"github.com/cloudcore/terraform-provider-cloudcore/acctest"
	"github.com/hashicorp/terraform-plugin-testing/helper/resource"
)

const lbListenerBase = `
resource "cloudcore_vpc" "listener" {
  name       = "acc-test-listener-vpc"
  cidr_block = "10.86.0.0/16"
}

resource "cloudcore_load_balancer" "listener" {
  name       = "acc-test-listener-lb"
  type       = "network"
  vpc_id     = cloudcore_vpc.listener.id
  subnet_ids = ["default"]
  internal   = true
}

resource "cloudcore_lb_target_group" "listener" {
  lb_id    = cloudcore_load_balancer.listener.id
  name     = "acc-test-listener-tg"
  port     = 80
  protocol = "tcp"
}
`

func TestAccLBListener_basic(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			// Create
			{
				Config: acctest.ProviderConfig + lbListenerBase + `
resource "cloudcore_lb_listener" "test" {
  lb_id           = cloudcore_load_balancer.listener.id
  port            = 80
  protocol        = "tcp"
  target_group_id = cloudcore_lb_target_group.listener.id
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttrSet("cloudcore_lb_listener.test", "id"),
					resource.TestCheckResourceAttr("cloudcore_lb_listener.test", "port", "80"),
					resource.TestCheckResourceAttr("cloudcore_lb_listener.test", "protocol", "tcp"),
					resource.TestCheckResourceAttrSet("cloudcore_lb_listener.test", "status"),
				),
			},
			// Update target group (swap to a new one)
			{
				Config: acctest.ProviderConfig + lbListenerBase + `
resource "cloudcore_lb_target_group" "listener_v2" {
  lb_id    = cloudcore_load_balancer.listener.id
  name     = "acc-test-listener-tg-v2"
  port     = 8080
  protocol = "tcp"
}
resource "cloudcore_lb_listener" "test" {
  lb_id           = cloudcore_load_balancer.listener.id
  port            = 80
  protocol        = "tcp"
  target_group_id = cloudcore_lb_target_group.listener_v2.id
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttrPair(
						"cloudcore_lb_listener.test", "target_group_id",
						"cloudcore_lb_target_group.listener_v2", "id",
					),
				),
			},
			// ImportState — format is lb_id/listener_id
			{
				ResourceName:      "cloudcore_lb_listener.test",
				ImportState:       true,
				ImportStateVerify: true,
			},
		},
	})
}

func TestAccLBListener_protocolValidation(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			{
				Config: acctest.ProviderConfig + lbListenerBase + `
resource "cloudcore_lb_listener" "bad" {
  lb_id           = cloudcore_load_balancer.listener.id
  port            = 443
  protocol        = "udp"
  target_group_id = cloudcore_lb_target_group.listener.id
}`,
				ExpectError: regexp.MustCompile(`value must be one of`),
			},
		},
	})
}
