package resources_test

import (
	"regexp"
	"testing"

	"github.com/cloudcore/terraform-provider-cloudcore/acctest"
	"github.com/hashicorp/terraform-plugin-testing/helper/resource"
)

const lbTGBase = `
resource "cloudcore_vpc" "tg" {
  name       = "acc-test-tg-vpc"
  cidr_block = "10.87.0.0/16"
}

resource "cloudcore_load_balancer" "tg" {
  name       = "acc-test-tg-lb"
  type       = "network"
  vpc_id     = cloudcore_vpc.tg.id
  subnet_ids = ["default"]
  internal   = true
}
`

func TestAccLBTargetGroup_basic(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			// Create with default health check
			{
				Config: acctest.ProviderConfig + lbTGBase + `
resource "cloudcore_lb_target_group" "test" {
  lb_id    = cloudcore_load_balancer.tg.id
  name     = "acc-test-tg"
  port     = 80
  protocol = "tcp"
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttrSet("cloudcore_lb_target_group.test", "id"),
					resource.TestCheckResourceAttr("cloudcore_lb_target_group.test", "name", "acc-test-tg"),
					resource.TestCheckResourceAttr("cloudcore_lb_target_group.test", "port", "80"),
					resource.TestCheckResourceAttr("cloudcore_lb_target_group.test", "protocol", "tcp"),
					resource.TestCheckResourceAttrSet("cloudcore_lb_target_group.test", "status"),
					resource.TestCheckResourceAttr("cloudcore_lb_target_group.test", "health_check.interval", "30"),
					resource.TestCheckResourceAttr("cloudcore_lb_target_group.test", "health_check.healthy_threshold", "2"),
					resource.TestCheckResourceAttr("cloudcore_lb_target_group.test", "health_check.unhealthy_threshold", "2"),
				),
			},
			// Update health check thresholds
			{
				Config: acctest.ProviderConfig + lbTGBase + `
resource "cloudcore_lb_target_group" "test" {
  lb_id    = cloudcore_load_balancer.tg.id
  name     = "acc-test-tg"
  port     = 80
  protocol = "tcp"
  health_check = {
    interval            = 15
    healthy_threshold   = 3
    unhealthy_threshold = 3
  }
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttr("cloudcore_lb_target_group.test", "health_check.interval", "15"),
					resource.TestCheckResourceAttr("cloudcore_lb_target_group.test", "health_check.healthy_threshold", "3"),
				),
			},
			// ImportState — format is lb_id/tg_id
			{
				ResourceName:      "cloudcore_lb_target_group.test",
				ImportState:       true,
				ImportStateVerify: true,
			},
		},
	})
}

func TestAccLBTargetGroup_http(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			{
				Config: acctest.ProviderConfig + lbTGBase + `
resource "cloudcore_load_balancer" "app_tg" {
  name       = "acc-test-app-tg-lb"
  type       = "application"
  vpc_id     = cloudcore_vpc.tg.id
  subnet_ids = ["default"]
  internal   = true
}

resource "cloudcore_lb_target_group" "http" {
  lb_id    = cloudcore_load_balancer.app_tg.id
  name     = "acc-test-http-tg"
  port     = 8080
  protocol = "http"
  health_check = {
    path                = "/health"
    interval            = 10
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttr("cloudcore_lb_target_group.http", "protocol", "http"),
					resource.TestCheckResourceAttr("cloudcore_lb_target_group.http", "health_check.path", "/health"),
					resource.TestCheckResourceAttr("cloudcore_lb_target_group.http", "health_check.interval", "10"),
				),
			},
		},
	})
}

func TestAccLBTargetGroup_protocolValidation(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			{
				Config: acctest.ProviderConfig + lbTGBase + `
resource "cloudcore_lb_target_group" "bad" {
  lb_id    = cloudcore_load_balancer.tg.id
  name     = "acc-test-bad-proto"
  port     = 80
  protocol = "udp"
}`,
				ExpectError: regexp.MustCompile(`value must be one of`),
			},
		},
	})
}
