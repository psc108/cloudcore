package provider

import (
	"context"
	"os"

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
	APIURL   types.String `tfsdk:"api_url"`
	APIToken types.String `tfsdk:"api_token"`
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

	c := client.New(apiURL, apiToken)
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
