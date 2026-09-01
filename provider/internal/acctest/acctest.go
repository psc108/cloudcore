package acctest

import (
	"os"
	"testing"

	"github.com/cloudcore/terraform-provider-cloudcore/internal/provider"
	"github.com/hashicorp/terraform-plugin-framework/providerserver"
	"github.com/hashicorp/terraform-plugin-go/tfprotov6"
)

// ProviderFactories wires the CloudCore provider into the test harness.
var ProviderFactories = map[string]func() (tfprotov6.ProviderServer, error){
	"cloudcore": providerserver.NewProtocol6WithError(provider.New("test")()),
}

// Skip skips the test when the API env vars are absent.
func Skip(t *testing.T) {
	t.Helper()
	if os.Getenv("CLOUDCORE_API_URL") == "" || os.Getenv("CLOUDCORE_API_TOKEN") == "" {
		t.Skip("CLOUDCORE_API_URL and CLOUDCORE_API_TOKEN must be set for acceptance tests")
	}
}

// ProviderConfig is the minimal provider block used in every test config.
const ProviderConfig = `
provider "cloudcore" {}
`
