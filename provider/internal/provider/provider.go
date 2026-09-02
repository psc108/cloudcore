package provider

import (
	"context"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/cloudcore/terraform-provider-cloudcore/internal/client"
	"github.com/cloudcore/terraform-provider-cloudcore/internal/datasources"
	"github.com/cloudcore/terraform-provider-cloudcore/internal/resources"
	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/provider"
	"github.com/hashicorp/terraform-plugin-framework/provider/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

var _ provider.Provider = &CloudCoreProvider{}

type CloudCoreProvider struct {
	version string
}

type CloudCoreProviderModel struct {
	APIURL         types.String `tfsdk:"api_url"`
	APIToken       types.String `tfsdk:"api_token"`
	RequestTimeout types.Int64  `tfsdk:"request_timeout"`
}

func New(version string) func() provider.Provider {
	return func() provider.Provider {
		return &CloudCoreProvider{version: version}
	}
}

func (p *CloudCoreProvider) Metadata(_ context.Context, _ provider.MetadataRequest, resp *provider.MetadataResponse) {
	resp.TypeName = "cloudcore"
	resp.Version = p.version
}

func (p *CloudCoreProvider) Schema(_ context.Context, _ provider.SchemaRequest, resp *provider.SchemaResponse) {
	resp.Schema = schema.Schema{
		Attributes: map[string]schema.Attribute{
			"api_url": schema.StringAttribute{
				Optional:    true,
				Description: "CloudCore API URL. Defaults to CLOUDCORE_API_URL env var.",
			},
			"api_token": schema.StringAttribute{
				Optional:    true,
				Sensitive:   true,
				Description: "CloudCore API token. Defaults to CLOUDCORE_API_TOKEN env var.",
			},
			"request_timeout": schema.Int64Attribute{
				Optional:    true,
				Description: "HTTP request timeout in seconds. Defaults to 30.",
			},
		},
	}
}

func (p *CloudCoreProvider) Configure(ctx context.Context, req provider.ConfigureRequest, resp *provider.ConfigureResponse) {
	var config CloudCoreProviderModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	apiURL := os.Getenv("CLOUDCORE_API_URL")
	if !config.APIURL.IsNull() {
		apiURL = config.APIURL.ValueString()
	}
	apiToken := os.Getenv("CLOUDCORE_API_TOKEN")
	if !config.APIToken.IsNull() {
		apiToken = config.APIToken.ValueString()
	}

	if apiURL == "" {
		resp.Diagnostics.AddError("Missing api_url", "Set api_url or CLOUDCORE_API_URL")
		return
	}
	if apiToken == "" {
		resp.Diagnostics.AddError("Missing api_token", "Set api_token or CLOUDCORE_API_TOKEN")
		return
	}

	// Warn if token is being sent over plain HTTP to a non-local endpoint.
	if !strings.HasPrefix(apiURL, "https://") {
		host := strings.TrimPrefix(strings.TrimPrefix(apiURL, "http://"), "https://")
		host = strings.SplitN(host, "/", 2)[0]
		host = strings.SplitN(host, ":", 2)[0]
		if host != "127.0.0.1" && host != "localhost" {
			resp.Diagnostics.AddWarning(
				"Insecure API URL",
				fmt.Sprintf("api_url %q does not use HTTPS. The API token will be transmitted in plaintext.", apiURL),
			)
		}
	}

	opts := []client.Option{}
	if !config.RequestTimeout.IsNull() && config.RequestTimeout.ValueInt64() > 0 {
		opts = append(opts, client.WithTimeout(time.Duration(config.RequestTimeout.ValueInt64())*time.Second))
	}

	c := client.New(apiURL, apiToken, opts...)
	resp.ResourceData = c
	resp.DataSourceData = c
}

func (p *CloudCoreProvider) Resources(_ context.Context) []func() resource.Resource {
	return []func() resource.Resource{
		resources.NewVPCResource,
		resources.NewInstanceResource,
		resources.NewLoadBalancerResource,
		resources.NewSecurityGroupResource,
		resources.NewDNSZoneResource,
		resources.NewDNSRecordResource,
		resources.NewNFSServerResource,
		resources.NewLBTargetGroupResource,
		resources.NewLBListenerResource,
	}
}

func (p *CloudCoreProvider) DataSources(_ context.Context) []func() datasource.DataSource {
	return []func() datasource.DataSource{
		datasources.NewVPCDataSource,
		datasources.NewInstanceDataSource,
		datasources.NewLoadBalancerDataSource,
		datasources.NewSecurityGroupDataSource,
		datasources.NewNFSServerDataSource,
		datasources.NewDNSZoneDataSource,
		datasources.NewDNSRecordDataSource,
	}
}
