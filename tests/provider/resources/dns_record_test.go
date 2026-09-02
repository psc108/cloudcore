package resources_test

import (
	"testing"

	"github.com/cloudcore/terraform-provider-cloudcore/acctest"
	"github.com/hashicorp/terraform-plugin-testing/helper/resource"
)

const dnsRecordBase = `
resource "cloudcore_dns_zone" "rec_zone" {
  name = "acc-records.cloudcore.local"
}
`

func TestAccDNSRecord_basic(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			// Create A record with default TTL
			{
				Config: acctest.ProviderConfig + dnsRecordBase + `
resource "cloudcore_dns_record" "test" {
  zone  = cloudcore_dns_zone.rec_zone.name
  name  = "www"
  type  = "A"
  value = "10.0.0.1"
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttr("cloudcore_dns_record.test", "id", "acc-records.cloudcore.local/www/A"),
					resource.TestCheckResourceAttr("cloudcore_dns_record.test", "value", "10.0.0.1"),
					resource.TestCheckResourceAttr("cloudcore_dns_record.test", "ttl", "300"),
				),
			},
			// Update value and TTL (in-place — no RequiresReplace on value/ttl)
			{
				Config: acctest.ProviderConfig + dnsRecordBase + `
resource "cloudcore_dns_record" "test" {
  zone  = cloudcore_dns_zone.rec_zone.name
  name  = "www"
  type  = "A"
  value = "10.0.0.2"
  ttl   = 60
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttr("cloudcore_dns_record.test", "value", "10.0.0.2"),
					resource.TestCheckResourceAttr("cloudcore_dns_record.test", "ttl", "60"),
				),
			},
			// ImportState — format is zone/name/type
			{
				ResourceName:      "cloudcore_dns_record.test",
				ImportState:       true,
				ImportStateVerify: true,
			},
		},
	})
}

func TestAccDNSRecord_cname(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			{
				Config: acctest.ProviderConfig + dnsRecordBase + `
resource "cloudcore_dns_record" "cname" {
  zone  = cloudcore_dns_zone.rec_zone.name
  name  = "alias"
  type  = "CNAME"
  value = "www.acc-records.cloudcore.local"
  ttl   = 120
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttr("cloudcore_dns_record.cname", "id", "acc-records.cloudcore.local/alias/CNAME"),
					resource.TestCheckResourceAttr("cloudcore_dns_record.cname", "type", "CNAME"),
				),
			},
			{
				ResourceName:      "cloudcore_dns_record.cname",
				ImportState:       true,
				ImportStateVerify: true,
			},
		},
	})
}

func TestAccDNSRecord_txt(t *testing.T) {
	acctest.Skip(t)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: acctest.ProviderFactories,
		Steps: []resource.TestStep{
			{
				Config: acctest.ProviderConfig + dnsRecordBase + `
resource "cloudcore_dns_record" "txt" {
  zone  = cloudcore_dns_zone.rec_zone.name
  name  = "@"
  type  = "TXT"
  value = "v=spf1 include:cloudcore.local ~all"
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttr("cloudcore_dns_record.txt", "type", "TXT"),
					resource.TestCheckResourceAttr("cloudcore_dns_record.txt", "value", "v=spf1 include:cloudcore.local ~all"),
				),
			},
		},
	})
}
